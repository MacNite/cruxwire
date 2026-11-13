#!/usr/bin/env python3
"""Article image resolution for Cruxwire (pure stdlib).

Every card in the onboarding wizard and the feed shows a 16:9 picture area.
A picture is resolved in this order:

  1. RSS `media:content` / `media:thumbnail` / image `enclosure`
  2. an `<img>` safely extracted from the item's HTML (description/content)
  3. the publisher page's `og:image` / `twitter:image` (relative URLs resolved,
     fetched through netsafe with a timeout and byte cap)
  4. a local, topic-specific fallback illustration in static/fallback/

Images are presentational only — nothing here ever blocks a vote or a run.
Resolved metadata is cached (bounded) in IMAGE_CACHE_FILE so the publisher page
is fetched at most once per article.
"""
import base64
import html
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse

import netsafe

IMAGE_CACHE_FILE = os.environ.get('IMAGE_CACHE_FILE', '/data/image_cache.json')
IMAGE_CACHE_MAX = int(os.environ.get('IMAGE_CACHE_MAX', '4000'))
IMAGE_PAGE_TIMEOUT = float(os.environ.get('IMAGE_PAGE_TIMEOUT', '6'))
IMAGE_PAGE_LIMIT = int(os.environ.get('IMAGE_PAGE_LIMIT', str(400 * 1024)))
# Re-try a failed og:image lookup after this long (a slow site may recover).
IMAGE_RETRY_SECONDS = int(os.environ.get('IMAGE_RETRY_SECONDS', str(12 * 3600)))

FALLBACK_DIR_URL = '/static/fallback/'

# Topic → keywords. Order matters: first topic with the most hits wins; ties
# resolve in this order. Keys must have a matching static/fallback/<key>.svg.
TOPICS = {
    'technology': ['tech', 'software', 'app', 'apple', 'google', 'microsoft', 'chip', 'ai ',
                   ' ai', 'artificial intelligence', 'robot', 'computer', 'smartphone', 'iphone',
                   'android', 'startup', 'cyber', 'hack', 'internet', 'cloud', 'data', 'linux',
                   'developer', 'programming', 'openai', 'llm', 'gpu', 'semiconductor', 'devtools'],
    'science': ['science', 'research', 'study', 'scientist', 'space', 'nasa', 'esa', 'physics',
                'biology', 'chemistry', 'astronom', 'planet', 'galaxy', 'quantum', 'fossil',
                'species', 'genome', 'telescope', 'mars', 'moon', 'climate model', 'lab'],
    'energy': ['energy', 'solar', 'wind', 'battery', 'batteries', 'power grid', 'grid', 'oil',
               'gas', 'nuclear', 'renewable', 'electric vehicle', ' ev ', 'evs', 'hydrogen',
               'emission', 'carbon', 'climate', 'heat pump', 'photovoltaic', 'coal', 'fusion'],
    'business': ['business', 'market', 'stock', 'shares', 'economy', 'economic', 'inflation',
                 'bank', 'finance', 'investor', 'earnings', 'revenue', 'profit', 'ipo',
                 'acquisition', 'merger', 'ceo', 'company', 'trade', 'tariff', 'startup funding'],
    'world': ['world', 'war', 'election', 'government', 'minister', 'president', 'parliament',
              'politic', 'ukraine', 'russia', 'china', 'eu ', 'european union', 'nato', 'un ',
              'united nations', 'border', 'protest', 'diplomat', 'sanction', 'policy', 'law'],
    'health': ['health', 'medical', 'medicine', 'hospital', 'disease', 'vaccine', 'virus',
               'cancer', 'drug', 'patient', 'doctor', 'mental health', 'covid', 'nutrition',
               'fitness', 'clinical'],
    'sports': ['sport', 'football', 'soccer', 'basketball', 'tennis', 'olympic', 'league',
               'match', 'championship', 'nba', 'nfl', 'fifa', 'bundesliga', 'formula 1', 'f1 ',
               'cup final', 'tournament'],
    'gaming': ['game', 'gaming', 'playstation', 'xbox', 'nintendo', 'steam', 'esports', 'console'],
    'culture': ['music', 'film', 'movie', 'tv ', 'series', 'book', 'art ', 'artist', 'culture',
                'festival', 'album', 'netflix', 'celebrity', 'photography', 'design',
                'woodworking', 'recipe', 'food', 'travel'],
}
FALLBACK_TOPICS = tuple(TOPICS) + ('general',)

# Category keys (from categories.json) that map straight onto a topic.
CATEGORY_TOPIC = {
    'ai': 'technology', 'tech': 'technology', 'devtools': 'technology',
    'technology': 'technology', 'productivity': 'technology', 'pm': 'business',
    'science': 'science', 'space': 'science', 'energy': 'energy', 'climate': 'energy',
    'business': 'business', 'finance': 'business', 'economy': 'business',
    'world': 'world', 'politics': 'world', 'news': 'general', 'general': 'general',
    'health': 'health', 'sports': 'sports', 'sport': 'sports', 'gaming': 'gaming',
    'music': 'culture', 'photography': 'culture', 'woodworking': 'culture', 'culture': 'culture',
}


def infer_topic(title='', description='', category=''):
    """Cheap keyword topic classifier (no LLM). Used to balance onboarding
    candidates and to pick a fallback illustration."""
    text = f' {title} {description} '.lower()
    best, best_hits = None, 0
    for topic, words in TOPICS.items():
        hits = sum(1 for w in words if w in text)
        if hits > best_hits:
            best, best_hits = topic, hits
    if best:
        return best
    return CATEGORY_TOPIC.get((category or '').lower(), 'general')


def fallback_image(topic):
    topic = topic if topic in FALLBACK_TOPICS else 'general'
    return f'{FALLBACK_DIR_URL}{topic}.svg'


# ──────────────────────────────────────────────────────────────────────
# URL normalisation
# ──────────────────────────────────────────────────────────────────────
_TRACKING_PARAMS = re.compile(r'^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ref_src|igshid|cmpid|ocid)$', re.I)


def normalize_image_url(raw, base=None):
    """Clean an image URL: unescape entities, resolve relative / protocol-
    relative against `base`, require http(s) with a host, drop fragments.
    Returns None for anything unusable (data:, javascript:, blank, tracking
    pixels by obvious name)."""
    if not raw or not isinstance(raw, str):
        return None
    u = html.unescape(raw).strip().strip('"\'')
    if not u or len(u) > 2048:
        return None
    if u.startswith('//'):
        u = 'https:' + u
    if base:
        u = urllib.parse.urljoin(base, u)
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ('http', 'https') or not p.netloc:
        return None
    path_l = p.path.lower()
    if any(t in path_l for t in ('pixel.gif', 'spacer.gif', '/1x1', 'blank.gif', 'transparent.gif')):
        return None
    if path_l.endswith('.svg') and 'logo' in path_l:
        return None   # publisher logos make poor previews
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or '/', p.query, ''))


def canonical_url(url):
    """Stable form of an article URL for de-duplicating feedback: lower-case
    scheme/host, drop fragment, tracking params and trailing slash."""
    if not isinstance(url, str) or not url.strip():
        return ''
    p = urllib.parse.urlsplit(url.strip())
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if not _TRACKING_PARAMS.match(k)]
    host = (p.hostname or '').lower()
    if p.port and not ((p.scheme == 'http' and p.port == 80) or (p.scheme == 'https' and p.port == 443)):
        host = f'{host}:{p.port}'
    path = p.path.rstrip('/') or '/'
    return urllib.parse.urlunsplit((p.scheme.lower(), host, path, urllib.parse.urlencode(q), ''))


# ──────────────────────────────────────────────────────────────────────
# Extraction
# ──────────────────────────────────────────────────────────────────────
_ATTR = r'''{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))'''


def _attr(tag, name):
    m = re.search(_ATTR.format(name=name), tag, re.IGNORECASE)
    if not m:
        return None
    return next((g for g in m.groups() if g is not None), None)


def extract_rss_image(block, base=None):
    """Picture from one RSS <item>/Atom <entry> block (raw XML text), or None.
    Steps 1+2 of the resolution order."""
    if not block:
        return None
    # 1. media:content / media:thumbnail / enclosure (largest declared width wins)
    best, best_w = None, -1
    for tag in re.findall(r'<media:(?:content|thumbnail)\b[^>]*>', block, re.IGNORECASE):
        url = _attr(tag, 'url')
        medium = (_attr(tag, 'medium') or '').lower()
        typ = (_attr(tag, 'type') or '').lower()
        if medium and medium != 'image':
            continue
        if typ and not typ.startswith('image/'):
            continue
        try:
            w = int(_attr(tag, 'width') or 0)
        except ValueError:
            w = 0
        n = normalize_image_url(url, base)
        if n and w > best_w:
            best, best_w = n, w
    if best:
        return best
    for tag in re.findall(r'<enclosure\b[^>]*>', block, re.IGNORECASE):
        typ = (_attr(tag, 'type') or '').lower()
        url = _attr(tag, 'url')
        if typ.startswith('image/') or (not typ and re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', url or '', re.I)):
            n = normalize_image_url(url, base)
            if n:
                return n
    # Atom <link rel="enclosure" type="image/...">
    for tag in re.findall(r'<link\b[^>]*rel=["\']enclosure["\'][^>]*>', block, re.IGNORECASE):
        if (_attr(tag, 'type') or '').lower().startswith('image/'):
            n = normalize_image_url(_attr(tag, 'href'), base)
            if n:
                return n
    # 2. first plausible <img> in the item's HTML (escaped or CDATA)
    htmlish = html.unescape(block) if '&lt;img' in block.lower() else block
    for tag in re.findall(r'<img\b[^>]*>', htmlish, re.IGNORECASE):
        src = _attr(tag, 'src') or _attr(tag, 'data-src')
        try:
            w = int(_attr(tag, 'width') or 0)
            h = int(_attr(tag, 'height') or 0)
        except ValueError:
            w = h = 0
        if (w and w < 80) or (h and h < 60):
            continue   # tracking pixels / icons
        n = normalize_image_url(src, base)
        if n:
            return n
    return None


def extract_page_image(page_html, page_url):
    """og:image / twitter:image from a publisher page (step 3), or None."""
    if not page_html:
        return None
    head = page_html[:200000]
    for tag in re.findall(r'<meta\b[^>]*>', head, re.IGNORECASE):
        key = (_attr(tag, 'property') or _attr(tag, 'name') or '').lower()
        if key in ('og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image', 'twitter:image:src'):
            n = normalize_image_url(_attr(tag, 'content'), page_url)
            if n:
                return n
    m = re.search(r'<link\b[^>]*rel=["\']image_src["\'][^>]*>', head, re.IGNORECASE)
    if m:
        return normalize_image_url(_attr(m.group(0), 'href'), page_url)
    return None


def extract_page_meta(page_html, page_url):
    """Title / description / site name / image / canonical URL from a page's
    head — used for seed-article import."""
    out = {'title': None, 'description': None, 'site': None,
           'image': extract_page_image(page_html, page_url), 'canonical': None}
    head = (page_html or '')[:200000]
    for tag in re.findall(r'<meta\b[^>]*>', head, re.IGNORECASE):
        key = (_attr(tag, 'property') or _attr(tag, 'name') or '').lower()
        val = _attr(tag, 'content')
        if not val:
            continue
        val = clean_text(val)
        if key in ('og:title', 'twitter:title') and not out['title']:
            out['title'] = val
        elif key in ('og:description', 'description', 'twitter:description') and not out['description']:
            out['description'] = val
        elif key == 'og:site_name' and not out['site']:
            out['site'] = val
    if not out['title']:
        m = re.search(r'<title[^>]*>([\s\S]*?)</title>', head, re.IGNORECASE)
        if m:
            out['title'] = clean_text(m.group(1))
    m = re.search(r'<link\b[^>]*rel=["\']canonical["\'][^>]*>', head, re.IGNORECASE)
    if m:
        href = _attr(m.group(0), 'href')
        if href:
            c = urllib.parse.urljoin(page_url, html.unescape(href))
            if urllib.parse.urlsplit(c).scheme in ('http', 'https'):
                out['canonical'] = c
    for k in ('title', 'description', 'site'):
        if out[k]:
            out[k] = out[k][:300]
    return out


def clean_text(s, limit=None):
    """Strip tags + decode entities + collapse whitespace (safe plain text)."""
    if not s:
        return ''
    s = re.sub(r'<!\[CDATA\[|\]\]>', '', s)
    s = re.sub(r'<(script|style)[\s\S]*?</\1>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(html.unescape(s)) if '&amp;' in s else html.unescape(s)
    s = re.sub(r'<[^>]+>', ' ', s)          # entity-escaped markup, once decoded
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit] if limit else s


# ──────────────────────────────────────────────────────────────────────
# Google News
# ──────────────────────────────────────────────────────────────────────
def is_google_news(url):
    host = (urllib.parse.urlsplit(url or '').hostname or '').lower()
    return host == 'news.google.com'


def decode_google_news_url(url):
    """Best-effort offline decode of a news.google.com/rss/articles/<id> link
    to the publisher URL. Older ids embed the URL in a base64 protobuf; newer
    ones don't (returns None, and the caller degrades to the fallback)."""
    if not is_google_news(url):
        return None
    path = urllib.parse.urlsplit(url).path
    m = re.search(r'/(?:rss/)?articles/([A-Za-z0-9_\-]+)', path)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
    except Exception:
        return None
    um = re.search(rb'https?://[\x21-\x7e]+', raw)
    if not um:
        return None
    cand = um.group(0).decode('ascii', errors='ignore')
    # Trailing protobuf bytes can bleed in; cut at the first control-ish char.
    cand = re.split(r'[\x00-\x20"<>\\^`{|}]', cand)[0]
    p = urllib.parse.urlsplit(cand)
    if p.scheme in ('http', 'https') and p.netloc and 'google.' not in p.netloc:
        return cand
    return None


def resolve_canonical(url):
    """Resolve a feed link to a fetchable publisher URL where possible (only
    Google News needs this today). Never raises; returns the input on failure."""
    if is_google_news(url):
        return decode_google_news_url(url) or url
    return url


# ──────────────────────────────────────────────────────────────────────
# Cache + page lookup
# ──────────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache = None


def _load_cache():
    global _cache
    if _cache is None:
        try:
            with open(IMAGE_CACHE_FILE, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
            _cache = d if isinstance(d, dict) else {}
        except Exception:
            _cache = {}
    return _cache


def _save_cache():
    c = _cache or {}
    if len(c) > IMAGE_CACHE_MAX:
        keep = sorted(c.items(), key=lambda kv: kv[1].get('t', 0), reverse=True)[:IMAGE_CACHE_MAX]
        c.clear()
        c.update(keep)
    d = os.path.dirname(IMAGE_CACHE_FILE) or '.'
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.images-', suffix='.json', dir=d)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(c, fh)
        os.replace(tmp, IMAGE_CACHE_FILE)
    except Exception:
        pass   # the cache is an optimisation only


def cache_get(article_url):
    key = canonical_url(article_url)
    with _cache_lock:
        return dict(_load_cache().get(key) or {}) or None


def cache_put(article_url, image, kind):
    key = canonical_url(article_url)
    if not key:
        return
    with _cache_lock:
        _load_cache()[key] = {'image': image, 'kind': kind, 't': int(time.time())}
        _save_cache()


def lookup_page_image(article_url, fetcher=None):
    """og:image for an article page via netsafe (cached). Returns
    (image_or_None, kind) where kind is 'og' | 'none' | 'blocked' | 'error'.
    `fetcher(url) -> (final_url, html)` is injectable for tests."""
    cached = cache_get(article_url)
    if cached and (cached.get('image') or time.time() - cached.get('t', 0) < IMAGE_RETRY_SECONDS):
        return cached.get('image'), cached.get('kind') or 'og'
    target = resolve_canonical(article_url)
    image, kind = None, 'none'
    try:
        if fetcher:
            final_url, body = fetcher(target)
        else:
            resp = netsafe.fetch(target, timeout=IMAGE_PAGE_TIMEOUT, limit=IMAGE_PAGE_LIMIT,
                                 accept='text/html,application/xhtml+xml')
            final_url, body = resp.url, (resp.text() if 'html' in resp.content_type or not resp.content_type else '')
        # Google News pages that didn't decode offline land on a Google
        # interstitial whose og:image is Google's own logo — not a preview.
        if not is_google_news(final_url):
            image = extract_page_image(body, final_url)
            kind = 'og' if image else 'none'
    except netsafe.BlockedURL:
        kind = 'blocked'
    except Exception:
        kind = 'error'
    cache_put(article_url, image, kind)
    return image, kind


def resolve_for_article(article, allow_fetch=True, fetcher=None):
    """Full resolution for an article dict ({url, title, description?,
    category?, image?}). Returns {'image', 'image_kind', 'fallback_image',
    'topic'}; `image` may be None but `fallback_image` is always set."""
    topic = article.get('topic') or infer_topic(article.get('title', ''),
                                                article.get('description', ''),
                                                article.get('category', ''))
    image = normalize_image_url(article.get('image')) if article.get('image') else None
    kind = 'rss' if image else None
    if not image and allow_fetch and article.get('url'):
        image, k = lookup_page_image(article['url'], fetcher=fetcher)
        kind = k if image else 'fallback'
    return {'image': image, 'image_kind': kind or 'fallback',
            'fallback_image': fallback_image(topic), 'topic': topic}
