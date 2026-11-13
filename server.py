#!/usr/bin/env python3
"""Cruxwire HTTP server.

Single-process app: serves the static dashboard, the per-user state API, and
in-app feed management, and starts the ingestion pipeline (pipeline.py) on a
background scheduler thread. No nginx, no n8n, no NAS share.

Endpoints:
  GET  /                      digest.html
  GET  /favicon.ico  + favicon-16x16.png / favicon-32x32.png / apple-touch-icon.png   icons
  GET  /digest.json           current digest (from DIGEST_FILE; empty if none yet)
  GET  /state    PUT /state   per-user read/later/history/learning state
  GET  /feeds    POST /feeds  DELETE /feeds   GET /feeds/check   feed management
  GET  /feeds.opml            export feeds as OPML
  POST /feeds/import          batch-add mapped feeds (OPML import); returns a summary
  GET  /settings PUT /settings  runtime-editable tuning knobs + models + blocklist
  GET  /models                installed Ollama models (for the model pickers)
  GET  /status                live pipeline status (running/phase/last run)
  POST /refresh               trigger a pipeline run now (202 started / 409 running / 429 cooldown)

Accounts (AUTH_MODE=local; see users.py):
  GET  /login                 sign-in / first-admin setup page
  GET  /auth/me               {mode, user|null, needsSetup}
  POST /auth/login /auth/logout /auth/setup     session cookie in / out; one-time admin setup
  PUT  /auth/password         change own password {current, new}
  GET|POST|PUT|DELETE /users  admin user management (?id=)
  Every other endpoint acts on the signed-in user's own data (401 when signed out).

Personalization / onboarding:
  GET  /onboarding            status, progress, feeds, examples + a balanced candidate batch
                              (?n=8&exclude=id,id&force=1 to rebuild the pool)
  POST /onboarding/sources    validate (with sample headlines) and add an RSS feed or a
                              Google News search ({url,source?,category?} | {query,lang?,country?})
  POST /onboarding/status     {status: in_progress|skipped|not_started} (start / skip / reopen)
  POST /onboarding/skip       {id} skip a candidate (NOT a dislike)
  POST /onboarding/complete   validate minimum likes, mark complete, start a personalised run
  POST /onboarding/seeds      {urls:[...]} import liked article URLs (SSRF-checked)
  GET  /feedback              {votes:{id:vote}} (+ items[] with ?full=1)
  POST /feedback              {id, vote: 1|-1|0|null} by server-known article id
  GET  /image?id=             resolved preview image (RSS → og:image → local fallback)
  GET  /articles              ranked, diversified, unread digest page (?offset&limit&category)
  GET  /static/fallback/*.svg local fallback illustrations

Security: mutating requests with a cross-site Origin are refused (CSRF guard);
CORS is off unless CORS_ALLOW_ORIGIN is set; request bodies are size-capped.
Without accounts (AUTH_MODE=none), deploy on a LAN/VPN or behind an
authenticated reverse proxy; with accounts, still terminate TLS in a proxy.
"""
import concurrent.futures
import json
import math
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import ids
import images
import onboarding
import pipeline
import preferences
import settings
import users

STATE_FILE  = os.environ.get('STATE_FILE', '/data/state.json')
DIGEST_FILE = os.environ.get('DIGEST_FILE', '/data/digest.json')
FEEDS_FILE  = os.environ.get('FEEDS_FILE', '/data/feeds.json')
STATIC_DIR  = os.environ.get('STATIC_DIR', '.')
SEED_FEEDS  = os.environ.get('SEED_FEEDS', '')
SEED_CATEGORIES = os.environ.get('SEED_CATEGORIES', '')
PORT = int(os.environ.get('PORT', '8090'))
# CORS is off by default (the UI is same-origin). Set to an origin list
# (comma-separated) or '*' to re-enable for external integrations.
CORS_ALLOW_ORIGIN = os.environ.get('CORS_ALLOW_ORIGIN', '').strip()
# Optional CSP frame-ancestors value, e.g. "'self' https://ha.example.lan" to
# allow only Home Assistant to embed the app. Unset = framing allowed.
FRAME_ANCESTORS = os.environ.get('FRAME_ANCESTORS', '').strip()
MAX_BODY_BYTES = int(os.environ.get('MAX_BODY_BYTES', str(4 * 1024 * 1024)))   # /state etc.
# Slow-client protection: a socket that sends nothing for REQUEST_TIMEOUT_S
# seconds (headers or body) is dropped, and at most MAX_CONNECTIONS requests are
# served at once (further connections wait in the listen backlog).
REQUEST_TIMEOUT_S = float(os.environ.get('REQUEST_TIMEOUT_S', '30'))
MAX_CONNECTIONS = max(1, int(os.environ.get('MAX_CONNECTIONS', '64')))
BIND_ADDR = os.environ.get('BIND_ADDR', '0.0.0.0').strip() or '0.0.0.0'
SMALL_BODY_BYTES = 64 * 1024                                                   # votes, onboarding

# History retention is a runtime-editable setting now (see settings.py); the env
# var there remains the default. Read Later is curated and never aged out.

# readIds are kept (not pruned to the digest) so "done" sticks across runs — see
# prune(). Capped only as a runaway guard; far more than any lookback window
# needs (a human won't read this many stories before the feed drops them).
MAX_READ_IDS = 20000

# Valid feed categories come from the configured category list (categories.json,
# served at GET /categories). POST /feeds enforces them so the UI can't insert a
# category the client has no label/color for.
def allowed_categories():
    return {c['key'] for c in settings.load_categories()}

STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.ico': 'image/x-icon',
    '.png': 'image/png',
    '.json': 'application/json',
    '.svg': 'image/svg+xml',
}
_FALLBACK_RE = re.compile(r'^/static/fallback/([a-z]{1,20})\.svg$')

# Icon files served at the root, by exact name (no path traversal).
ICON_FILES = ('favicon.ico', 'favicon-16x16.png', 'favicon-32x32.png',
              'apple-touch-icon.png', 'logo-mark.png')

DEFAULT_STATE = {
    'updatedAt': None,
    'readIds': [],
    'later': [],
    'history': [],
    'viewState': {
        'currentView': 'digest',
        'currentCat': 'all',
        'digestCat': 'all',
        # Client display preference: show per-story ranking score chips. Opt-in.
        'showScores': False,
    },
    # Per-source interaction counters used by client-side ranking. Persists
    # independently of readIds/later/history so the user's inbox hygiene
    # (clearing Later, history aging out) doesn't wipe their preferences.
    'sourceStats': {},
    'learning': {'lastDecayAt': None},
}

# Serialize read-modify-write so concurrent PUTs don't trample each other.
_state_lock = threading.Lock()
_feeds_lock = threading.Lock()


def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def load_state():
    try:
        with open(users.path(STATE_FILE), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            if not isinstance(data, dict):
                return DEFAULT_STATE.copy()
            return _migrate_state_ids({**DEFAULT_STATE, **data})
    except FileNotFoundError:
        return DEFAULT_STATE.copy()
    except Exception:
        return DEFAULT_STATE.copy()


def _migrate_state_ids(state):
    """Read Later / History entries saved under the old 32-bit article ids get
    the current id (they carry their URL). readIds can't be re-keyed (no URL),
    so they are matched by legacy id instead - see ids.is_read."""
    for key in ('later', 'history'):
        items = state.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get('id'), str):
                    item['id'] = ids.migrate_id(item['id'], item.get('url'))
    return state


def save_state(state):
    target = users.path(STATE_FILE)
    os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
    # Atomic write so a crash mid-write can't truncate state.json.
    dir_ = os.path.dirname(target) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.state-', suffix='.json', dir=dir_)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def prune(state):
    """Drop entries that can no longer affect any view.

    readIds: kept (deduped, capped) — NOT pruned to the current digest. Under
             the carry-forward model the pipeline vacates a read story from the
             digest immediately, but its source feed can still serve it for up
             to LOOKBACK_HOURS — so the id must persist or the story gets
             re-ingested as "fresh" and reappears. Pruning to the digest is
             exactly what made dismissed stories come back.
    later:   never aged out (manually curated); only de-duped/validated.
    history: viewedAt older than HISTORY_RETENTION_DAYS calendar days.
    """
    cfg = settings.load()
    history_retention_days = cfg['history_retention_days']

    seen = set()
    kept = []
    for rid in (state.get('readIds') or []):
        if isinstance(rid, str) and rid not in seen:
            seen.add(rid)
            kept.append(rid)
    state['readIds'] = kept[-MAX_READ_IDS:]   # newest-last; cap is a runaway guard only

    # Read Later is a manually-curated list — it is never aged out. Only drop
    # malformed entries and de-dupe by id; items persist until the user removes
    # them. (Retention applies to History, which is auto-tracked; see below.)
    later = []
    seen_later = set()
    for item in (state.get('later') or []):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
            continue
        if item['id'] in seen_later:
            continue
        seen_later.add(item['id'])
        later.append(item)
    state['later'] = later

    history_cutoff = datetime.now().astimezone().date() - timedelta(days=history_retention_days - 1)
    history = []
    for item in (state.get('history') or []):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
            continue
        ts = _parse_iso(item.get('viewedAt'))
        if ts is None or ts.astimezone().date() >= history_cutoff:
            history.append(item)
    state['history'] = history

    # Drop sourceStats entries for sources no longer in feeds.json. User intent
    # is "once removed, gone for good". Guard against a transient empty/unreadable
    # feeds list so a disk hiccup can't nuke every counter.
    try:
        feeds = load_feeds()
    except Exception:
        feeds = []
    if feeds:
        feed_sources = {
            f.get('source') for f in feeds
            if isinstance(f, dict) and isinstance(f.get('source'), str)
        }
        stats = state.get('sourceStats') or {}
        state['sourceStats'] = {s: v for s, v in stats.items() if s in feed_sources}

    return state


def _normalize_source_stats(raw):
    """Coerce sourceStats into {source: {opens, saves, dismisses}} with
    numeric counters. Counters can be fractional because of decay."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for source, counters in raw.items():
        if not isinstance(source, str) or not isinstance(counters, dict):
            continue
        coerced = {}
        for key in ('opens', 'saves', 'dismisses'):
            val = counters.get(key, 0)
            # bool is an int subclass and strings like "NaN" parse as floats;
            # accept only real finite numbers so state.json stays valid JSON.
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                val = 0
            coerced[key] = float(val)
        out[source] = coerced
    return out


def _normalize_learning(raw):
    if not isinstance(raw, dict):
        return {'lastDecayAt': None}
    last = raw.get('lastDecayAt')
    return {'lastDecayAt': last if isinstance(last, str) else None}


def _list_field(payload, key):
    val = payload.get(key)
    if val is None:
        return []
    if not isinstance(val, list):
        raise ValueError(f'{key} must be a list')
    return val


def normalize_payload(payload):
    """Coerce incoming PUT into the expected shape; ignore unknown fields.
    Raises ValueError (-> 400) for fields of the wrong type."""
    vs_default = DEFAULT_STATE['viewState']
    raw_vs = payload.get('viewState') if isinstance(payload.get('viewState'), dict) else {}
    view_state = {**vs_default, **{k: raw_vs[k] for k in vs_default
                                   if k in raw_vs and isinstance(raw_vs[k], type(vs_default[k]))}}
    updated = payload.get('updatedAt')

    return {
        'updatedAt': updated if isinstance(updated, str) else None,
        'readIds': [r for r in _list_field(payload, 'readIds') if isinstance(r, str) and len(r) <= 64],
        'later': [i for i in _list_field(payload, 'later') if isinstance(i, dict)],
        'history': [i for i in _list_field(payload, 'history') if isinstance(i, dict)],
        'viewState': view_state,
        'sourceStats': _normalize_source_stats(payload.get('sourceStats')),
        'learning': _normalize_learning(payload.get('learning')),
    }


def load_feeds():
    try:
        with open(users.path(FEEDS_FILE), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        # A malformed feeds.json shouldn't crash the server — surface an empty
        # list so the UI can at least show the add form.
        return []


def save_feeds(feeds):
    """Atomic write — the pipeline reads this file too, so it must never see
    partial bytes."""
    target = users.path(FEEDS_FILE)
    os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
    dir_ = os.path.dirname(target) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.feeds-', suffix='.json', dir=dir_)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(feeds, fh, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_TITLE_RE = re.compile(
    r'<title[^>]*>\s*(?:<!\[CDATA\[\s*)?([^<\]]+?)(?:\s*\]\]>)?\s*</title>',
    re.IGNORECASE,
)


def validate_feed_url(url, timeout=10):
    """Fetch the URL (SSRF-safe, size/time bounded) and confirm it actually
    returns an RSS or Atom feed with articles.

    Returns {'ok': bool, 'error': str?, 'title': str?, 'kind': 'rss'|'atom'?,
    'sample': [...]?}.
    """
    return onboarding.inspect_feed(url, timeout=timeout)


def normalize_feed_payload(payload):
    if not isinstance(payload, dict):
        return None, 'Payload must be an object'
    url = (payload.get('url') or '').strip()
    source = (payload.get('source') or '').strip()
    category = (payload.get('category') or '').strip()
    if not url:
        return None, 'url is required'
    if not source:
        return None, 'source is required'
    if not category:
        return None, 'category is required'
    if category not in allowed_categories():
        return None, f'Unknown category: {category}'
    return {'url': url, 'source': source, 'category': category}, None


def feeds_to_opml(feeds, categories):
    """Serialize the feed list to OPML 2.0, grouping feeds into <outline>
    folders by category (in category/priority order). Pure stdlib."""
    cats = {c['key']: c for c in categories}
    order = [c['key'] for c in categories]
    groups = {}
    for f in feeds:
        if isinstance(f, dict) and f.get('url'):
            groups.setdefault(f.get('category') or '', []).append(f)

    opml = ET.Element('opml', version='2.0')
    head = ET.SubElement(opml, 'head')
    ET.SubElement(head, 'title').text = 'Cruxwire feeds'
    body = ET.SubElement(opml, 'body')

    def add_folder(key):
        label = (cats.get(key) or {}).get('label') or (key or 'Uncategorized')
        folder = ET.SubElement(body, 'outline', text=label, title=label)
        for f in groups[key]:
            src = f.get('source') or f['url']
            ET.SubElement(folder, 'outline', type='rss', text=src, title=src, xmlUrl=f['url'])

    for key in order:                       # configured categories first, in order
        if key in groups:
            add_folder(key)
    for key in groups:                      # any leftover (unknown) categories
        if key not in order:
            add_folder(key)

    xml = ET.tostring(opml, encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml


def import_feeds(items, do_validate=True):
    """Batch-add mapped feeds: drop bad payloads / unknown categories, skip
    duplicates (existing + within the batch), optionally validate each URL
    concurrently, then append the survivors. Returns a summary."""
    with _feeds_lock:
        existing_urls = {f.get('url') for f in load_feeds() if isinstance(f, dict)}

    seen, candidates, invalid, dupes = set(), [], [], 0
    for it in (items or []):
        feed, err = normalize_feed_payload(it if isinstance(it, dict) else {})
        if err:
            src = (it or {}).get('source', '') if isinstance(it, dict) else ''
            invalid.append({'url': (it or {}).get('url', '') if isinstance(it, dict) else '',
                            'source': src, 'error': err})
            continue
        if feed['url'] in existing_urls or feed['url'] in seen:
            dupes += 1
            continue
        seen.add(feed['url'])
        candidates.append(feed)

    # Validate outside the lock (each call hits the network).
    failed, valid = [], candidates
    if do_validate and candidates:
        valid = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(candidates))) as ex:
            checked = list(ex.map(lambda f: (f, validate_feed_url(f['url'])), candidates))
        for f, res in checked:
            (valid if res.get('ok') else failed).append(
                f if res.get('ok') else {'url': f['url'], 'source': f['source'],
                                         'error': res.get('error') or 'Invalid feed'})

    added = 0
    with _feeds_lock:
        feeds = load_feeds()
        cur = {f.get('url') for f in feeds if isinstance(f, dict)}
        for f in valid:
            if f['url'] in cur:
                dupes += 1
                continue
            cur.add(f['url'])
            feeds.append(f)
            added += 1
        if added:
            save_feeds(feeds)

    return {'added': added, 'skipped_dupes': dupes, 'failed': failed, 'invalid': invalid}


def seed_feeds_if_missing():
    """First-run convenience: copy the bundled sample feed list into the data
    volume so a fresh deploy starts with feeds instead of an empty UI."""
    if os.path.exists(FEEDS_FILE) or not SEED_FEEDS:
        return
    try:
        with open(SEED_FEEDS, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, list) and data:
            save_feeds(data)
            print(f'seeded {len(data)} feeds from {SEED_FEEDS}', flush=True)
    except Exception as exc:
        print(f'feed seed failed: {exc}', flush=True)


def seed_categories_if_missing():
    """First-run convenience: copy the bundled sample categories onto the data
    volume so a self-hoster has an editable starting point (and the app loads
    them from one place). Until edited, load_categories() falls back to the
    built-in defaults anyway, so this is purely for discoverability."""
    target = users.path(settings.CATEGORIES_FILE)
    if os.path.exists(target) or not SEED_CATEGORIES:
        return
    try:
        with open(SEED_CATEGORIES, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, list) and data:
            os.makedirs(os.path.dirname(target) or '.', exist_ok=True)
            with open(target, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2)
            print(f'seeded {len(data)} categories from {SEED_CATEGORIES}', flush=True)
    except Exception as exc:
        print(f'category seed failed: {exc}', flush=True)


def deploy_info():
    """Deploy-time wiring shown read-only in the UI — set via docker-compose /
    env at container start, so editing needs a restart (not a writable setting).
    Model selection is NOT here; it's a runtime setting."""
    return {
        'ollama_host': pipeline.OLLAMA_HOST,
        'port': PORT,
        'state_file': STATE_FILE,
        'digest_file': DIGEST_FILE,
        'feeds_file': FEEDS_FILE,
        'settings_file': settings.SETTINGS_FILE,
    }


def list_ollama_models():
    """Model names installed on the configured Ollama host, for the model
    pickers in Settings. Returns {'ok': bool, 'models': [...], 'error': str?}."""
    url = pipeline.OLLAMA_HOST + '/api/tags'
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
        names = sorted(
            m.get('name') for m in (data.get('models') or [])
            if isinstance(m, dict) and m.get('name')
        )
        return {'ok': True, 'models': names}
    except Exception as exc:
        return {'ok': False, 'models': [], 'error': f'{exc}'}


def settings_payload():
    """Full Settings response: effective values, the schema that drives the
    form, and read-only deploy info (admins only). With accounts on, each
    schema entry says whether this caller may edit it."""
    admin = users.current().get('role') == 'admin'
    schema = settings.schema()
    for spec in schema.values():
        spec['editable'] = admin or spec['scope'] == 'user'
    return {
        'settings': settings.load(),
        'schema': schema,
        'deploy': deploy_info() if admin else None,
        'multiUser': users.enabled(),
    }


def ranked_articles(category='all', offset=0, limit=20):
    """The digest as a ranked feed page: unread and unsaved stories only, one
    representative per same-story cluster, re-ranked by the composite score
    with topic/publisher diversity and the exploration share. Paging walks
    further down the same ranking, so 'Discover more' never repeats items."""
    try:
        with open(users.path(DIGEST_FILE), 'r', encoding='utf-8') as fh:
            digest = json.load(fh)
    except Exception:
        digest = {}
    arts = [a for a in (digest.get('articles') or []) if isinstance(a, dict) and a.get('id')]
    with _state_lock:
        state = load_state()
    read = set(r for r in (state.get('readIds') or []) if isinstance(r, str))
    later = {i.get('id') for i in (state.get('later') or []) if isinstance(i, dict)}
    seen = lambda a: ids.is_read(a['id'], a.get('url'), read) or a['id'] in later
    hidden_clusters = {a.get('cluster_id') for a in arts if seen(a)}
    reps = {}
    for a in arts:
        cid = a.get('cluster_id') or a['id']
        if seen(a) or cid in hidden_clusters:
            continue
        if category and category != 'all' and a.get('category') != category:
            continue
        cur = reps.get(cid)
        if cur is None or (a.get('cluster_rep') and not cur.get('cluster_rep')):
            reps[cid] = a
    for cid, rep in reps.items():
        rep['cluster_others'] = [
            {'id': o['id'], 'title': o.get('title'), 'url': o.get('url'),
             'source': o.get('publisher') or o.get('source')}
            for o in arts if (o.get('cluster_id') or o['id']) == cid and o is not rep
            and not ids.is_read(o['id'], o.get('url'), read)][:6]
    cfg = settings.load()
    ranked = pipeline.diversify(list(reps.values()), explore_share=cfg['exploration_share'])
    votes = preferences.votes_map()
    total = len(ranked)
    offset = max(0, offset)
    limit = max(1, min(100, limit))
    page = []
    for a in ranked[offset:offset + limit]:
        a = dict(a)
        a['vote'] = votes.get(a['id'], 0)
        if not a.get('fallback_image'):
            a.update(pipeline.image_fields(a, a.get('image'), cache=False))
        page.append(a)
    nxt = offset + len(page)
    return {'generated_at': digest.get('generated_at'), 'total': total, 'offset': offset,
            'next_offset': nxt if nxt < total else None, 'articles': page}


def _parse_vote(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        raise ValueError('vote must be 1, -1, 0 or null')
    if isinstance(v, (int, float)) and v in (1, -1, 0):
        return int(v)
    raise ValueError('vote must be 1, -1, 0 or null')


class BodyTooLarge(Exception):
    pass


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent request threads, so a pile
    of slow or idle connections can't spawn unbounded threads."""
    daemon_threads = True

    def __init__(self, *args, max_connections=MAX_CONNECTIONS, **kwargs):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class StateHandler(BaseHTTPRequestHandler):
    # Per-socket inactivity timeout (applied by StreamRequestHandler.setup) -
    # bounds how long a client may stall while sending headers or a body.
    timeout = REQUEST_TIMEOUT_S

    server_version = 'Cruxwire'
    sys_version = ''

    # ── response helpers ─────────────────────────────────────────────
    def _cors(self):
        if not CORS_ALLOW_ORIGIN:
            return
        if CORS_ALLOW_ORIGIN == '*':
            self.send_header('Access-Control-Allow-Origin', '*')
            return
        origin = self.headers.get('Origin') or ''
        allowed = [o.strip() for o in CORS_ALLOW_ORIGIN.split(',') if o.strip()]
        if origin in allowed:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')

    def _security_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if FRAME_ANCESTORS:
            self.send_header('Content-Security-Policy', f'frame-ancestors {FRAME_ANCESTORS}')

    def _set_headers(self, status=200, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self._cors()
        self._security_headers()
        self.end_headers()

    def _json(self, status, payload):
        self._set_headers(status)
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def _serve_file(self, fs_path, content_type, cache='no-cache'):
        try:
            with open(fs_path, 'rb') as fh:
                data = fh.read()
        except FileNotFoundError:
            self.send_error(404, 'Not Found')
            return
        except Exception:
            self.send_error(500, 'Read error')
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        # Always revalidate static assets so a deploy shows up on a normal
        # reload — no hard-refresh needed after the UI changes.
        self.send_header('Cache-Control', cache)
        self._cors()
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _serve_digest(self):
        """Serve the current digest, or an empty one if the pipeline hasn't
        produced its first run yet (nicer than a 404 in the UI)."""
        try:
            with open(users.path(DIGEST_FILE), 'rb') as fh:
                data = fh.read()
        except FileNotFoundError:
            self._json(200, {'generated_at': None, 'article_count': 0, 'articles': []})
            return
        except Exception:
            self.send_error(500, 'Read error')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self._cors()
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self, allow_list=False, max_bytes=SMALL_BODY_BYTES):
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            return None, 'Invalid Content-Length'
        if length < 0:
            return None, 'Invalid Content-Length'
        if length > max_bytes:
            raise BodyTooLarge()
        body = self.rfile.read(length) if length else b''
        try:
            payload = json.loads(body.decode('utf-8') if body else '{}')
        except Exception as exc:
            return None, f'Invalid JSON: {exc}'
        if allow_list and isinstance(payload, list):
            return payload, None
        if not isinstance(payload, dict):
            return None, 'Payload must be an object'
        return payload, None

    def _body_or_400(self, **kw):
        """Read a JSON object body; on error reply 400 and return None."""
        payload, err = self._read_json_body(**kw)
        if err:
            self._json(400, {'error': err})
            return None
        return payload

    def _same_origin(self):
        """CSRF guard for mutating requests: a browser always sends Origin on
        cross-origin POST/PUT/DELETE; refuse it unless it matches the Host we
        were reached on (or an explicitly allowed CORS origin). Non-browser
        clients (curl, Home Assistant REST) send no Origin and pass."""
        origin = self.headers.get('Origin')
        if not origin:
            return True
        if CORS_ALLOW_ORIGIN == '*' or origin in [o.strip() for o in CORS_ALLOW_ORIGIN.split(',') if o.strip()]:
            return True
        host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '').split(',')[0].strip().lower()
        try:
            o = urlparse(origin)
        except ValueError:
            return False
        return bool(host) and (o.netloc or '').lower() == host

    def _guard_mutation(self):
        if self._same_origin():
            return True
        self._json(403, {'error': 'Cross-origin request refused'})
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    # ── auth ─────────────────────────────────────────────────────────
    def _session_token(self):
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
        except Exception:
            return None
        m = c.get(users.SESSION_COOKIE)
        return m.value if m else None

    def _resolve_user(self):
        """The user making this request: the implicit owner without accounts,
        else whoever the session cookie belongs to (None = not logged in)."""
        if not users.enabled():
            return users.OWNER_PSEUDO
        return users.session_user(self._session_token())

    def _is_admin(self):
        return bool(self.user) and self.user.get('role') == 'admin'

    def _require_admin(self):
        if self._is_admin():
            return True
        self._json(403, {'error': 'Admins only'})
        return False

    def _cookie_secure(self):
        if users.COOKIE_SECURE in ('1', 'true', 'yes'):
            return True
        if users.COOKIE_SECURE in ('0', 'false', 'no'):
            return False
        proto = (self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        return proto == 'https'

    def _session_cookie(self, token, max_age):
        parts = [f'{users.SESSION_COOKIE}={token}', 'Path=/', 'HttpOnly',
                 f'SameSite={users.COOKIE_SAMESITE}', f'Max-Age={max_age}']
        if self._cookie_secure() or users.COOKIE_SAMESITE == 'None':
            parts.append('Secure')
        return '; '.join(parts)

    def _json_with_cookie(self, status, payload, cookie):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Set-Cookie', cookie)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self._security_headers()
        self.end_headers()

    def _client_ip(self):
        return self.client_address[0] if self.client_address else ''

    def _me_payload(self):
        return {'mode': users.AUTH_MODE, 'user': users.public(self.user),
                'needsSetup': users.needs_setup()}

    def _start_session(self, user, status=200):
        token = users.create_session(user['id'])
        self.user = user
        self._json_with_cookie(status, self._me_payload(),
                               self._session_cookie(token, users.SESSION_TTL_S))

    def _auth_routes(self, method, path):
        """Login/logout/setup and account endpoints. Returns True if handled."""
        if method == 'GET' and path == '/login':
            if not users.enabled():
                self._redirect('/')
                return True
            self._serve_file(os.path.join(STATIC_DIR, 'login.html'), STATIC_TYPES['.html'])
            return True
        if method == 'GET' and path == '/auth/me':
            self._json(200, self._me_payload())
            return True
        if method == 'POST' and path == '/auth/login':
            if not users.enabled():
                self._json(400, {'error': 'Accounts are not enabled (AUTH_MODE=none).'})
                return True
            payload = self._body_or_400()
            if payload is None:
                return True
            username = str(payload.get('username') or '')[:64].strip().lower()
            keys = ('u:' + username, 'ip:' + self._client_ip())
            if users.throttled(*keys):
                self._json(429, {'error': 'Too many failed attempts. Try again later.'})
                return True
            user = users.authenticate(username, str(payload.get('password') or ''))
            if user is None:
                users.record_failure(*keys)
                self._json(401, {'error': 'Wrong username or password.'})
                return True
            users.clear_failures(keys[0])
            self._start_session(user)
            return True
        if method == 'POST' and path == '/auth/setup':
            if not users.needs_setup():
                self._json(409, {'error': 'Setup is already complete.'})
                return True
            payload = self._body_or_400()
            if payload is None:
                return True
            try:
                # first_only: re-checked under the account-store lock, so two
                # concurrent setup requests can't both create an admin.
                user = users.create_user(payload.get('username'), payload.get('password'), 'admin',
                                         payload.get('display_name'), first_only=True)
            except (users.SetupComplete, users.StoreCorrupt):
                self._json(409, {'error': 'Setup is already complete.'})
                return True
            except ValueError as exc:
                self._json(400, {'error': str(exc)})
                return True
            self._start_session(user, 201)
            return True
        if method == 'POST' and path == '/auth/logout':
            users.revoke_session(self._session_token())
            self._json_with_cookie(200, {'ok': True}, self._session_cookie('', 0))
            return True
        return False

    def _account_routes(self, method, path, qs):
        """/auth/password (self-service) and /users (admin). Requires a
        logged-in user. Returns True if handled."""
        if path == '/auth/password' and method == 'PUT':
            if not users.enabled():
                self._json(400, {'error': 'Accounts are not enabled (AUTH_MODE=none).'})
                return True
            payload = self._body_or_400()
            if payload is None:
                return True
            try:
                users.set_password(self.user['id'], payload.get('current'), payload.get('new'))
            except PermissionError as exc:
                self._json(403, {'error': str(exc)})
                return True
            except ValueError as exc:
                self._json(400, {'error': str(exc)})
                return True
            # Changing the password signs out every session; start a fresh one.
            self._start_session(users.get(self.user['id']))
            return True
        if path != '/users':
            return False
        if not users.enabled():
            self._json(400, {'error': 'Accounts are not enabled (AUTH_MODE=none).'})
            return True
        if not self._require_admin():
            return True
        uid = (qs.get('id') or [''])[0]
        try:
            if method == 'GET':
                self._json(200, {'users': [users.public(u) for u in users.list_users()]})
            elif method == 'POST':
                payload = self._body_or_400()
                if payload is None:
                    return True
                u = users.create_user(payload.get('username'), payload.get('password'),
                                      payload.get('role') or 'user', payload.get('display_name'))
                with users.as_user(u):
                    seed_categories_if_missing()   # an editable starting point
                self._json(201, users.public(u))
            elif method == 'PUT':
                payload = self._body_or_400()
                if payload is None:
                    return True
                if uid == self.user['id'] and (payload.get('disabled') or payload.get('role') == 'user'):
                    self._json(400, {'error': 'You cannot disable or demote your own account.'})
                    return True
                patch = {k: payload[k] for k in ('display_name', 'role', 'disabled', 'password')
                         if k in payload}
                self._json(200, users.public(users.update_user(uid, patch)))
            elif method == 'DELETE':
                if uid == self.user['id']:
                    self._json(400, {'error': 'You cannot delete your own account.'})
                    return True
                self._json(200, {'deleted': users.public(users.delete_user(uid))})
            else:
                self.send_error(405, 'Method Not Allowed')
        except KeyError:
            self._json(404, {'error': 'No such user'})
        except ValueError as exc:
            self._json(400, {'error': str(exc)})
        return True

    def _is_public_asset(self, path):
        return path.lstrip('/') in ICON_FILES or bool(_FALLBACK_RE.match(path))

    # ── routing ──────────────────────────────────────────────────────
    def _dispatch(self, method):
        if method != 'GET' and not self._guard_mutation():
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            self.user = self._resolve_user()
            if self._auth_routes(method, path):
                return
            if self.user is None and not self._is_public_asset(path):
                if method == 'GET' and path in ('/', '/index.html', '/digest.html'):
                    self._redirect('/login')
                else:
                    self._json(401, {'error': 'Login required', 'login': '/login'})
                return
            with users.as_user(self.user or users.OWNER_PSEUDO):
                if self.user is not None and self._account_routes(method, path, parse_qs(parsed.query)):
                    return
                getattr(self, '_' + method.lower())()
        except BodyTooLarge:
            self._json(413, {'error': 'Request body too large'})
        except users.StoreCorrupt as exc:
            print(f'[server] {exc}', flush=True)
            self._json(503, {'error': 'The account store is damaged; see the server log.'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        self._dispatch('GET')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_POST(self):
        self._dispatch('POST')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def _get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        q1 = lambda k, d='': (qs.get(k) or [d])[0]
        if path == '/state':
            with _state_lock:
                state = prune(load_state())
            self._json(200, state)
            return
        if path == '/feeds':
            self._json(200, load_feeds())
            return
        if path == '/feeds.opml':
            data = feeds_to_opml(load_feeds(), settings.load_categories()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/x-opml; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="cruxwire-feeds.opml"')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if path == '/settings':
            self._json(200, settings_payload())
            return
        if path == '/models':
            if not self._require_admin():
                return
            self._json(200, list_ollama_models())
            return
        if path == '/status':
            self._json(200, pipeline.get_status())
            return
        if path == '/runs':
            if not self._require_admin():
                return
            self._json(200, pipeline.get_runs())
            return
        if path == '/categories':
            self._json(200, settings.load_categories())
            return
        if path == '/search':
            q = q1('q')[:300]
            try:
                thr = float(q1('threshold'))
            except ValueError:
                thr = pipeline.SEARCH_THRESHOLD
            results = pipeline.semantic_search(q, threshold=thr)
            self._json(200, {'query': q, 'results': results})
            return
        if path == '/feeds/check':
            result = validate_feed_url(q1('url'))
            self._json(200 if result['ok'] else 400, result)
            return
        if path == '/digest.json':
            self._serve_digest()
            return
        if path == '/onboarding':
            try:
                n = int(q1('n', '8'))
            except ValueError:
                n = 8
            exclude = [x for x in q1('exclude').split(',') if x][:200]
            self._json(200, onboarding.status_payload(include_batch=q1('batch', '1') != '0',
                                                      exclude=exclude, n=n,
                                                      force_pool=q1('force') in ('1', 'true')))
            return
        if path == '/feedback':
            out = {'votes': preferences.votes_map()}
            if q1('full') in ('1', 'true'):
                out['items'] = preferences.votes_list()
            self._json(200, out)
            return
        if path == '/image':
            res = onboarding.resolve_image(q1('id'))
            if res is None:
                self._json(404, {'error': 'Unknown article'})
                return
            self._json(200, res)
            return
        if path == '/articles':
            try:
                offset, limit = int(q1('offset', '0')), int(q1('limit', '20'))
            except ValueError:
                self._json(400, {'error': 'offset/limit must be integers'})
                return
            self._json(200, ranked_articles(q1('category', 'all')[:40], offset, limit))
            return
        m = _FALLBACK_RE.match(path)
        if m:
            name = m.group(1)
            if name not in images.FALLBACK_TOPICS:
                self.send_error(404, 'Not Found')
                return
            self._serve_file(os.path.join(STATIC_DIR, 'static', 'fallback', name + '.svg'),
                             STATIC_TYPES['.svg'], cache='public, max-age=86400')
            return
        # Static assets (explicit whitelist — no path traversal).
        if path in ('/', '/index.html', '/digest.html'):
            self._serve_file(os.path.join(STATIC_DIR, 'digest.html'), STATIC_TYPES['.html'])
            return
        if path.lstrip('/') in ICON_FILES:
            name = path.lstrip('/')
            self._serve_file(os.path.join(STATIC_DIR, name), STATIC_TYPES[os.path.splitext(name)[1]])
            return
        self.send_error(404, 'Not Found')

    def _put(self):
        path = urlparse(self.path).path
        if path == '/settings':
            payload = self._body_or_400()
            if payload is None:
                return
            # settings.save validates/clamps and ignores unknown keys; only an
            # admin may change the global (non-personal) ones.
            settings.save(payload, allow_global=self._is_admin())
            self._json(200, settings_payload())
            return
        if path == '/categories':
            payload = self._body_or_400(allow_list=True)
            if payload is None:
                return
            try:
                cats = settings.save_categories(payload)
            except ValueError as exc:
                self._json(400, {'error': str(exc)})
                return
            self._json(200, cats)
            return
        if path != '/state':
            self.send_error(404, 'Not Found')
            return
        payload, err = self._read_json_body(max_bytes=MAX_BODY_BYTES)
        if err:
            self.send_error(400, err)
            return

        try:
            normalize_payload(payload)
        except ValueError as exc:
            self._json(400, {'error': str(exc)})
            return
        with _state_lock:
            state = normalize_payload(payload)
            # Old clients that don't send sourceStats / learning would silently
            # wipe them. Preserve whatever's on disk when not included explicitly.
            if 'sourceStats' not in payload:
                state['sourceStats'] = load_state().get('sourceStats', {})
            if 'learning' not in payload:
                state['learning'] = load_state().get('learning', {'lastDecayAt': None})
            state = prune(state)
            state['updatedAt'] = _now_iso()
            save_state(state)
        self._json(200, state)

    def _post(self):
        path = urlparse(self.path).path
        if path == '/refresh':
            result = pipeline.refresh_async()
            if result == 'started':
                self._json(202, {'status': 'refresh triggered'})
            elif result == 'running':
                self._json(409, {'status': 'already running', 'error': 'A refresh is already running.'})
            else:
                self._json(429, {'status': 'cooldown',
                                 'error': 'Refreshed recently — try again in a minute.'})
            return
        if path == '/feedback':
            payload = self._body_or_400()
            if payload is None:
                return
            aid = payload.get('id')
            try:
                vote = _parse_vote(payload.get('vote'))
            except ValueError as exc:
                self._json(400, {'error': str(exc)})
                return
            article, origin = onboarding.find_article(aid)
            if article is None:
                self._json(404, {'error': 'Unknown article id'})
                return
            if payload.get('context') == 'onboarding':
                origin = 'onboarding'
            entry, changed = preferences.set_vote(aid, article, vote, origin=origin or 'feed')
            if vote and preferences.onboarding()['status'] == 'not_started':
                preferences.set_status('in_progress')
            self._json(200, {'id': aid, 'vote': entry['vote'] if entry else 0, 'changed': changed,
                             'progress': onboarding.progress_payload()})
            return
        if path == '/onboarding/sources':
            payload = self._body_or_400()
            if payload is None:
                return
            self._onboarding_add_source(payload)
            return
        if path == '/onboarding/status':
            payload = self._body_or_400()
            if payload is None:
                return
            status = payload.get('status')
            if status not in ('in_progress', 'skipped', 'not_started'):
                self._json(400, {'error': 'status must be in_progress, skipped or not_started'})
                return
            ob = preferences.set_status(status)
            self._json(200, {'onboarding': {k: v for k, v in ob.items() if k != 'skipped'}})
            return
        if path == '/onboarding/skip':
            payload = self._body_or_400()
            if payload is None:
                return
            article, _ = onboarding.find_article(payload.get('id'))
            if article is None:
                self._json(404, {'error': 'Unknown article id'})
                return
            preferences.add_skip(article['id'])
            self._json(200, {'ok': True})
            return
        if path == '/onboarding/complete':
            ok, out = onboarding.complete()
            self._json(200 if ok else 400, out)
            return
        if path == '/onboarding/seeds':
            payload = self._body_or_400()
            if payload is None:
                return
            try:
                results = onboarding.import_seeds(payload.get('urls'))
            except ValueError as exc:
                self._json(400, {'error': str(exc)})
                return
            self._json(200, {'results': results, 'progress': onboarding.progress_payload()})
            return
        if path == '/tldr':
            payload = self._body_or_400()
            if payload is None:
                return
            url = (payload or {}).get('url')
            title = (payload or {}).get('title') or ''
            if not isinstance(url, str) or not url.startswith('http'):
                self._json(400, {'error': 'valid url required'})
                return
            result = pipeline.generate_tldr(url, str(title)[:300])
            if result is None:
                self._json(502, {'error': 'Could not summarise (Ollama may be down)'})
                return
            self._json(200, result)
            return
        if path == '/feeds/import':
            payload = self._body_or_400(max_bytes=MAX_BODY_BYTES)
            if payload is None:
                return
            items = payload.get('feeds')
            if not isinstance(items, list):
                self._json(400, {'error': 'feeds[] is required'})
                return
            do_validate = payload.get('validate', True) is not False
            self._json(200, import_feeds(items[:1000], do_validate))
            return
        if path != '/feeds':
            self.send_error(404, 'Not Found')
            return
        payload = self._body_or_400()
        if payload is None:
            return
        feed, err = normalize_feed_payload(payload)
        if err:
            self._json(400, {'error': err})
            return

        # Skip validation when the client is restoring a feed it just removed —
        # the URL was confirmed at original-add time and revalidating could fail
        # flakily for a feed we already know is good.
        qs = parse_qs(urlparse(self.path).query)
        skip_validate = (qs.get('skip_validate') or ['0'])[0] in ('1', 'true')

        if not skip_validate:
            validation = validate_feed_url(feed['url'])
            if not validation['ok']:
                self._json(400, {'error': validation.get('error') or 'Invalid feed'})
                return

        with _feeds_lock:
            feeds = load_feeds()
            if any(isinstance(f, dict) and f.get('url') == feed['url'] for f in feeds):
                self._json(409, {'error': 'Feed already exists'})
                return
            feeds.append(feed)
            save_feeds(feeds)
        self._json(201, feed)

    def _onboarding_add_source(self, payload):
        """Validate (with sample headlines) and add one feed. Accepts a URL or
        a Google News search query. Duplicate URLs are reported, not errors."""
        url = payload.get('url')
        if not url and payload.get('query'):
            try:
                url = onboarding.google_news_search_url(payload.get('query'), payload.get('lang') or 'en-US',
                                                        payload.get('country') or 'US')
            except ValueError as exc:
                self._json(400, {'ok': False, 'error': str(exc)})
                return
        if not isinstance(url, str) or not url.strip():
            self._json(400, {'ok': False, 'error': 'Enter a feed URL or a search query.'})
            return
        url = url.strip()[:2048]
        with _feeds_lock:
            existing = next((f for f in load_feeds() if isinstance(f, dict) and f.get('url') == url), None)
        if existing:
            self._json(200, {'ok': True, 'duplicate': True, 'feed': existing})
            return
        result = validate_feed_url(url)
        if not result.get('ok'):
            self._json(400, {'ok': False, 'url': url, 'error': result.get('error') or 'Invalid feed'})
            return
        cats = settings.load_categories()
        keys = [c['key'] for c in cats]
        category = payload.get('category') if payload.get('category') in keys else (keys[0] if keys else 'tech')
        default_name = (('Google News: ' + str(payload.get('query')).strip()[:60]) if payload.get('query')
                        else (result.get('title') or urlparse(url).hostname or url))
        source = (payload.get('source') or '').strip()[:80] or default_name
        feed = {'url': url, 'source': source, 'category': category}
        with _feeds_lock:
            feeds = load_feeds()
            if not any(isinstance(f, dict) and f.get('url') == url for f in feeds):
                feeds.append(feed)
                save_feeds(feeds)
        self._json(201, {'ok': True, 'feed': feed, 'title': result.get('title'),
                         'count': result.get('count'), 'sample': result.get('sample') or []})

    def _delete(self):
        if urlparse(self.path).path != '/feeds':
            self.send_error(404, 'Not Found')
            return
        qs = parse_qs(urlparse(self.path).query)
        url = (qs.get('url') or [''])[0].strip()
        if not url:
            self._json(400, {'error': 'url query param is required'})
            return

        with _feeds_lock:
            feeds = load_feeds()
            removed = next((f for f in feeds if isinstance(f, dict) and f.get('url') == url), None)
            if removed is None:
                self._json(404, {'error': 'Feed not found'})
                return
            new_feeds = [f for f in feeds if not (isinstance(f, dict) and f.get('url') == url)]
            save_feeds(new_feeds)

        # Wipe the source's stats too so the ghost row disappears immediately.
        # The client stashes the returned stats for in-session undo.
        removed_stats = None
        source = removed.get('source')
        if source:
            with _state_lock:
                state = load_state()
                stats = state.get('sourceStats') or {}
                if source in stats:
                    removed_stats = stats.pop(source)
                    state['sourceStats'] = stats
                    state['updatedAt'] = _now_iso()
                    save_state(state)
        self._json(200, {'removed': removed, 'stats': removed_stats})

    def log_message(self, format, *args):
        return


def migrate_preferences():
    """Create preferences.json on first start. An install that already has
    reading activity is marked onboarding='skipped' so an upgrade doesn't throw
    the user into the first-run wizard (it stays available in Settings)."""
    state = load_state() if os.path.exists(STATE_FILE) else {}
    existing = bool(state.get('history') or state.get('later') or state.get('readIds'))
    if preferences.migrate_if_needed(existing):
        print(f'preferences initialised (existing user: {existing})', flush=True)


if __name__ == '__main__':
    msg = users.bootstrap()
    if msg:
        print(f'auth ({users.AUTH_MODE}): {msg}', flush=True)
    seed_feeds_if_missing()
    seed_categories_if_missing()
    migrate_preferences()
    pipeline.start_background(run_now=True)
    server = BoundedThreadingHTTPServer((BIND_ADDR, PORT), StateHandler)
    print(f'Cruxwire server listening on http://{BIND_ADDR}:{PORT}/', flush=True)
    if not users.enabled() and BIND_ADDR not in ('127.0.0.1', '::1', 'localhost'):
        print('WARNING: AUTH_MODE=none - there is no login, so anyone who can reach this port '
              'controls the app. Keep it on a trusted network, or set AUTH_MODE=local / put an '
              'authenticating reverse proxy in front. See SECURITY.md.', flush=True)
    server.serve_forever()
