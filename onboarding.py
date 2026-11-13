#!/usr/bin/env python3
"""First-run onboarding: source validation, a balanced candidate queue for
training, seed-article import, completion, and server-side article lookup.

The candidate pool (ONBOARDING_POOL_FILE) is built WITHOUT the LLM — just feed
parsing — so a brand-new install can start training within seconds, before the
first (slow, CPU-bound) scoring run finishes. It is merged with the current
digest (already-scored articles with images) when one exists.
"""
import concurrent.futures
import hashlib
import json
import os
import random
import re
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import images
import netsafe
import pipeline
import preferences
import settings
import users

ONBOARDING_POOL_FILE = os.environ.get('ONBOARDING_POOL_FILE', '/data/onboarding_pool.json')
POOL_LOOKBACK_HOURS = int(os.environ.get('ONBOARDING_LOOKBACK_HOURS', '168'))
POOL_PER_FEED = int(os.environ.get('ONBOARDING_PER_FEED', '40'))
POOL_MAX_AGE_S = int(os.environ.get('ONBOARDING_POOL_MAX_AGE_S', str(3 * 3600)))
BATCH_MAX = 20
MAX_SEED_URLS = 10

# Editable starting points shown in the Sources step (the user can change or
# remove all of them). Google News "search" feeds turn any query into a feed.
EXAMPLE_FEEDS = [
    {'source': 'BBC News — World', 'url': 'https://feeds.bbci.co.uk/news/world/rss.xml', 'topic': 'world'},
    {'source': 'Ars Technica', 'url': 'https://feeds.arstechnica.com/arstechnica/index', 'topic': 'technology'},
    {'source': 'ScienceDaily', 'url': 'https://www.sciencedaily.com/rss/all.xml', 'topic': 'science'},
    {'source': 'Google News: renewable energy',
     'url': 'https://news.google.com/rss/search?q=renewable+energy&hl=en-US&gl=US&ceid=US:en', 'topic': 'energy'},
    {'source': 'Google News: Energiewende (DE)',
     'url': 'https://news.google.com/rss/search?q=Energiewende&hl=de&gl=DE&ceid=DE:de', 'topic': 'energy'},
    {'source': 'Hacker News', 'url': 'https://hnrss.org/frontpage', 'topic': 'technology'},
]

_pool_lock = threading.Lock()
_image_sem = threading.BoundedSemaphore(4)   # bounded concurrent og:image lookups


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _atomic_write(path, obj):
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.pool-', suffix='.json', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────
# Feed inspection (validation with sample headlines)
# ──────────────────────────────────────────────────────────────────────
def google_news_search_url(query, lang='en-US', country='US'):
    """Build a Google News RSS search feed URL for a free-text query."""
    q = (query or '').strip()[:200]
    if not q:
        raise ValueError('query is required')
    lang = lang if isinstance(lang, str) and len(lang) <= 8 else 'en-US'
    country = country if isinstance(country, str) and len(country) <= 4 else 'US'
    hl = lang
    ceid = f'{country}:{lang.split("-")[0]}'
    return ('https://news.google.com/rss/search?' +
            urllib.parse.urlencode({'q': q, 'hl': hl, 'gl': country, 'ceid': ceid}))


def inspect_feed(url, timeout=10, sample=5):
    """Fetch (safely) and parse a feed. Returns {'ok', 'kind', 'title',
    'count', 'sample': [{title, url, published_at}], 'error'}. Errors are
    phrased so the user can act on them."""
    if not url or not isinstance(url, str):
        return {'ok': False, 'error': 'Enter a feed URL.'}
    url = url.strip()
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.netloc:
        return {'ok': False, 'error': 'The URL must start with http:// or https:// and include a host.'}
    try:
        resp = netsafe.fetch(url, timeout=timeout, limit=pipeline.FEED_LIMIT,
                             accept='application/rss+xml, application/atom+xml, application/xml, text/xml, */*')
    except netsafe.ResolveError:
        return {'ok': False, 'error': f'Could not find the host “{p.hostname}” — check the URL for typos.'}
    except netsafe.BlockedURL as exc:
        return {'ok': False, 'error': f'Blocked for safety: {exc}. Private/LAN addresses are not allowed '
                                      f'(set ALLOW_PRIVATE_FETCH=1 to permit them).'}
    except Exception as exc:
        code = getattr(exc, 'code', None)
        if code:
            hint = ' (not found — check the URL)' if code == 404 else (
                ' (access denied by the site)' if code in (401, 403) else '')
            return {'ok': False, 'error': f'The site answered HTTP {code}{hint}.'}
        reason = getattr(exc, 'reason', exc)
        return {'ok': False, 'error': f'Could not reach the feed: {reason}'}
    body = resp.text()
    low = body[:20000].lower()
    kind = None
    if '<rss' in low or '<rdf:rdf' in low:
        kind = 'rss'
    elif '<feed' in low and 'xmlns' in low:
        kind = 'atom'
    if not kind:
        hint = ' It looks like a web page — look for an RSS/Atom link on it.' if '<html' in low else ''
        return {'ok': False, 'error': 'That URL did not return an RSS or Atom feed.' + hint}
    m = re.search(r'<title[^>]*>\s*(?:<!\[CDATA\[\s*)?([^<\]]+?)(?:\s*\]\]>)?\s*</title>', body, re.I)
    title = images.clean_text(m.group(1), 120) if m else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=3650)
    items = pipeline.parse_feed(body, title or p.netloc, 'general', cutoff)
    if not items:
        return {'ok': False, 'kind': kind, 'title': title,
                'error': 'The feed parsed but contains no articles right now.'}
    items.sort(key=lambda a: a['published_at'], reverse=True)
    return {'ok': True, 'kind': kind, 'title': title, 'count': len(items),
            'sample': [{'title': a['title'], 'url': a['url'], 'published_at': a['published_at']}
                       for a in items[:sample]]}


# ──────────────────────────────────────────────────────────────────────
# Candidate pool
# ──────────────────────────────────────────────────────────────────────
def _feeds_key(feeds):
    urls = sorted(f.get('url', '') for f in feeds if isinstance(f, dict))
    return hashlib.sha1('\n'.join(urls).encode()).hexdigest()[:16]


def _load_pool():
    try:
        with open(users.path(ONBOARDING_POOL_FILE), 'r', encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _digest_articles():
    return [a for a in pipeline._load_prev_digest_articles() if isinstance(a, dict) and a.get('id')]


def _candidate_from(a):
    meta = images.resolve_for_article(a, allow_fetch=False)
    cached = None if meta['image'] else images.cache_get(a.get('url', ''))
    image = meta['image'] or (cached or {}).get('image')
    return {
        'id': a['id'],
        'title': a.get('title', ''),
        'url': a.get('url', ''),
        'source': a.get('source', ''),
        'publisher': a.get('publisher') or a.get('source', ''),
        'category': a.get('category', ''),
        'published_at': a.get('published_at'),
        'description': (a.get('description') or a.get('summary') or '')[:400],
        'image': image,
        'topic': meta['topic'],
        'fallback_image': meta['fallback_image'],
    }


def refresh_pool(feeds=None, fetch=None):
    """Re-fetch every feed (no LLM) and rebuild the candidate pool. `fetch`
    (url -> text) is injectable for tests. Returns the pool dict."""
    feeds = feeds if feeds is not None else pipeline.load_feeds()
    fetch = fetch or (lambda u: pipeline.http_get_text(u, pipeline.FEED_TIMEOUT))
    cfg = settings.load()
    blocklist = [k.lower() for k in cfg['block_keywords']]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=POOL_LOOKBACK_HOURS)
    errors, arts = [], []

    def one(feed):
        url = feed.get('url', '')
        body = fetch(url)
        if not body:
            return feed, None
        return feed, pipeline.parse_feed(body, feed.get('source', ''), feed.get('category', 'general'), cutoff)

    valid = [f for f in feeds if isinstance(f, dict) and f.get('url')]
    if valid:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(valid))) as ex:
            for feed, items in ex.map(one, valid):
                if items is None:
                    errors.append({'url': feed.get('url'), 'source': feed.get('source'),
                                   'error': 'Could not fetch this feed right now.'})
                    continue
                items.sort(key=lambda a: a['published_at'], reverse=True)
                arts.extend(items[:POOL_PER_FEED])

    seen_urls, out = set(), []
    for a in _digest_articles() + arts:
        canon = images.canonical_url(a.get('url', ''))
        if not canon or canon in seen_urls:
            continue
        title = a.get('title', '')
        if pipeline.TITLE_BLOCKLIST.search(title) or settings.is_blocked(title, blocklist):
            continue
        seen_urls.add(canon)
        a = dict(a)
        a['id'] = a.get('id') or pipeline.stable_id(a['url'])
        out.append(_candidate_from(a))
    pool = {'fetchedAt': _now_iso(), 'fetchedTs': time.time(), 'feedsKey': _feeds_key(feeds),
            'feedErrors': errors, 'articles': out}
    try:
        _atomic_write(users.path(ONBOARDING_POOL_FILE), pool)
    except Exception as exc:
        pipeline.log(f'onboarding pool write failed: {exc}')
    return pool


def get_pool(force=False):
    feeds = pipeline.load_feeds()
    with _pool_lock:
        pool = _load_pool()
        stale = (pool is None or force
                 or pool.get('feedsKey') != _feeds_key(feeds)
                 or time.time() - (pool.get('fetchedTs') or 0) > POOL_MAX_AGE_S
                 or not pool.get('articles'))
        if stale:
            pool = refresh_pool(feeds)
        return pool


def _counts(items, keyf):
    c = {}
    for it in items:
        k = keyf(it)
        c[k] = c.get(k, 0) + 1
    return c


def select_batch(pool_articles, feedback, skipped, exclude=(), n=8, rng=None):
    """Balanced candidate selection. Excludes voted / skipped / excluded ids and
    already-voted canonical URLs; rotates across feeds (least-voted feed
    first) and, within a feed, prefers the topic least represented so far.
    Returns (batch, remaining_count)."""
    rng = rng or random.Random()
    voted_ids = set(feedback)
    voted_urls = {e.get('canonicalUrl') for e in feedback.values() if e.get('canonicalUrl')}
    exclude = set(exclude or ())
    cands = [a for a in pool_articles
             if a['id'] not in voted_ids and a['id'] not in skipped and a['id'] not in exclude
             and images.canonical_url(images.resolve_canonical(a.get('url', ''))) not in voted_urls]
    remaining = len(cands)
    src_count = _counts(feedback.values(), lambda e: e.get('source') or '')
    topic_count = _counts(feedback.values(), lambda e: e.get('topic') or 'general')
    queues = {}
    for a in sorted(cands, key=lambda a: a.get('published_at') or '', reverse=True):
        queues.setdefault(a.get('source') or '', []).append(a)
    batch = []
    while len(batch) < n and any(queues.values()):
        order = sorted((s for s, q in queues.items() if q),
                       key=lambda s: (src_count.get(s, 0), rng.random()))
        for s in order:
            if len(batch) >= n:
                break
            q = queues[s]
            if not q:
                continue
            # least-represented topic first; recency breaks ties (stable sort)
            best = min(range(len(q)), key=lambda i: topic_count.get(q[i].get('topic') or 'general', 0))
            a = q.pop(best)
            batch.append(a)
            src_count[s] = src_count.get(s, 0) + 1
            t = a.get('topic') or 'general'
            topic_count[t] = topic_count.get(t, 0) + 1
    return batch, remaining - len(batch)


def progress_payload():
    cfg = settings.load()
    data = preferences.load()
    pr = preferences.progress(data)
    min_likes = cfg['onboarding_min_likes']
    return {
        **pr,
        'minLikes': min_likes,
        'recommendedDislikes': cfg['onboarding_recommended_dislikes'],
        'recommendedTopics': 3,
        'canFinish': pr['likes'] >= min_likes,
    }


def status_payload(include_batch=True, exclude=(), n=8, force_pool=False):
    data = preferences.load()
    out = {
        'onboarding': {k: v for k, v in data['onboarding'].items() if k != 'skipped'},
        'progress': progress_payload(),
        'feeds': pipeline.load_feeds(),
        'examples': EXAMPLE_FEEDS,
    }
    if include_batch:
        pool = get_pool(force=force_pool)
        batch, remaining = select_batch(pool.get('articles') or [], data['feedback'],
                                        set(data['onboarding'].get('skipped') or []), exclude,
                                        max(1, min(BATCH_MAX, n)))
        out.update({'candidates': batch, 'remaining': remaining,
                    'exhausted': not batch, 'feedErrors': pool.get('feedErrors') or [],
                    'poolSize': len(pool.get('articles') or [])})
    return out


# ──────────────────────────────────────────────────────────────────────
# Server-side article lookup (votes reference articles by id only)
# ──────────────────────────────────────────────────────────────────────
def find_article(article_id):
    """Locate an article the server already knows: current digest, onboarding
    pool, or an existing feedback entry. Returns (article_dict, origin) or
    (None, None). Client-supplied titles/scores are never trusted."""
    if not isinstance(article_id, str) or not article_id or len(article_id) > 64:
        return None, None
    for a in _digest_articles():
        if a.get('id') == article_id:
            return a, 'feed'
    pool = _load_pool() or {}
    for a in pool.get('articles') or []:
        if a.get('id') == article_id:
            return a, 'onboarding'
    e = preferences.feedback().get(article_id)
    if e:
        return {'id': article_id, 'title': e.get('title'), 'url': e.get('url'),
                'description': e.get('description'), 'source': e.get('source'),
                'category': e.get('category'), 'topic': e.get('topic'),
                'image': e.get('image'), 'published_at': e.get('publishedAt')}, e.get('origin') or 'feed'
    return None, None


def resolve_image(article_id, fetcher=None):
    """Image for a known article id — RSS image, else cached/fetched og:image,
    else the topic fallback. Never raises."""
    a, _ = find_article(article_id)
    if not a:
        return None
    with _image_sem:
        meta = images.resolve_for_article(a, allow_fetch=True, fetcher=fetcher)
    return {'id': article_id, **meta}


# ──────────────────────────────────────────────────────────────────────
# Seeds + completion
# ──────────────────────────────────────────────────────────────────────
def import_seeds(urls, fetcher=None):
    """Import user-provided liked article URLs as +1 votes. Each URL is
    fetched through netsafe (SSRF-checked, size/time-bounded) and only its
    title/description/image metadata is kept."""
    if not isinstance(urls, list):
        raise ValueError('urls[] is required')
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()][:MAX_SEED_URLS]
    results = []
    for url in urls:
        try:
            if fetcher:
                final_url, body = fetcher(url)
            else:
                resp = netsafe.fetch(url, timeout=8, limit=400 * 1024,
                                     accept='text/html,application/xhtml+xml')
                final_url, body = resp.url, resp.text()
        except netsafe.BlockedURL as exc:
            results.append({'url': url, 'ok': False, 'error': f'Blocked for safety: {exc}'})
            continue
        except Exception as exc:
            results.append({'url': url, 'ok': False, 'error': f'Could not fetch: {getattr(exc, "reason", exc)}'})
            continue
        meta = images.extract_page_meta(body, final_url)
        if not meta.get('title'):
            results.append({'url': url, 'ok': False, 'error': 'No article title found on that page.'})
            continue
        canonical = meta.get('canonical') or final_url
        host = (urllib.parse.urlsplit(canonical).hostname or '').removeprefix('www.')
        article = {'id': pipeline.stable_id(canonical), 'title': meta['title'], 'url': canonical,
                   'description': meta.get('description') or '', 'source': meta.get('site') or host,
                   'image': meta.get('image'), 'published_at': None, 'category': ''}
        entry, _ = preferences.set_vote(article['id'], article, 1, origin='seed')
        results.append({'url': url, 'ok': True, 'id': article['id'], 'title': entry['title']})
    if any(r['ok'] for r in results) and preferences.onboarding()['status'] == 'not_started':
        preferences.set_status('in_progress')
    return results


def complete(trigger_refresh=True):
    """Validate the minimum likes and mark onboarding complete; kick off the
    background profile warm-up + a personalised refresh. Returns
    (ok, payload)."""
    pr = progress_payload()
    if not pr['canFinish']:
        return False, {'error': f"Like at least {pr['minLikes']} stories first "
                                f"({pr['likes']}/{pr['minLikes']}).", 'progress': pr}
    ob = preferences.set_status('completed')
    refresh = None
    if trigger_refresh:
        # The refresh run embeds the new likes itself; only warm the profile
        # separately when a run can't start right now.
        refresh = pipeline.refresh_async(force=True)
        if refresh != 'started':
            pipeline.warm_feedback_embeddings_async()
    return True, {'onboarding': {k: v for k, v in ob.items() if k != 'skipped'},
                  'progress': pr, 'refresh': refresh}
