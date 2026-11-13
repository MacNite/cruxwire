#!/usr/bin/env python3
"""Explicit feedback (likes/dislikes) and onboarding state.

Stored in PREFERENCES_FILE (default /data/preferences.json) with the same
atomic-write + lock conventions as state.json / settings.json:

    {
      "version": 1,
      "onboarding": {"version": 1, "status": "completed", "startedAt": ...,
                     "completedAt": ..., "skipped": ["id", ...]},
      "feedback": {
        "<article-id>": {"vote": 1, "canonicalUrl": "...", "url": "...",
                         "title": "...", "description": "...", "source": "...",
                         "category": "...", "topic": "...", "origin": "feed",
                         "updatedAt": "2026-11-13T06:30:00Z"}
      }
    }

Semantics (deliberately independent of state.json):
  * vote is +1 / -1; 0 / None removes it. Repeat votes are idempotent; a
    changed vote updates the single entry in place.
  * de-duplicated by article id AND canonical URL (the same story reached via a
    different id replaces the older entry instead of counting twice).
  * a vote never marks anything read or saved, and dismiss is not a dislike —
    those live in state.json and are never touched here.
  * title/description are copied in at vote time so the signal survives the
    article expiring from the digest.
"""
import json
import os
import tempfile
import threading
from datetime import datetime, timezone

import ids
import images
import users

PREFERENCES_FILE = os.environ.get('PREFERENCES_FILE', '/data/preferences.json')
MAX_FEEDBACK = int(os.environ.get('MAX_FEEDBACK', '5000'))
MAX_SKIPPED = 2000
ONBOARDING_VERSION = 1
STATUSES = ('not_started', 'in_progress', 'completed', 'skipped')

_lock = threading.RLock()


def _path():
    """The current user's preferences file (see users.path)."""
    return users.path(PREFERENCES_FILE)


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _default():
    return {
        'version': 1,
        'onboarding': {'version': ONBOARDING_VERSION, 'status': 'not_started',
                       'startedAt': None, 'completedAt': None, 'skipped': []},
        'feedback': {},
    }


def _coerce(raw):
    d = _default()
    if not isinstance(raw, dict):
        return d
    ob = raw.get('onboarding') if isinstance(raw.get('onboarding'), dict) else {}
    status = ob.get('status') if ob.get('status') in STATUSES else 'not_started'
    d['onboarding'].update({
        'status': status,
        'startedAt': ob.get('startedAt') if isinstance(ob.get('startedAt'), str) else None,
        'completedAt': ob.get('completedAt') if isinstance(ob.get('completedAt'), str) else None,
        'skipped': [s for s in (ob.get('skipped') or []) if isinstance(s, str)][-MAX_SKIPPED:],
    })
    if ob.get('migrated'):
        d['onboarding']['migrated'] = True
    fb = raw.get('feedback') if isinstance(raw.get('feedback'), dict) else {}
    for aid, e in fb.items():
        if not isinstance(aid, str) or not isinstance(e, dict):
            continue
        if e.get('vote') not in (1, -1):
            continue
        # Votes saved under the old 32-bit ids move to the current id (the
        # entry keeps its URL), so the vote still shows on the article.
        d['feedback'][ids.migrate_id(aid, e.get('url'))] = e
    return d


def _read():
    try:
        with open(_path(), 'r', encoding='utf-8') as fh:
            return _coerce(json.load(fh))
    except FileNotFoundError:
        return None
    except Exception:
        # Malformed file: never crash — start from defaults (the next write
        # replaces it atomically).
        return _default()


def exists():
    return os.path.exists(_path())


def load():
    with _lock:
        return _read() or _default()


def _write(data):
    target = _path()
    d = os.path.dirname(target) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.prefs-', suffix='.json', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate_if_needed(existing_user):
    """First start after upgrading: an install that already has reading
    activity shouldn't be hijacked by the first-run wizard. Mark onboarding
    'skipped' (reachable again from Settings → Personalize)."""
    with _lock:
        if exists():
            return False
        data = _default()
        if existing_user:
            data['onboarding']['status'] = 'skipped'
            data['onboarding']['migrated'] = True
        _write(data)
        return True


# ──────────────────────────────────────────────────────────────────────
# Feedback
# ──────────────────────────────────────────────────────────────────────
def _clip(v, n):
    return images.clean_text(v, n) if isinstance(v, str) else ''


def make_entry(article, vote, origin='feed'):
    """Build a feedback entry from a SERVER-KNOWN article dict."""
    url = article.get('url') or ''
    return {
        'vote': vote,
        'canonicalUrl': images.canonical_url(images.resolve_canonical(url)),
        'url': url,
        'title': _clip(article.get('title'), 300),
        'description': _clip(article.get('description') or article.get('summary'), 400),
        'source': _clip(article.get('source'), 120),
        'category': _clip(article.get('category'), 40),
        'topic': article.get('topic') or images.infer_topic(
            article.get('title', ''), article.get('description') or article.get('summary') or '',
            article.get('category', '')),
        'image': article.get('image') if isinstance(article.get('image'), str) else None,
        'publishedAt': article.get('published_at') if isinstance(article.get('published_at'), str) else None,
        'origin': origin,
        'updatedAt': _now_iso(),
    }


def set_vote(article_id, article, vote, origin='feed'):
    """Apply a vote for `article_id`. `article` is the server-side record (or
    None when only removing). Returns (entry_or_None, changed: bool)."""
    if vote in (0, None):
        vote = 0
    if vote not in (1, -1, 0):
        raise ValueError('vote must be 1, -1, 0 or null')
    with _lock:
        data = _read() or _default()
        fb = data['feedback']
        cur = fb.get(article_id)
        if vote == 0:
            if cur is None:
                return None, False
            fb.pop(article_id, None)
            _write(data)
            return None, True
        if article is None and cur is None:
            raise KeyError(article_id)
        if cur and cur.get('vote') == vote:
            return cur, False                     # idempotent repeat
        entry = make_entry(article, vote, origin) if article else {**cur, 'vote': vote, 'updatedAt': _now_iso()}
        if cur and cur.get('origin') and not article:
            entry['origin'] = cur['origin']
        # Same story under another id → replace, don't double count.
        canon = entry.get('canonicalUrl')
        if canon:
            for other_id in [k for k, e in fb.items() if k != article_id and e.get('canonicalUrl') == canon]:
                fb.pop(other_id, None)
        fb[article_id] = entry
        # A vote supersedes an onboarding "skip" for the same card.
        sk = data['onboarding']['skipped']
        if article_id in sk:
            data['onboarding']['skipped'] = [s for s in sk if s != article_id]
        if len(fb) > MAX_FEEDBACK:
            for k, _ in sorted(fb.items(), key=lambda kv: kv[1].get('updatedAt') or '')[:len(fb) - MAX_FEEDBACK]:
                fb.pop(k, None)
        _write(data)
        return entry, True


def feedback():
    return load()['feedback']


def votes_map():
    return {k: e['vote'] for k, e in feedback().items()}


def progress(data=None):
    data = data or load()
    fb = data['feedback'].values()
    likes = [e for e in fb if e.get('vote') == 1]
    dislikes = [e for e in fb if e.get('vote') == -1]
    return {
        'likes': len(likes),
        'dislikes': len(dislikes),
        'topics': sorted({e.get('topic') or 'general' for e in likes}),
        'sources': sorted({e.get('source') for e in likes if e.get('source')}),
    }


def votes_list(limit=200):
    """Votes newest-first for the review/undo panel."""
    items = [{'id': k, **e} for k, e in feedback().items()]
    items.sort(key=lambda e: e.get('updatedAt') or '', reverse=True)
    return items[:limit]


# ──────────────────────────────────────────────────────────────────────
# Onboarding
# ──────────────────────────────────────────────────────────────────────
def onboarding():
    return load()['onboarding']


def set_status(status):
    if status not in STATUSES:
        raise ValueError(f'bad status {status}')
    with _lock:
        data = _read() or _default()
        ob = data['onboarding']
        if status == 'in_progress' and not ob.get('startedAt'):
            ob['startedAt'] = _now_iso()
        if status == 'completed':
            ob['completedAt'] = _now_iso()
        ob['status'] = status
        _write(data)
        return ob


def add_skip(article_id):
    with _lock:
        data = _read() or _default()
        sk = data['onboarding']['skipped']
        if article_id not in sk:
            sk.append(article_id)
            data['onboarding']['skipped'] = sk[-MAX_SKIPPED:]
            _write(data)
        return True


def skipped_ids():
    return set(onboarding().get('skipped') or [])
