#!/usr/bin/env python3
"""Article identifiers, and the one-time migration from the legacy ids.

An article id is derived from its URL: the first 16 hex chars (64 bits) of the
URL's SHA-256. The previous scheme was a 32-bit polynomial hash, short enough
that distinct URLs collided in practice (`.../Aa` and `.../BB` share an id),
which let one subscriber's pooled article be served for another's link.

Ids are persisted in several places per user: digest.json (articles and
cluster ids), embeddings.json (vector keys), state.json (readIds, later,
history), preferences.json (feedback keys, skipped onboarding ids) and the
onboarding pool. `migrate_current_user()` rewrites every legacy id whose URL is
known to the new id. A legacy id is only rewritten when it is exactly the
legacy hash of the record's own URL, so the migration is idempotent and never
re-points an id at the wrong story. Read marks for stories that already left
the digest have no URL to map from; they are left as they are (harmless: no
new article can carry a legacy id).
"""
import hashlib
import json
import os
import re
import tempfile

import users

ID_LEN = 16
_LEGACY_RE = re.compile(r'^[0-9a-f]{8}$')


def stable_id(url):
    """Deterministic 16-hex-char id from the URL (64 bits of SHA-256)."""
    return hashlib.sha256((url or '').encode('utf-8', 'surrogatepass')).hexdigest()[:ID_LEN]


def legacy_id(url):
    """The pre-v2 32-bit polynomial id (only used to migrate old data)."""
    h = 0
    for ch in url or '':
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return format(h, '08x')


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _write(path, obj):
    d = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.idmig-', suffix='.json', dir=d)
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


def _learn(mapping, aid, url):
    """Record legacy→new for a record if `aid` is the legacy hash of `url`."""
    if isinstance(aid, str) and isinstance(url, str) and url and _LEGACY_RE.match(aid) \
            and legacy_id(url) == aid:
        mapping[aid] = stable_id(url)


def _records(obj, key):
    items = obj.get(key) if isinstance(obj, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def migrate_current_user(digest_file, embeddings_file, state_file, preferences_file,
                         pool_file=None):
    """Rewrite legacy ids in the current user's files (paths are the global
    defaults; they are resolved through users.path). Returns the number of
    distinct ids remapped. Safe to run repeatedly."""
    paths = {k: users.path(v) for k, v in (
        ('digest', digest_file), ('emb', embeddings_file), ('state', state_file),
        ('prefs', preferences_file), ('pool', pool_file)) if v}
    data = {k: _read(p) for k, p in paths.items()}

    mapping = {}
    for a in _records(data.get('digest'), 'articles'):
        _learn(mapping, a.get('id'), a.get('url'))
        for o in a.get('_clusterOthers') or a.get('cluster_others') or []:
            if isinstance(o, dict):
                _learn(mapping, o.get('id'), o.get('url'))
    for key in ('later', 'history'):
        for i in _records(data.get('state'), key):
            _learn(mapping, i.get('id'), i.get('url'))
    fb = (data.get('prefs') or {}).get('feedback') if isinstance(data.get('prefs'), dict) else None
    if isinstance(fb, dict):
        for aid, e in fb.items():
            if isinstance(e, dict):
                _learn(mapping, aid, e.get('url'))
    for a in _records(data.get('pool'), 'articles'):
        _learn(mapping, a.get('id'), a.get('url'))
    if not mapping:
        return 0

    m = lambda i: mapping.get(i, i) if isinstance(i, str) else i

    d = data.get('digest')
    if isinstance(d, dict) and isinstance(d.get('articles'), list):
        for a in _records(d, 'articles'):
            a['id'] = m(a.get('id'))
            if 'cluster_id' in a:
                a['cluster_id'] = m(a['cluster_id'])
        _write(paths['digest'], d)

    e = data.get('emb')
    if isinstance(e, dict) and isinstance(e.get('vectors'), dict):
        e['vectors'] = {m(k): v for k, v in e['vectors'].items()}
        _write(paths['emb'], e)

    s = data.get('state')
    if isinstance(s, dict):
        if isinstance(s.get('readIds'), list):
            s['readIds'] = list(dict.fromkeys(m(r) for r in s['readIds']))
        for key in ('later', 'history'):
            for i in _records(s, key):
                i['id'] = m(i.get('id'))
        _write(paths['state'], s)

    p = data.get('prefs')
    if isinstance(p, dict):
        if isinstance(p.get('feedback'), dict):
            p['feedback'] = {m(k): v for k, v in p['feedback'].items()}
        ob = p.get('onboarding')
        if isinstance(ob, dict) and isinstance(ob.get('skipped'), list):
            ob['skipped'] = [m(x) for x in ob['skipped']]
        _write(paths['prefs'], p)

    pool = data.get('pool')
    if isinstance(pool, dict) and isinstance(pool.get('articles'), list):
        for a in _records(pool, 'articles'):
            a['id'] = m(a.get('id'))
        _write(paths['pool'], pool)
    return len(mapping)
