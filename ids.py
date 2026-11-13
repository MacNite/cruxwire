"""Article ids.

An article's id is derived from its URL: the first 16 hex chars (64 bits) of
its SHA-256. Ids used to be an 8-hex-char 32-bit string hash, which collided
trivially (e.g. ".../Aa" and ".../BB"), letting one URL's cached article stand
in for another's. State saved before the switch still carries those legacy ids:

  * records that store the URL next to the id (votes, Read Later, History,
    the previous digest) are re-keyed on load with `migrate_id`;
  * read markers (readIds) store only the id, so `is_read` also accepts the
    URL's legacy id.
"""
import hashlib
import re

_LEGACY_RE = re.compile(r'^[0-9a-f]{8}$')


def article_id(url):
    return hashlib.sha256((url or '').encode('utf-8')).hexdigest()[:16]


def legacy_id(url):
    """The pre-SHA-256 id. Only for honouring old state, never for identity."""
    h = 0
    for ch in url or '':
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return format(h, '08x')


def is_legacy(aid):
    return isinstance(aid, str) and bool(_LEGACY_RE.match(aid))


def migrate_id(aid, url):
    """The current id for a record saved as (aid, url): legacy ids whose URL
    still hashes to them are upgraded; anything else is returned unchanged."""
    if is_legacy(aid) and url and legacy_id(url) == aid:
        return article_id(url)
    return aid


def is_read(aid, url, read_ids):
    return aid in read_ids or (bool(url) and legacy_id(url) in read_ids)
