#!/usr/bin/env python3
"""Ingestion pipeline for Cruxwire.

Ported from the old n8n Code nodes into pure Python stdlib. One run_once():
fetch feeds -> parse RSS/Atom -> filter -> score+summarise+embed via Ollama ->
cluster same-story coverage -> atomically write digest.json.

A background scheduler thread runs it on a cron-like schedule. No external
dependencies; the only network calls are to the feeds, the article pages, and
the Ollama host.
"""
import concurrent.futures
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import article_ids
import corpus_archive
import images
import netsafe
import settings
import users

# ──────────────────────────────────────────────────────────────────────
# CONFIG. Deploy-time wiring (Ollama, ports, paths) stays env-only. The
# tuning knobs below (similarity, boosts, lookback, schedule, blocklist) are
# read from settings.py at runtime so the UI can change them without a
# restart; the env values here are the defaults settings.py falls back to.
# ──────────────────────────────────────────────────────────────────────
OLLAMA_HOST   = os.environ.get('OLLAMA_HOST', 'http://localhost:11434').rstrip('/')
OLLAMA_MODEL  = os.environ.get('OLLAMA_MODEL', 'qwen3.5:4b')
EMBED_MODEL   = os.environ.get('EMBED_MODEL', 'nomic-embed-text')

SIM_THRESHOLD = float(os.environ.get('SIM_THRESHOLD', '0.82'))
BOOST_CAP     = float(os.environ.get('BOOST_CAP', '1.5'))
BOOST_K       = float(os.environ.get('BOOST_K', '0.6'))

LOOKBACK_HOURS    = int(os.environ.get('LOOKBACK_HOURS', '36'))
MAX_ARTICLES      = int(os.environ.get('MAX_ARTICLES', '1024'))
SCORE_CONCURRENCY = int(os.environ.get('SCORE_CONCURRENCY', '1'))
# Hard cap on simultaneous Ollama requests from this process (pipeline scoring,
# embeddings, TL;DRs, search, onboarding profile). 1 = one model job at a time,
# which is what a CPU-only host with ~10 GB RAM wants. Page fetches still run
# in parallel; only the model calls queue.
OLLAMA_MAX_PARALLEL = max(1, int(os.environ.get('OLLAMA_MAX_PARALLEL', '1')))

ACTIVE_START_HOUR = int(os.environ.get('ACTIVE_START_HOUR', '6'))
ACTIVE_END_HOUR   = int(os.environ.get('ACTIVE_END_HOUR', '22'))
INTERVAL_HOURS    = max(1, int(os.environ.get('INTERVAL_HOURS', '2')))

FEEDS_FILE  = os.environ.get('FEEDS_FILE', '/data/feeds.json')
DIGEST_FILE = os.environ.get('DIGEST_FILE', '/data/digest.json')
STATE_FILE  = os.environ.get('STATE_FILE', '/data/state.json')
RUNS_FILE   = os.environ.get('RUNS_FILE', '/data/runs.json')
MAX_RUNS    = int(os.environ.get('MAX_RUNS', '200'))  # bounded run-history log
# Article embeddings for the current digest, kept out of digest.json (which is
# served to the browser) so semantic search can rank without shipping vectors.
EMBEDDINGS_FILE  = os.environ.get('EMBEDDINGS_FILE', '/data/embeddings.json')
SEARCH_THRESHOLD = float(os.environ.get('SEARCH_THRESHOLD', '0.5'))  # min cosine to match

# Taste vector: how many of the most-recent saved/opened items to learn from,
# and how much more a "saved" item counts than an "opened" one.
TASTE_MAX_ITEMS  = int(os.environ.get('TASTE_MAX_ITEMS', '40'))
TASTE_SAVE_WEIGHT = float(os.environ.get('TASTE_SAVE_WEIGHT', '2.0'))

# Embedding cache (text-hash → vector) so feedback examples, saved/opened
# titles and carried stories are embedded once, not on every run.
EMBED_CACHE_FILE = os.environ.get('EMBED_CACHE_FILE', '/data/embed_cache.json')
EMBED_CACHE_MAX  = int(os.environ.get('EMBED_CACHE_MAX', '3000'))

# Explicit-feedback ranking (see apply_feedback). Similarities are calibrated
# linearly from [LO, HI] onto [0, 1] before weighting; tuned for
# nomic-embed-text, where unrelated news sits ~0.4-0.5 and same-topic ~0.7+.
FEEDBACK_SIM_LO   = float(os.environ.get('FEEDBACK_SIM_LO', '0.50'))
FEEDBACK_SIM_HI   = float(os.environ.get('FEEDBACK_SIM_HI', '0.80'))
FEEDBACK_TOP_K    = int(os.environ.get('FEEDBACK_TOP_K', '2'))
FEEDBACK_MAX_POS  = int(os.environ.get('FEEDBACK_MAX_POS', '120'))
FEEDBACK_MAX_NEG  = int(os.environ.get('FEEDBACK_MAX_NEG', '80'))
FEEDBACK_EMBED_BUDGET = int(os.environ.get('FEEDBACK_EMBED_BUDGET', '60'))  # new embeds per run
SOURCE_AFFINITY_CAP = 0.5
# Minimum seconds between manual (POST /refresh) runs.
REFRESH_COOLDOWN_S = int(os.environ.get('REFRESH_COOLDOWN_S', '60'))

FEED_TIMEOUT   = int(os.environ.get('FEED_TIMEOUT', '12'))
PAGE_TIMEOUT   = int(os.environ.get('PAGE_TIMEOUT', '8'))
OLLAMA_TIMEOUT = int(os.environ.get('OLLAMA_TIMEOUT', '120'))

# Categories (keys + the interest descriptions the scorer's prompt is built
# from) are configured in settings.load_categories(); the run loads them fresh.

USER_AGENT = 'Mozilla/5.0 Cruxwire (news aggregator)'

# Titles matching any of these are dropped (deal/ad spam).
TITLE_BLOCKLIST = re.compile('|'.join([
    r'save \$?\d+',
    r'\d+%\s*off',
    r'\$\d+\s*off',
    r'\bdeal(s)?\b.{0,40}\b(today|now|day|week|month|under|below)\b',
    r'\bbest\b.{0,30}\bdeals?\b',
    r'\bcoupon(s)?\b',
    r'\bpromo\s*code\b',
    r'\bprime day\b',
    r'\bblack friday\b',
    r'\bcyber monday\b',
    r'\bsale\b.{0,20}(ends|now|today)',
    r'\bgiveaway\b',
    r'\bsponsored\b',
]), re.IGNORECASE)

_run_lock = threading.Lock()
_runs_lock = threading.Lock()
_ollama_sem = threading.BoundedSemaphore(OLLAMA_MAX_PARALLEL)
_ollama_inflight = 0          # observability / tests: current concurrent model calls
_ollama_peak = 0
_ollama_count_lock = threading.Lock()
_stop = threading.Event()

# Live run status, polled by the UI via GET /status. Updated as run_once moves
# through its phases so the frontend can show an "Updating…" indicator and
# auto-surface a fresh digest when one lands.
_status = {
    'running': False,
    'phase': None,          # fetching | scoring | clustering | None
    'started_at': None,     # ISO, when the current/last run began
    'last_run_at': None,    # ISO, when the last run finished
    'last_ok': None,        # bool, did the last run write a digest
    'article_count': None,  # articles in the last successful digest
    'feed_count': None,     # feeds fetched in the current/last run
}
_status_lock = threading.Lock()


def _set_status(**kw):
    with _status_lock:
        _status.update(kw)


def get_status():
    with _status_lock:
        return dict(_status)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'[pipeline {ts}] {msg}', flush=True)


# ──────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────
FEED_LIMIT = int(os.environ.get('FEED_LIMIT', str(5 * 1024 * 1024)))


def http_get_text(url, timeout, limit=None):
    """GET a URL and return decoded text, or '' on any failure. Goes through
    netsafe: feed and article URLs are user/feed controlled, so private-network
    destinations, unsafe redirects and oversized bodies are refused."""
    return netsafe.get_text(url, timeout=timeout, limit=limit or FEED_LIMIT)


def ollama_post(path, payload, timeout):
    """POST to Ollama, holding the process-wide model-job semaphore so at most
    OLLAMA_MAX_PARALLEL model calls run at once (CPU hosts serialise anyway;
    queuing here keeps RAM flat and latency predictable)."""
    global _ollama_inflight, _ollama_peak
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        OLLAMA_HOST + path, data=data,
        headers={'Content-Type': 'application/json'}, method='POST')
    with _ollama_sem:
        with _ollama_count_lock:
            _ollama_inflight += 1
            _ollama_peak = max(_ollama_peak, _ollama_inflight)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        finally:
            with _ollama_count_lock:
                _ollama_inflight -= 1


def ollama_ps():
    """Models currently loaded in Ollama (GET /api/ps), or [] on failure. Used
    to tell whether the chat model is resident in VRAM (GPU) or pushed to CPU."""
    try:
        with urllib.request.urlopen(OLLAMA_HOST + '/api/ps', timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        models = data.get('models') if isinstance(data, dict) else None
        return models if isinstance(models, list) else []
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────
# Feed parsing (hand-rolled, CDATA-aware — same approach as v1)
# ──────────────────────────────────────────────────────────────────────
def clean_text(s):
    s = s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
         .replace('&quot;', '"').replace('&#39;', "'").replace('&apos;', "'")
    s = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s)
    return s.strip()


def extract_field(xml, tag):
    for pat in (
        rf'<{tag}[^>]*><!\[CDATA\[([\s\S]*?)\]\]></{tag}>',
        rf'<{tag}[^>]*>([\s\S]*?)</{tag}>',
    ):
        m = re.search(pat, xml, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ''


def parse_date(s):
    """Best-effort parse of RSS (RFC822) or Atom (ISO) dates -> aware datetime."""
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _gnews_publisher(block):
    """Google News items carry the real publisher in <source url=...>Name</source>."""
    m = re.search(r'<source\b[^>]*>([\s\S]*?)</source>', block, re.IGNORECASE)
    return images.clean_text(m.group(1)) if m else ''


def _item(block, title, url, pub, source, category, desc_raw):
    description = images.clean_text(desc_raw, 400)
    if images.is_google_news(url):
        publisher = _gnews_publisher(block)
        # Google News titles end in " - Publisher"; keep the headline clean.
        if publisher and title.endswith(' - ' + publisher):
            title = title[: -len(' - ' + publisher)].strip()
        if description and description.startswith(title):
            description = ''   # GN descriptions just repeat the headline
        if publisher:
            source_label = publisher
        else:
            source_label = source
    else:
        source_label = source
    return {'title': title, 'url': url, 'source': source, 'publisher': source_label,
            'category': category, 'published_at': pub.isoformat(),
            'description': description,
            'image': images.extract_rss_image(block, url)}


def parse_feed(xml, source, category, cutoff):
    """Parse RSS <item>s and Atom <entry>s. Each article carries a plain-text
    `description` (sanitised, <=400 chars) and an `image` from the item's media
    tags / enclosure / inline <img> (None if the feed has none)."""
    out = []

    # RSS <item> (also <item rdf:about=...> in RSS 1.0)
    for m in re.finditer(r'<item\b[^>]*>([\s\S]*?)</item>', xml, re.IGNORECASE):
        block = m.group(1)
        title = images.clean_text(extract_field(block, 'title'))
        url = clean_text(extract_field(block, 'link') or extract_field(block, 'guid')).strip()
        pub = parse_date(extract_field(block, 'pubDate')
                         or extract_field(block, 'dc:date')) or datetime.now(timezone.utc)
        if not title or not url.startswith('http'):
            continue
        if pub < cutoff:
            continue
        desc = extract_field(block, 'description') or extract_field(block, 'content:encoded')
        out.append(_item(block, title, url, pub, source, category, desc))

    # Atom <entry>
    for m in re.finditer(r'<entry\b[^>]*>([\s\S]*?)</entry>', xml, re.IGNORECASE):
        block = m.group(1)
        title = images.clean_text(extract_field(block, 'title'))
        lm = (re.search(r'<link[^>]*rel=["\']alternate["\'][^>]*href=["\']([^"\']+)["\']', block, re.IGNORECASE)
              or re.search(r'<link(?![^>]*rel=["\'](?:enclosure|self|replies)["\'])[^>]*href=["\']([^"\']+)["\']', block, re.IGNORECASE))
        url = clean_text(lm.group(1) if lm else extract_field(block, 'id')).strip()
        pub = parse_date(extract_field(block, 'published')
                         or extract_field(block, 'updated')) or datetime.now(timezone.utc)
        if not title or not url.startswith('http'):
            continue
        if pub < cutoff:
            continue
        desc = extract_field(block, 'summary') or extract_field(block, 'content')
        out.append(_item(block, title, url, pub, source, category, desc))

    return out


# Collision-resistant id from the URL (see article_ids for the migration).
stable_id = article_ids.stable_id


# ──────────────────────────────────────────────────────────────────────
# Scoring + embedding (Ollama)
# ──────────────────────────────────────────────────────────────────────
def build_system_prompt(categories):
    """The scorer's system prompt, built from the configured categories — the
    `interest` lines (in priority order) are what tune relevance to the reader."""
    interests = "\n".join(f"{i + 1}. {c['interest']}"
                          for i, c in enumerate(categories))
    return ("You are a relevance scorer for a personal news digest.\n\n"
            "Reader interests, roughly in priority order:\n"
            f"{interests}\n\n"
            "Scoring:\n"
            "8-10 = high relevance, reader would likely read in full\n"
            "5-7  = moderate relevance, worth a skim\n"
            "0-4  = low relevance or off-topic\n\n"
            "Rules:\n"
            "- Respond ONLY with valid JSON. No markdown fences, no commentary.\n"
            "- summary must be 1-2 sentences, max 180 characters, no em dashes.\n"
            "- category must be one of the exact strings listed in the schema.")


def build_neutral_prompt(categories):
    """Multi-user scorer prompt: one score per article that is NOT tied to any
    one reader (each user's interests are applied afterwards by embedding, see
    apply_interest), plus a category from the union of everyone's categories."""
    topics = "\n".join(f"- {c['key']}: {c['interest']}" for c in categories)
    return ("You are an editor rating stories for a general news digest.\n\n"
            "Rate how substantive and newsworthy the article is for a curious, "
            "well-informed reader, regardless of topic.\n\n"
            "Categories (key: description):\n"
            f"{topics}\n\n"
            "Scoring:\n"
            "8-10 = important or exceptionally informative, worth reading in full\n"
            "5-7  = solid, worth a skim\n"
            "0-4  = thin, promotional, clickbait or trivial\n\n"
            "Rules:\n"
            "- Respond ONLY with valid JSON. No markdown fences, no commentary.\n"
            "- summary must be 1-2 sentences, max 180 characters, no em dashes.\n"
            "- category must be one of the exact keys listed in the schema.")


# Per-user interest matching (multi-user mode): cosine similarity between an
# article and the user's category interest descriptions, calibrated from
# [LO, HI] onto [0, 1].
INTEREST_SIM_LO = float(os.environ.get('INTEREST_SIM_LO', '0.45'))
INTEREST_SIM_HI = float(os.environ.get('INTEREST_SIM_HI', '0.75'))


def apply_interest(articles, categories, embed_model, embed_cache, weight):
    """Multi-user relevance: blend the shared, reader-neutral LLM score with
    how well each article matches THIS user's category interests, and put it
    in one of the user's own categories. Cheap — one embedding per category
    (cached), dot products per article, no chat calls.

        score = (1 - weight) * base_score + weight * 10 * interest_match

    Category: the LLM's pick if the user has that category, else the category
    the user filed the source feed under, else the closest interest."""
    cat_vecs = []
    for c in categories:
        text = f"{c.get('label', '')}: {c.get('interest') or c.get('label') or c['key']}"
        v = embed_cache.embed(text, embed_model) if embed_cache else _embed(text, embed_model)
        n = _normalize(v) if v else None
        if n:
            cat_vecs.append((c['key'], n))
    keys = {c['key'] for c in categories}
    weight = max(0.0, min(1.0, weight))
    for a in articles:
        base = a.get('base_score', a.get('score', 0.0))
        a['base_score'] = base
        n = _normalize(a['embedding']) if a.get('embedding') else None
        best_key, best_sim = None, None
        if n and cat_vecs:
            best_key, best_sim = max(((k, sum(x * y for x, y in zip(n, v))) for k, v in cat_vecs),
                                     key=lambda kv: kv[1])
        llm_cat, hint = a.get('llm_category') or a.get('category'), a.get('_hint_cat')
        if llm_cat in keys:
            a['category'] = llm_cat
        elif hint in keys:
            a['category'] = hint
        elif best_key:
            a['category'] = best_key
        if best_sim is None:
            a['interest_match'] = None
            a['score'] = base
            continue
        match = calibrate(best_sim, INTEREST_SIM_LO, INTEREST_SIM_HI)
        a['interest_match'] = round(match, 3)
        a['score'] = round((1 - weight) * base + weight * 10.0 * match, 2)
    return articles


def _fetch_page(url):
    """Return (og_image_or_None, body_excerpt) for an article URL. Google News
    links are resolved to the publisher first where that can be done offline;
    og:image / twitter:image are resolved against the final page URL."""
    target = images.resolve_canonical(url)
    try:
        resp = netsafe.fetch(target, timeout=PAGE_TIMEOUT, limit=120000,
                             accept='text/html,application/xhtml+xml')
        html, final_url = resp.text(), resp.url
    except Exception:
        return None, ''
    if not html:
        return None, ''
    image = None if images.is_google_news(final_url) else images.extract_page_image(html, final_url)
    body = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.IGNORECASE)
    body = re.sub(r'<style[\s\S]*?</style>', ' ', body, flags=re.IGNORECASE)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = re.sub(r'\s+', ' ', body).strip()[:900]
    return image, body


def _score(article, excerpt, chat_model, system_prompt, valid_cats):
    """Ask Ollama for {score, summary, category}; fall back gracefully.

    `system_prompt` and `valid_cats` come from the configured categories.
    Returns (score, summary, category, ok, ms). `ok` is whether Ollama answered
    (False ⇒ the call errored/timed out and the score is a fallback); `ms` is
    the chat-call latency. Both feed the run-history health signals so a silent
    Ollama outage (many fallbacks) or a CPU fallback (high latency) is visible.
    """
    user_prompt = (
        "Score this article and return JSON only.\n\n"
        f"Title:    {article['title']}\n"
        f"Source:   {article['source']}\n"
        f"Hint cat: {article['category']}\n"
        f"Excerpt:  {excerpt or '(not available)'}\n\n"
        "Return this exact schema:\n"
        '{\n  "score": <number 0.0-10.0, one decimal place>,\n'
        '  "summary": "<1-2 sentences, max 180 chars, no em dashes>",\n'
        f'  "category": "<one of: {", ".join(valid_cats)}>"\n}}'
    )
    score, summary, category = 5.0, '', article['category']
    ok = False
    t0 = time.monotonic()
    try:
        resp = ollama_post('/api/chat', {
            'model': chat_model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'stream': False, 'format': 'json',
            # Disable reasoning models' "thinking" (e.g. qwen3): with format=json
            # it burns the num_predict budget on hidden reasoning before the JSON,
            # breaking the output and slowing runs. Harmless no-op on plain models.
            'think': False,
            'options': {'temperature': 0.1, 'num_predict': 250},
        }, OLLAMA_TIMEOUT)
        ok = True  # Ollama answered (parsing may still default the score)
        raw = (resp.get('message') or {}).get('content') or resp.get('response') or ''
        parsed = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
        try:
            score = round(min(10.0, max(0.0, float(parsed.get('score')))), 1)
        except (TypeError, ValueError):
            pass
        if isinstance(parsed.get('summary'), str):
            summary = parsed['summary'].replace('—', '--').strip()[:200]
        if parsed.get('category') in valid_cats:
            category = parsed['category']
    except Exception:
        pass
    ms = (time.monotonic() - t0) * 1000.0
    if not summary:
        summary = article['title'][:160]
    return score, summary, category, ok, ms


def _embed(text, embed_model=EMBED_MODEL):
    """Return an embedding vector for text, or None on failure."""
    try:
        resp = ollama_post('/api/embeddings',
                           {'model': embed_model, 'prompt': text}, OLLAMA_TIMEOUT)
        vec = resp.get('embedding')
        if isinstance(vec, list) and vec:
            return [float(x) for x in vec]
    except Exception:
        pass
    return None


def image_fields(article, page_image=None, page_kind='og', cache=True):
    """Image metadata for a digest article, following the resolution order
    RSS media → RSS <img> → publisher og:/twitter:image → local fallback.
    `fallback_image` is always set so the UI never shows a blank/broken box."""
    rss = images.normalize_image_url(article.get('image')) if article.get('image') else None
    topic = images.infer_topic(article.get('title', ''), article.get('description', ''),
                               article.get('category', ''))
    image = rss or page_image
    if image and cache:
        images.cache_put(article['url'], image, 'rss' if rss else page_kind)
    return {'image': image, 'image_kind': 'rss' if rss else ('og' if page_image else 'fallback'),
            'fallback_image': images.fallback_image(topic), 'topic': topic}


def enrich(article, chat_model, embed_model, system_prompt, valid_cats, embed_cache=None):
    """Fetch page, score/summarise, and embed one article."""
    t0 = time.monotonic()
    page_image, excerpt = _fetch_page(article['url'])
    if not excerpt and article.get('description'):
        excerpt = article['description'][:900]
    score, summary, category, score_ok, score_ms = _score(
        article, excerpt, chat_model, system_prompt, valid_cats)
    text = f"{article['title']}\n{summary}"
    embedding = embed_cache.embed(text, embed_model) if embed_cache else _embed(text, embed_model)
    out = {
        'id': article['id'],
        'title': article['title'],
        'url': article['url'],
        'source': article['source'],
        'publisher': article.get('publisher') or article['source'],
        'category': category,
        'score': score,
        'summary': summary,
        'description': article.get('description') or '',
        'published_at': article['published_at'],
        'embedding': embedding,
        '_score_ok': score_ok,           # transient: dropped before the digest is written
        '_score_ms': round(score_ms, 1),
        '_enrich_ms': round((time.monotonic() - t0) * 1000.0, 1),
    }
    out.update(image_fields({**article, 'category': category}, page_image))
    return out


TLDR_SYSTEM = """You write a TL;DR that helps a reader decide whether to open the \
full article. Be specific to THIS article — concrete facts, names, numbers, and \
claims, never generic filler. Each bullet is one short idea (max ~110 chars). The \
bottom line is one sentence on whether it's worth reading and for whom. Respond \
ONLY with valid JSON, no markdown."""


# Reading-time estimate. ~200 wpm is a common adult non-fiction average.
READ_WPM = 200
MIN_BODY_WORDS = 200   # below this we couldn't extract a real body — omit the estimate


def _article_word_count(html):
    """Best-effort word count of the article body for the reading-time estimate.

    Prefers schema.org JSON-LD (`wordCount`, else `articleBody` text): that
    structured data carries the real article even on JS-rendered pages where the
    body lives in a data blob rather than <p> tags (e.g. HackerNoon). Falls back
    to paragraph text for plain server-rendered pages, and returns 0 when neither
    yields a real body (so the caller omits the estimate)."""
    for ld in re.findall(r'<script[^>]*application/ld\+json[^>]*>([\s\S]*?)</script>', html, re.IGNORECASE):
        m = re.search(r'"wordCount"\s*:\s*(\d+)', ld)
        if m:
            return int(m.group(1))
        bm = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', ld)
        if bm:
            return len(re.sub(r'\\[a-z]', ' ', bm.group(1)).split())
    paras = re.findall(r'<p\b[^>]*>([\s\S]*?)</p>', html, re.IGNORECASE)
    prose = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', ' '.join(paras))).strip()
    return len(prose.split())


def _article_text(url, limit=6000):
    """Cleaned page text for the TL;DR prompt, plus a word count of the article
    body for the reading-time estimate. Returns (text, word_count): `text` is the
    whole cleaned body truncated to `limit` for prompting; `word_count` comes from
    structured data when available (see `_article_word_count`)."""
    html = http_get_text(url, PAGE_TIMEOUT, limit=300000)
    if not html:
        return '', 0
    word_count = _article_word_count(html)
    body = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.IGNORECASE)
    body = re.sub(r'<style[\s\S]*?</style>', ' ', body, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body)).strip()[:limit]
    return text, word_count


def generate_tldr(url, title, chat_model=None):
    """Ollama TL;DR for a saved article: {tldr: [bullets], bottom_line: str}.
    Returns None on failure (Ollama down / nothing usable) so the caller can
    surface a retry."""
    chat_model = chat_model or settings.load()['ollama_model']
    text, word_count = _article_text(url)
    user_prompt = (
        "Write a TL;DR for someone deciding whether to read this article.\n\n"
        f"Title: {title}\n"
        f"Article text: {text or '(could not fetch the article body — use the title)'}\n\n"
        "Return this exact schema:\n"
        '{\n  "tldr": ["3-5 short bullet points — the key takeaways"],\n'
        '  "bottom_line": "<one sentence: is it worth reading, and for whom>"\n}'
    )
    try:
        resp = ollama_post('/api/chat', {
            'model': chat_model,
            'messages': [
                {'role': 'system', 'content': TLDR_SYSTEM},
                {'role': 'user', 'content': user_prompt},
            ],
            'stream': False, 'format': 'json',
            'think': False,   # see _score: keep reasoning models from eating the budget
            'options': {'temperature': 0.2, 'num_predict': 400},
        }, OLLAMA_TIMEOUT)
        raw = (resp.get('message') or {}).get('content') or resp.get('response') or ''
        parsed = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
    except Exception as exc:
        log(f'tldr failed for {url}: {exc}')
        return None
    bullets = parsed.get('tldr')
    bullets = ([str(b).strip()[:200] for b in bullets if str(b).strip()][:6]
               if isinstance(bullets, list) else [])
    bottom = parsed.get('bottom_line')
    bottom = str(bottom).strip()[:300] if isinstance(bottom, str) else ''
    if not bullets and not bottom:
        return None
    result = {'tldr': bullets, 'bottom_line': bottom}
    # Reading-time estimate from the article's word count. Below the floor we
    # couldn't extract a real body (paywall/failed fetch), so omit rather than
    # show a figure built from page chrome.
    if word_count >= MIN_BODY_WORDS:
        result['read_minutes'] = max(1, round(word_count / READ_WPM))
    return result


# ──────────────────────────────────────────────────────────────────────
# Clustering
# ──────────────────────────────────────────────────────────────────────
def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return None
    return [x / norm for x in vec]


# ──────────────────────────────────────────────────────────────────────
# Personalization + semantic blocking (both reuse article embeddings)
# ──────────────────────────────────────────────────────────────────────
def _embed_many(texts, embed_model, workers=4):
    """Embed a list of texts concurrently; returns vectors (or None per item)."""
    if not texts:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(texts))) as ex:
        return list(ex.map(lambda t: _embed(t, embed_model), texts))


def _load_user_state():
    try:
        with open(users.path(STATE_FILE), 'r', encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_embeddings_index():
    """{article_id: vector} for the current digest, or {} if none yet."""
    try:
        with open(users.path(EMBEDDINGS_FILE), 'r', encoding='utf-8') as fh:
            d = json.load(fh)
        vecs = d.get('vectors') if isinstance(d, dict) else None
        return vecs if isinstance(vecs, dict) else {}
    except Exception:
        return {}


def semantic_search(query, threshold=SEARCH_THRESHOLD, limit=80):
    """Rank the current digest's articles by cosine similarity of their stored
    embedding to the query's embedding. Returns [{'id', 'score'}] descending.

    Embeds the query with the same model and the same plain title+summary
    convention the pipeline used, so the query lands in the same vector space.
    """
    query = (query or '').strip()
    if not query:
        return []
    model = settings.load().get('embed_model') or EMBED_MODEL
    qn = _normalize(_embed(query, model) or [])
    if not qn:
        return []
    results = []
    for aid, vec in _load_embeddings_index().items():
        n = _normalize(vec) if vec else None
        if not n:
            continue
        sim = sum(x * y for x, y in zip(qn, n))
        if sim >= threshold:
            results.append({'id': aid, 'score': round(sim, 4)})
    results.sort(key=lambda r: r['score'], reverse=True)
    return results[:limit]


def semantic_block(articles, topics, threshold, embed_model):
    """Drop articles whose embedding is too close to any 'topic to avoid'
    phrase. Keyword/regex blocking already ran earlier; this is the semantic
    layer. Articles without an embedding are kept (can't judge them)."""
    topics = [t for t in (topics or []) if isinstance(t, str) and t.strip()]
    if not topics:
        return articles
    topic_norms = [n for n in (_normalize(v) for v in _embed_many(topics, embed_model)) if n]
    if not topic_norms:
        return articles
    kept, dropped = [], 0
    for a in articles:
        n = _normalize(a['embedding']) if a.get('embedding') else None
        if n is not None and max(sum(x * y for x, y in zip(n, tn)) for tn in topic_norms) > threshold:
            dropped += 1
            continue
        kept.append(a)
    if dropped:
        log(f'semantic blocklist dropped {dropped} article(s)')
    return kept


class EmbedCache:
    """Bounded text→vector cache persisted to EMBED_CACHE_FILE. Keyed on a hash
    of (model, text), so switching the embedding model never mixes spaces.
    Vectors are rounded to 5 dp to keep the file small."""

    def __init__(self, path=None, max_items=None):
        self.path = path or EMBED_CACHE_FILE
        self.max_items = max_items or EMBED_CACHE_MAX
        self._lock = threading.Lock()
        self._d = OrderedDict()
        self._dirty = False
        self.hits = self.misses = 0
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
            if isinstance(raw, dict) and isinstance(raw.get('vectors'), dict):
                for k, v in raw['vectors'].items():
                    if isinstance(v, list) and v:
                        self._d[k] = v
        except Exception:
            pass

    @staticmethod
    def key(text, model):
        return hashlib.sha1(f'{model}\n{text}'.encode('utf-8')).hexdigest()[:24]

    def get(self, text, model):
        k = self.key(text, model)
        with self._lock:
            v = self._d.get(k)
            if v is not None:
                self._d.move_to_end(k)
                self.hits += 1
            return v

    def put(self, text, model, vec):
        if not vec:
            return
        k = self.key(text, model)
        with self._lock:
            self._d[k] = [round(float(x), 5) for x in vec]
            self._d.move_to_end(k)
            while len(self._d) > self.max_items:
                self._d.popitem(last=False)
            self._dirty = True

    def embed(self, text, model, allow_compute=True):
        v = self.get(text, model)
        if v is not None or not allow_compute:
            return v
        self.misses += 1
        v = _embed(text, model)
        if v:
            self.put(text, model, v)
        return v

    def save(self):
        with self._lock:
            if not self._dirty:
                return
            data = {'vectors': dict(self._d)}
            self._dirty = False
        try:
            _atomic_write_json(self.path, data)
        except Exception as exc:
            log(f'embed cache save failed: {exc}')


def apply_taste(articles, embed_model, weight, embed_cache=None):
    """Annotate each article with a `taste_boost` from how close it is to the
    user's taste — the weighted centroid of embeddings of titles they've saved
    (weighted higher) and opened. weight is the max points an on-taste article
    can gain. No history yet (or weight 0) → every boost is 0. Implicit signal:
    deliberately weaker than explicit likes (see apply_feedback)."""
    for a in articles:
        a['taste_boost'] = 0.0
    if weight <= 0:
        return articles

    state = _load_user_state()
    later = state.get('later') if isinstance(state.get('later'), list) else []
    history = state.get('history') if isinstance(state.get('history'), list) else []
    samples = []  # (title, weight, recency_key)
    for it in later:
        if isinstance(it, dict) and it.get('title'):
            samples.append((it['title'], TASTE_SAVE_WEIGHT, it.get('savedAt') or ''))
    for it in history:
        if isinstance(it, dict) and it.get('title'):
            samples.append((it['title'], 1.0, it.get('viewedAt') or ''))
    if not samples:
        return articles
    samples.sort(key=lambda s: s[2], reverse=True)   # most recent first
    samples = samples[:TASTE_MAX_ITEMS]

    if embed_cache is not None:
        embs = [embed_cache.embed(s[0], embed_model) for s in samples]
    else:
        embs = _embed_many([s[0] for s in samples], embed_model)
    acc, wsum = None, 0.0
    for (_, w, _), emb in zip(samples, embs):
        n = _normalize(emb) if emb else None
        if n is None:
            continue
        if acc is None:
            acc = [0.0] * len(n)
        for i in range(len(n)):
            acc[i] += w * n[i]
        wsum += w
    centroid = _normalize(acc) if acc and wsum else None
    if centroid is None:
        return articles

    n_boosted = 0
    for a in articles:
        n = _normalize(a['embedding']) if a.get('embedding') else None
        if n is None:
            continue
        sim = sum(x * y for x, y in zip(n, centroid))
        if sim > 0:
            a['taste_boost'] = round(weight * sim, 3)
            n_boosted += 1
    log(f'taste vector from {len(samples)} items; boosted {n_boosted} article(s)')
    return articles


# ──────────────────────────────────────────────────────────────────────
# Explicit feedback (likes / dislikes) → ranking
# ──────────────────────────────────────────────────────────────────────
def feedback_text(entry):
    """The text embedded for a feedback example — mirrors the article
    convention (title + summary/description)."""
    return f"{entry.get('title', '')}\n{entry.get('description', '')}".strip()


def calibrate(sim, lo=None, hi=None):
    lo = FEEDBACK_SIM_LO if lo is None else lo
    hi = FEEDBACK_SIM_HI if hi is None else hi
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (sim - lo) / (hi - lo)))


def _topk_mean(sims, k):
    if not sims:
        return 0.0, -1
    order = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)[:max(1, k)]
    return sum(sims[i] for i in order) / len(order), order[0]


def load_feedback_examples(embed_model, embed_cache, budget=None):
    """(positives, negatives): lists of {title, source, vec(normalised)} for the
    most recent explicit votes. Embeddings come from the cache; at most
    `budget` missing ones are computed this call (rest on later runs)."""
    try:
        import preferences
        fb = preferences.feedback()
    except Exception as exc:
        log(f'feedback unavailable: {exc}')
        return [], []
    budget = FEEDBACK_EMBED_BUDGET if budget is None else budget
    entries = sorted(fb.values(), key=lambda e: e.get('updatedAt') or '', reverse=True)
    pos_e = [e for e in entries if e.get('vote') == 1][:FEEDBACK_MAX_POS]
    neg_e = [e for e in entries if e.get('vote') == -1][:FEEDBACK_MAX_NEG]
    computed = 0

    def vecs(lst):
        nonlocal computed
        out = []
        for e in lst:
            text = feedback_text(e)
            if not text:
                continue
            v = embed_cache.embed(text, embed_model, allow_compute=False) if embed_cache else None
            if v is None and computed < budget:
                computed += 1
                v = embed_cache.embed(text, embed_model) if embed_cache else _embed(text, embed_model)
            n = _normalize(v) if v else None
            if n:
                out.append({'title': e.get('title', ''), 'source': e.get('source', ''), 'vec': n})
        return out

    return vecs(pos_e), vecs(neg_e)


def source_affinity(weight):
    """Publisher affinity from explicit votes, kept SEPARATE from topic
    similarity: {publisher: boost in [-cap, cap]}. A smoothed like/dislike
    balance, so a single like barely moves a publisher."""
    try:
        import preferences
        fb = preferences.feedback()
    except Exception:
        return {}
    counts = {}
    for e in fb.values():
        src = (e.get('source') or '').strip()
        if not src:
            continue
        c = counts.setdefault(src, [0, 0])
        c[0 if e.get('vote') == 1 else 1] += 1
    out = {}
    for src, (likes, dislikes) in counts.items():
        v = weight * (likes - dislikes) / (likes + dislikes + 2)
        out[src] = round(max(-SOURCE_AFFINITY_CAP, min(SOURCE_AFFINITY_CAP, v)), 3)
    return out


def apply_feedback(articles, positives, negatives, like_w, dislike_w, cap,
                   source_weight=0.0, affinity=None):
    """Annotate articles with `feedback_boost`, `source_boost`, `explore`,
    and an optional `why` (nearest liked example's title).

    Scores each article against MULTIPLE examples (mean of the top-k cosine
    similarities to liked items, and separately to disliked items) instead of
    one averaged taste vector, so several distinct interests all keep pulling
    their own stories up. Similarities are calibrated, weighted, and the total
    is capped to ±cap so base relevance / freshness / coverage still matter.
    """
    affinity = affinity if affinity is not None else (source_affinity(source_weight) if source_weight else {})
    n_pos = n_neg = 0
    for a in articles:
        a['feedback_boost'] = 0.0
        a['source_boost'] = affinity.get(a.get('publisher') or a.get('source'), 0.0) if affinity else 0.0
        a['explore'] = False
        a.pop('why', None)
        n = _normalize(a['embedding']) if a.get('embedding') else None
        if n is None:
            a['explore'] = bool(positives)
            continue
        ps = [sum(x * y for x, y in zip(n, p['vec'])) for p in positives]
        ns = [sum(x * y for x, y in zip(n, q['vec'])) for q in negatives]
        pos_sim, best = _topk_mean(ps, FEEDBACK_TOP_K)
        neg_sim, _ = _topk_mean(ns, FEEDBACK_TOP_K)
        pc, nc = (calibrate(pos_sim) if ps else 0.0), (calibrate(neg_sim) if ns else 0.0)
        boost = like_w * pc - dislike_w * nc
        boost = max(-cap, min(cap, boost))
        a['feedback_boost'] = round(boost, 3)
        if boost > 0:
            n_pos += 1
        elif boost < 0:
            n_neg += 1
        # Cheap "Why this story?" — the single nearest liked example.
        if best >= 0 and calibrate(ps[best]) >= 0.5:
            a['why'] = positives[best]['title'][:140]
        # Exploration candidates: not already matched by likes, not close to
        # dislikes. Diversify() mixes a share of these into the top of the feed.
        a['explore'] = bool(positives) and pc < 0.25 and nc < 0.5
    if positives or negatives:
        log(f'feedback: {len(positives)} likes / {len(negatives)} dislikes; '
            f'boosted {n_pos}, demoted {n_neg}')
    return articles


def rank_score(a):
    """The composite the digest is ordered by (the client multiplies in its own
    per-source open/save/dismiss affinity on top)."""
    return (a.get('score', 0.0) + a.get('cluster_boost', 0.0) + a.get('taste_boost', 0.0)
            + a.get('feedback_boost', 0.0) + a.get('source_boost', 0.0))


TOPIC_REPEAT_PENALTY = 0.6    # per earlier same-topic pick
SOURCE_REPEAT_PENALTY = 0.4   # per earlier same-publisher pick
TOP_WINDOW = 10
MAX_PER_TOPIC_TOP = 4         # in the first TOP_WINDOW, if alternatives exist


def diversify(items, key=rank_score, explore_share=0.25, topic_of=None, source_of=None):
    """Greedy re-ranking for the feed: best composite first, but each repeat of
    a topic / publisher costs a penalty, a single topic can hold at most
    MAX_PER_TOPIC_TOP of the first TOP_WINDOW slots, and roughly
    `explore_share` of slots go to exploration candidates (a['explore']) so the
    feed doesn't collapse onto what you already liked. Pure function."""
    topic_of = topic_of or (lambda a: a.get('category') or a.get('topic') or '')
    source_of = source_of or (lambda a: a.get('publisher') or a.get('source') or '')
    remaining = sorted(items, key=key, reverse=True)
    period = int(round(1.0 / explore_share)) if explore_share and explore_share > 0 else 0
    have_explore = any(a.get('explore') for a in remaining)
    out, tcount, scount = [], {}, {}
    while remaining:
        pos = len(out)
        pool = remaining
        if period and have_explore and pos % period == period - 1:
            ex = [a for a in remaining if a.get('explore')]
            if ex:
                pool = ex
        if pos < TOP_WINDOW:
            capped = [a for a in pool if tcount.get(topic_of(a), 0) < MAX_PER_TOPIC_TOP]
            if capped:
                pool = capped

        def adj(a):
            return (key(a) - TOPIC_REPEAT_PENALTY * tcount.get(topic_of(a), 0)
                    - SOURCE_REPEAT_PENALTY * scount.get(source_of(a), 0))
        best = max(pool, key=adj)
        remaining.remove(best)
        out.append(best)
        t, s_ = topic_of(best), source_of(best)
        tcount[t] = tcount.get(t, 0) + 1
        scount[s_] = scount.get(s_, 0) + 1
    return out


def cluster(articles, sim_threshold=SIM_THRESHOLD, boost_cap=BOOST_CAP, boost_k=BOOST_K):
    """Assign cluster_id/size/boost/rep.

    Each story is anchored on its highest-scoring article; another article joins
    that story only if its cosine similarity to the anchor exceeds sim_threshold
    (and, if it is close to several anchors, the nearest one wins). Anchoring on
    the representative -- rather than single-link union-find -- avoids transitive
    "chaining", where A~B and B~C silently merge A and C even when A and C are
    unrelated. That chaining is what collapses a pile of loosely-related articles
    into one bogus mega-cluster. Articles without an embedding stay singletons."""
    n = len(articles)
    norms = [_normalize(a['embedding']) if a.get('embedding') else None
             for a in articles]

    def cosine(u, v):
        return sum(x * y for x, y in zip(u, v))

    # Seed clusters from the highest-scoring articles first, so the strongest
    # article anchors each story; weaker articles attach to the best-matching
    # existing anchor or start their own.
    order = sorted((i for i in range(n) if norms[i] is not None),
                   key=lambda i: articles[i]['score'], reverse=True)
    anchors = []          # anchor indices, in creation (descending-score) order
    members = {}          # anchor index -> list of member indices
    for i in order:
        best_anchor, best_sim = None, sim_threshold
        for a in anchors:
            s = cosine(norms[i], norms[a])
            if s > best_sim:
                best_anchor, best_sim = a, s
        if best_anchor is None:
            anchors.append(i)
            members[i] = [i]
        else:
            members[best_anchor].append(i)

    groups = list(members.values())
    # Articles without an embedding cannot be matched; each is its own story.
    groups.extend([i] for i in range(n) if norms[i] is None)

    for grp in groups:
        size = len(grp)
        # Representative: highest score, tie-break has-image.
        rep = max(grp, key=lambda i: (
            articles[i]['score'], 1 if articles[i].get('image') else 0))
        cid = articles[rep]['id']
        boost = min(boost_cap, boost_k * math.log2(size)) if size > 1 else 0.0
        for i in grp:
            articles[i]['cluster_id'] = cid
            articles[i]['cluster_size'] = size
            articles[i]['cluster_rep'] = (i == rep)
            articles[i]['cluster_boost'] = round(boost, 3) if i == rep else 0.0
    return articles


# ──────────────────────────────────────────────────────────────────────
# Carry-forward + retention
#
# The digest is no longer a fresh 36h snapshot. Unread stories are carried
# forward from the previous digest so a story you never got to doesn't vanish
# just because its source feed rotated it out. The pool is then pruned by a
# rank-weighted lifespan held inside a [floor, ceiling] band: weak stories die
# fast, strong ones linger, the inbox never goes dry or overflows, and a hard
# age cap overrides everything so nothing stale lingers. Read stories are
# dropped (vacated) — History keeps its own self-contained copy, untouched.
# ──────────────────────────────────────────────────────────────────────
def _age_hours(published_at, now=None):
    """Hours since an ISO `published_at`, or None if unparseable."""
    if not isinstance(published_at, str) or not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at)
    except ValueError:
        dt = parse_date(published_at)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def _load_prev_digest_articles():
    """Articles from the currently-stored digest (for carry-forward), or []."""
    try:
        with open(users.path(DIGEST_FILE), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return []
    arts = data.get('articles') if isinstance(data, dict) else None
    return arts if isinstance(arts, list) else []


def _load_read_ids():
    """The user's read article ids — so carry-forward can vacate read stories."""
    raw = _load_user_state().get('readIds')
    return {r for r in raw if isinstance(r, str)} if isinstance(raw, list) else set()


def _reembed(article, embed_model, index=None, embed_cache=None):
    """Give a carried/reused article its embedding so it can re-cluster against
    fresh coverage. Its Ollama score/summary are reused (the expensive, less
    stable part). The vector comes from the stored index (same model) or the
    embed cache when possible — only a genuinely missing one is recomputed."""
    vec = (index or {}).get(article.get('id'))
    if not vec:
        text = f"{article.get('title', '')}\n{article.get('summary', '')}"
        vec = embed_cache.embed(text, embed_model) if embed_cache else _embed(text, embed_model)
    article['embedding'] = vec
    if not article.get('fallback_image'):
        article.update(image_fields(article, article.get('image'), 'og'))
    return article


def select_new_for_scoring(new_articles, budget):
    """Pick at most `budget` never-scored articles, newest first but
    round-robin across sources so one prolific feed can't eat the whole CPU
    budget. Returns (selected, deferred_count)."""
    if budget is None or budget <= 0 or len(new_articles) <= budget:
        return list(new_articles), 0
    by_src = OrderedDict()
    for a in sorted(new_articles, key=lambda a: a['published_at'], reverse=True):
        by_src.setdefault(a.get('source', ''), []).append(a)
    queues = list(by_src.values())
    picked = []
    while len(picked) < budget and any(queues):
        for q in queues:
            if q and len(picked) < budget:
                picked.append(q.pop(0))
    return picked, len(new_articles) - len(picked)


def apply_retention(scored, cfg, now=None):
    """Prune the scored pool to a rank-weighted, volume-banded keep set.

    Operates on *stories* (clusters), not raw articles: each story lives for a
    TTL that scales with its rank between [base_ttl, max_ttl], clamped into a
    [floor, ceiling] band so the inbox never goes dry or overflows, with a hard
    age cap that overrides everything. A story's age is its freshest coverage.
    Returns the kept articles, preserving input order.
    """
    now = now or datetime.now(timezone.utc)
    ceiling = max(1, cfg['retain_ceiling'])
    floor = min(cfg['retain_floor'], ceiling)
    base_ttl = cfg['retain_base_ttl_hours']
    max_ttl = max(base_ttl, cfg['retain_max_ttl_hours'])
    hard_max = cfg['retain_hard_max_age_hours']

    boosted = rank_score

    # Group articles into stories.
    groups = {}
    for a in scored:
        groups.setdefault(a.get('cluster_id', a['id']), []).append(a)

    clusters = []
    for cid, members in groups.items():
        ages = [h for h in (_age_hours(m.get('published_at'), now) for m in members)
                if h is not None]
        clusters.append({
            'cid': cid,
            'age': min(ages) if ages else None,        # freshest coverage = story age
            'rank': max(boosted(m) for m in members),
        })

    # Hard age cap first: drop outright (None age = can't judge, so keep).
    live = [c for c in clusters if c['age'] is None or c['age'] < hard_max]
    live.sort(key=lambda c: c['rank'], reverse=True)   # best first
    m = len(live)

    kept, expired = set(), []
    for i, c in enumerate(live):
        pct = (m - 1 - i) / (m - 1) if m > 1 else 1.0  # 1.0 best … 0.0 worst
        ttl = base_ttl + (max_ttl - base_ttl) * pct
        if c['age'] is None or c['age'] <= ttl:
            kept.add(c['cid'])
        else:
            expired.append(c)                          # stays best-first

    # Ceiling: keep only the top-ranked stories (live is best-first).
    if len(kept) > ceiling:
        kept = set([c['cid'] for c in live if c['cid'] in kept][:ceiling])

    # Floor: backfill the best expired-but-in-window stories until we hit it.
    for c in expired:
        if len(kept) >= floor:
            break
        kept.add(c['cid'])

    return [a for a in scored if a.get('cluster_id', a['id']) in kept]


# ──────────────────────────────────────────────────────────────────────
# Digest assembly
# ──────────────────────────────────────────────────────────────────────
def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    d = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.digest-', suffix='.json', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────
# Run history — a bounded log of completed runs, served at GET /runs so the
# UI can show recent executions and surface failures.
# ──────────────────────────────────────────────────────────────────────
def get_runs():
    """The recorded run log (oldest first), or [] if none/unreadable."""
    try:
        with open(RUNS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _record_run(rec):
    """Append one completed run to the log, capped to MAX_RUNS (newest kept)."""
    with _runs_lock:
        runs = get_runs()
        runs.append(rec)
        try:
            _atomic_write_json(RUNS_FILE, runs[-MAX_RUNS:])
        except Exception as exc:
            log(f'failed to record run: {exc}')


def load_feeds():
    try:
        with open(users.path(FEEDS_FILE), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _user_digest_inputs(user):
    """Per-user inputs the shared phase needs: feeds, read ids, keyword
    blocklist, previous digest + embedding index."""
    with users.as_user(user):
        cfg = settings.load()
        feeds = [f for f in load_feeds() if isinstance(f, dict) and f.get('url')]
        return {
            'user': user,
            'feeds': feeds,
            'feed_urls': {f['url'] for f in feeds},
            'feed_cats': {f['url']: f.get('category') or '' for f in feeds},
            'read_ids': _load_read_ids(),
            'blocklist': [k.lower() for k in cfg['block_keywords']],
            'prev': [a for a in _load_prev_digest_articles() if isinstance(a, dict) and a.get('id')],
            'emb_index': _load_embeddings_index(),
        }


def _blocked_for(title, blocklist):
    return bool(TITLE_BLOCKLIST.search(title) or settings.is_blocked(title, blocklist))


def build_user_digest(inp, fresh, pool_by_id, embed_model, embed_cache, multi):
    """Personalize, cluster and retain one user's digest from the shared,
    already-scored article pool, and write it to the user's own files.
    Must run inside users.as_user(inp['user']). Returns (articles, clusters)."""
    cfg = settings.load()
    read_ids, blocklist = inp['read_ids'], inp['blocklist']
    hard_max = cfg['retain_hard_max_age_hours']

    # This user's fresh articles: from feeds they follow, unread, not blocked,
    # and scored (an article deferred by the CPU budget waits for a later run).
    mine, mine_ids = [], set()
    for a in fresh:
        if a['id'] in mine_ids or a['id'] in read_ids or not (a['_feeds'] & inp['feed_urls']):
            continue
        scored = pool_by_id.get(a['id'])
        if scored is not None and scored.get('url') != a['url']:
            scored = None       # id collision: never serve another story's record
        if scored is None or _blocked_for(scored.get('title', ''), blocklist):
            continue
        art = dict(scored)
        art['_hint_cat'] = next((inp['feed_cats'][u] for u in a['_feeds'] if u in inp['feed_cats']), '')
        mine.append(art)
        mine_ids.add(a['id'])
    # Carry forward this user's unread stories from their previous digest.
    carried = 0
    for prev in inp['prev']:
        aid = prev['id']
        if aid in read_ids or aid in mine_ids or _blocked_for(prev.get('title', ''), blocklist):
            continue
        age = _age_hours(prev.get('published_at'))
        if age is not None and age >= hard_max:
            continue
        pooled = pool_by_id.get(aid)
        art = dict(pooled if pooled is not None and pooled.get('url') == prev.get('url') else prev)
        mine.append(art)
        mine_ids.add(aid)
        carried += 1
    if carried:
        log(f'carrying forward {carried} unread stor{"y" if carried == 1 else "ies"}')

    if multi:
        apply_interest(mine, settings.load_categories(), embed_model, embed_cache,
                       cfg['interest_weight'])
    for a in mine:
        a.pop('_hint_cat', None)

    # Semantic blocklist, then personalization: implicit taste (saves/opens),
    # explicit likes / dislikes against multiple examples, publisher affinity.
    mine = semantic_block(mine, cfg['block_topics'], cfg['block_topic_threshold'], embed_model)
    apply_taste(mine, embed_model, cfg['taste_weight'], embed_cache)
    positives, negatives = load_feedback_examples(embed_model, embed_cache)
    apply_feedback(mine, positives, negatives,
                   cfg['feedback_like_weight'], cfg['feedback_dislike_weight'],
                   cfg['feedback_cap'], cfg['source_affinity_weight'])

    mine = cluster(mine, cfg['sim_threshold'], cfg['boost_cap'], cfg['boost_k'])
    # Keep each article's embedding (keyed by id) before stripping it from the
    # digest — semantic search ranks against these.
    emb_index = {a['id']: a['embedding'] for a in mine if a.get('embedding')}
    for a in mine:
        for k in ('embedding', '_score_ok', '_score_ms', '_enrich_ms'):
            a.pop(k, None)
    mine.sort(key=rank_score, reverse=True)

    pool_size = len(mine)
    mine = apply_retention(mine, cfg)
    if len(mine) != pool_size:
        log(f'retention kept {len(mine)}/{pool_size} articles')

    now = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(users.path(DIGEST_FILE), {
        'generated_at': now, 'article_count': len(mine), 'articles': mine})
    kept_ids = {a['id'] for a in mine}
    _atomic_write_json(users.path(EMBEDDINGS_FILE), {
        'generated_at': now, 'model': embed_model,
        'vectors': {i: v for i, v in emb_index.items() if i in kept_ids}})
    corpus_archive.archive_articles(mine, emb_index, embed_model, cfg, log=log)
    return mine, len({a['cluster_id'] for a in mine})


def run_once():
    """One full pipeline pass. No-op (returns False) if one is already running.

    Shared phase (once, whatever the number of users): fetch the union of every
    user's feeds, then score/summarise/embed each never-seen article ONCE.
    Per-user phase: build each user's digest from that pool — their feeds,
    read state, blocklists, categories, likes and taste — with embeddings only,
    no extra LLM calls.
    """
    if not _run_lock.acquire(blocking=False):
        log('run already in progress; skipping')
        return False
    started = _now_iso()
    _set_status(running=True, phase='fetching', started_at=started, feed_count=None)
    ok = False
    feed_count = new_count = article_count = clusters = 0
    scored_count = score_fallbacks = embed_fallbacks = 0
    reused_count = deferred_count = 0
    avg_score_ms = avg_enrich_ms = None
    gpu_ratio = None
    error = None
    user_count = 0
    try:
        multi = users.enabled()
        with users.as_user(users.OWNER_PSEUDO):
            cfg = settings.load()           # global keys (models, ingestion)
        inputs = [_user_digest_inputs(u) for u in users.active_users()]
        user_count = len(inputs)
        feeds = OrderedDict()
        for inp in inputs:
            for f in inp['feeds']:
                feeds.setdefault(f['url'], f)
        if not feeds:
            log('no feeds configured; nothing to do')
            return False
        feed_count = len(feeds)
        log(f'fetching {feed_count} feeds for {user_count} user(s)')
        _set_status(feed_count=feed_count)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg['lookback_hours'])

        # Fetch each distinct feed once, concurrently.
        def fetch(feed):
            return http_get_text(feed.get('url', ''), FEED_TIMEOUT), feed

        bodies = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(feeds))) as ex:
            for body, feed in ex.map(fetch, feeds.values()):
                if body:
                    bodies.append((body, feed))

        # Parse + dedup. Each article remembers every feed it came from, so it
        # reaches every user following any of them.
        by_url = {}
        for body, feed in bodies:
            for art in parse_feed(body, feed.get('source', ''),
                                  feed.get('category', 'tech'), cutoff):
                if art['url'] in by_url:
                    by_url[art['url']]['_feeds'].add(feed['url'])
                    continue
                if TITLE_BLOCKLIST.search(art['title']):
                    continue
                art['id'] = stable_id(art['url'])
                art['_feeds'] = {feed['url']}
                by_url[art['url']] = art

        # Keep only articles some subscriber still wants (not read, not on
        # their keyword blocklist) — no LLM budget for stories nobody will see.
        def wanted(a):
            return any(a['_feeds'] & inp['feed_urls'] and a['id'] not in inp['read_ids']
                       and not settings.is_blocked(a['title'], inp['blocklist'])
                       for inp in inputs)

        fresh = [a for a in by_url.values() if wanted(a)]
        fresh.sort(key=lambda a: a['published_at'], reverse=True)
        fresh = fresh[:cfg['max_articles']]

        # CPU budget: an article already scored in a previous run (in anyone's
        # digest) is REUSED — score/summary/image kept, embedding taken from the
        # stored index — so only genuinely new articles hit the chat model, and
        # at most `max_new_scored` of those per run (the rest wait).
        prev_by_id, emb_index_prev = {}, {}
        for inp in inputs:
            for a in inp['prev']:
                prev_by_id.setdefault(a['id'], a)
            for k, v in inp['emb_index'].items():
                emb_index_prev.setdefault(k, v)
        fresh_ids = {a['id'] for a in fresh}
        new_articles, reused = [], []
        for a in fresh:
            # Reuse a pooled record only when it is the same URL — an id match
            # alone must never hand one story's record to another link.
            prev = prev_by_id.get(a['id'])
            if prev is not None and prev.get('url') != a['url']:
                prev = None
            (reused if prev is not None else new_articles).append(a)
        to_score, deferred_count = select_new_for_scoring(new_articles, cfg['max_new_scored'])
        carried_ids = {a['id'] for inp in inputs for a in inp['prev']
                       if a['id'] not in inp['read_ids']} - fresh_ids
        log(f'parsed {len(fresh)} fresh articles: {len(new_articles)} new, '
            f'{len(reused)} already scored; scoring {len(to_score)} '
            f'(budget {cfg["max_new_scored"]}, deferred {deferred_count})')
        if not to_score and not carried_ids and not reused:
            return False
        new_count = len(new_articles)

        # Score + embed, using the admin-selected models. New articles get a
        # full enrich; reused/carried ones only need an embedding (usually cached).
        # Single-user: the scorer prompt carries the reader's interests, as
        # before. Multi-user: one neutral score per article; each user's
        # interests are applied afterwards by embedding (apply_interest).
        _set_status(phase='scoring')
        chat_model, embed_model = cfg['ollama_model'], cfg['embed_model']
        if multi:
            all_cats = OrderedDict()
            for inp in inputs:
                with users.as_user(inp['user']):
                    for c in settings.load_categories():
                        all_cats.setdefault(c['key'], c)
            categories = list(all_cats.values())
            system_prompt = build_neutral_prompt(categories)
        else:
            categories = settings.load_categories()
            system_prompt = build_system_prompt(categories)
        valid_cats = [c['key'] for c in categories]
        embed_cache = EmbedCache()

        def _enrich(article):
            return enrich(article, chat_model, embed_model, system_prompt, valid_cats, embed_cache)

        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg['score_concurrency']) as ex:
            scored = list(ex.map(_enrich, to_score))
        keep_prev = [dict(prev_by_id[i]) for i in ({a['id'] for a in reused} | carried_ids)]
        if keep_prev:
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg['score_concurrency']) as ex:
                scored.extend(ex.map(lambda a: _reembed(a, embed_model, emb_index_prev, embed_cache),
                                     keep_prev))
        reused_count = len(reused)
        for a in scored:
            a.setdefault('base_score', a.get('score', 0.0))
            a.setdefault('llm_category', a.get('category'))
        pool_by_id = {a['id']: a for a in scored}

        # Ollama health for this run: fallbacks (Ollama errored → default score),
        # average chat latency (high ≈ running on CPU), and whether the chat
        # model is resident in VRAM. Surfaced in Settings → Runs.
        fresh_scored = [a for a in scored if '_score_ok' in a]
        scored_count = len(fresh_scored)
        score_fallbacks = sum(1 for a in fresh_scored if not a.get('_score_ok'))
        lat = [a['_score_ms'] for a in fresh_scored if a.get('_score_ms') is not None]
        avg_score_ms = round(sum(lat) / len(lat), 1) if lat else None
        elat = [a['_enrich_ms'] for a in fresh_scored if a.get('_enrich_ms') is not None]
        avg_enrich_ms = round(sum(elat) / len(elat), 1) if elat else None
        embed_fallbacks = sum(1 for a in scored if a.get('embedding') is None)
        for m in ollama_ps():
            if m.get('name') == chat_model or m.get('model') == chat_model:
                size, vram = m.get('size') or 0, m.get('size_vram') or 0
                gpu_ratio = round(vram / size, 2) if size else None
                break
        log(f'ollama: {scored_count - score_fallbacks}/{scored_count} scored, '
            f'avg {avg_score_ms}ms/story, gpu_ratio={gpu_ratio}')

        # Per-user digests. One user's failure never blocks the others.
        _set_status(phase='personalizing')
        failures = []
        for inp in inputs:
            user = inp['user']
            if multi and not user.get('legacy') and users.get(user['id']) is None:
                continue        # deleted while this run was in progress
            with users.as_user(user):
                try:
                    arts, n_clusters = build_user_digest(inp, fresh, pool_by_id, embed_model,
                                                         embed_cache, multi)
                except Exception as exc:
                    failures.append(f'{user.get("username")}: {exc}')
                    log(f'digest for {user.get("username")} failed: {exc}')
                    continue
            article_count += len(arts)
            clusters += n_clusters
            if multi:
                log(f'{user.get("username")}: {len(arts)} articles in {n_clusters} clusters')
        embed_cache.save()
        if failures and len(failures) == len(inputs):
            raise RuntimeError('; '.join(failures))
        if failures:
            error = '; '.join(failures)
        log(f'wrote {article_count} articles in {clusters} clusters for {user_count} user(s) '
            f'(avg {avg_enrich_ms}ms/new article)')
        _set_status(article_count=article_count)
        ok = True
        return True
    except Exception as exc:
        error = str(exc)
        log(f'run failed: {exc}')
        return False
    finally:
        finished = _now_iso()
        try:
            dur = (datetime.fromisoformat(finished)
                   - datetime.fromisoformat(started)).total_seconds()
        except Exception:
            dur = None
        _record_run({
            'started_at': started,
            'finished_at': finished,
            'duration_s': round(dur, 1) if dur is not None else None,
            'status': 'ok' if ok else ('failed' if error else 'empty'),
            'feed_count': feed_count,
            'user_count': user_count,
            'new_count': new_count,
            'article_count': article_count,
            'clusters': clusters,
            'scored_count': scored_count,        # fresh articles sent to Ollama
            'score_fallbacks': score_fallbacks,  # of those, how many Ollama failed to score
            'embed_fallbacks': embed_fallbacks,
            'avg_score_ms': avg_score_ms,        # avg chat latency (high ⇒ likely CPU)
            'avg_enrich_ms': avg_enrich_ms,      # avg page fetch + score + embed per new article
            'reused_count': reused_count,        # already-scored articles reused (no chat call)
            'deferred_count': deferred_count,    # new articles over the per-run budget
            'ollama_peak_parallel': _ollama_peak,
            'gpu_ratio': gpu_ratio,              # chat model VRAM/size (1.0 ⇒ fully on GPU)
            'error': error,
        })
        _set_status(running=False, phase=None, last_run_at=finished, last_ok=ok)
        _run_lock.release()


# ──────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────
def _scheduler_loop():
    last_slot = None
    while not _stop.is_set():
        # Re-read each tick so schedule edits from the UI take effect live.
        cfg = settings.load()
        start, end, interval = (cfg['active_start_hour'],
                                cfg['active_end_hour'],
                                max(1, cfg['interval_hours']))
        minute = cfg.get('run_minute', 0)
        now = datetime.now().astimezone()
        in_window = start <= now.hour <= end
        on_interval = (now.hour - start) % interval == 0
        if in_window and on_interval and now.minute == minute:
            slot = (now.date(), now.hour)
            if slot != last_slot:
                last_slot = slot
                run_once()
        _stop.wait(30)


def start_background(run_now=True):
    """Start the scheduler thread, and optionally kick an immediate run."""
    threading.Thread(target=_scheduler_loop, name='scheduler', daemon=True).start()
    cfg = settings.load()
    log(f"scheduler started ({cfg['active_start_hour']}-{cfg['active_end_hour']} "
        f"every {cfg['interval_hours']}h at :{cfg.get('run_minute', 0):02d}; "
        f"max {cfg['max_new_scored']} new articles/run; "
        f"{OLLAMA_MAX_PARALLEL} concurrent model job(s))")
    if run_now:
        threading.Thread(target=run_once, name='initial-run', daemon=True).start()


_last_manual_refresh = 0.0
_refresh_guard = threading.Lock()


def refresh_async(force=False):
    """Trigger a one-off run in the background (POST /refresh). Never queues:
    returns 'running' if a run is in progress, 'cooldown' if the last manual
    trigger was under REFRESH_COOLDOWN_S ago, else 'started'."""
    global _last_manual_refresh
    with _refresh_guard:
        if get_status().get('running') or _run_lock.locked():
            return 'running'
        now = time.monotonic()
        if not force and _last_manual_refresh and now - _last_manual_refresh < REFRESH_COOLDOWN_S:
            return 'cooldown'
        _last_manual_refresh = now
        _set_status(running=True, phase='queued')
    threading.Thread(target=run_once, name='refresh', daemon=True).start()
    return 'started'


def warm_feedback_embeddings_async():
    """After onboarding (or a burst of votes) compute missing feedback
    embeddings in the background so the next run doesn't pay for them. Skips
    when a pipeline run is active (it will embed them itself)."""
    def _work():
        if _run_lock.locked():
            return
        try:
            cache = EmbedCache()
            pos, neg = load_feedback_examples(settings.load()['embed_model'], cache)
            cache.save()
            log(f'feedback profile warmed: {len(pos)} likes / {len(neg)} dislikes embedded')
        except Exception as exc:
            log(f'feedback warm-up failed: {exc}')
    users.spawn(_work, name='feedback-warm')


def benchmark(n=5):
    """CPU benchmark: fetch feeds, enrich up to n new articles serially with
    the configured models, and print per-article timings. Writes nothing."""
    cfg = settings.load()
    feeds = load_feeds()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg['lookback_hours'])
    arts = []
    for f in feeds[:10]:
        body = http_get_text(f.get('url', ''), FEED_TIMEOUT)
        for a in parse_feed(body, f.get('source', ''), f.get('category', 'tech'), cutoff)[:2]:
            a['id'] = stable_id(a['url'])
            arts.append(a)
    arts = arts[:n]
    cats = settings.load_categories()
    prompt, valid = build_system_prompt(cats), [c['key'] for c in cats]
    print(f'benchmark: {len(arts)} articles, chat={cfg["ollama_model"]} embed={cfg["embed_model"]}')
    times = []
    for a in arts:
        r = enrich(a, cfg['ollama_model'], cfg['embed_model'], prompt, valid)
        times.append(r['_enrich_ms'])
        print(f"  {r['_enrich_ms']:8.0f} ms total  {r['_score_ms']:8.0f} ms chat  "
              f"ok={r['_score_ok']}  {a['title'][:60]}")
    if times:
        avg = sum(times) / len(times)
        print(f'avg {avg:.0f} ms/article → ~{avg * cfg["max_new_scored"] / 60000:.1f} min for '
              f'max_new_scored={cfg["max_new_scored"]}')


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--benchmark':
        benchmark(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    else:
        run_once()
