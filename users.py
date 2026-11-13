#!/usr/bin/env python3
"""User accounts, sessions, and per-user data paths.

Cruxwire is multi-user when AUTH_MODE is set:

    AUTH_MODE=none   (default) today's single-user behaviour: no login, every
                     request acts as the one implicit user.
    AUTH_MODE=local  username/password accounts, cookie sessions, an admin who
                     manages users. Each user has private feeds, categories,
                     likes/dislikes, read state, personal ranking settings and
                     their own digest. Add OIDC_* settings to also offer SSO.
    AUTH_MODE=oidc   the same accounts, with OpenID Connect SSO required to be
                     configured (see oidc.py). LOCAL_LOGIN decides whether
                     passwords still work: on | admins (break-glass) | off.

Storage (plain JSON on the data volume, like everything else):

    /data/users.json       accounts  {"users": [{id, username, ...}]}
    /data/sessions.json    sessions  {sha256(token): {uid, created, expires}}
    /data/users/<uid>/     that user's state/feeds/categories/preferences/digest

The *owner* account (the first admin, created on upgrade) keeps using the
pre-multi-user files directly in /data, so switching AUTH_MODE on needs no data
migration and switching it back off returns you to exactly your own data.

Who "the current user" is travels in a thread-local: the HTTP handler sets it
per request (`as_user`), the pipeline sets it while building each user's
digest, and `path()` resolves a per-user file for whoever is current.
Background threads started on a user's behalf must use `spawn()` so the
identity follows them.
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

AUTH_MODES = ('none', 'local', 'oidc')
AUTH_MODE = (os.environ.get('AUTH_MODE', 'none') or 'none').strip().lower()
if AUTH_MODE not in AUTH_MODES:
    raise SystemExit(f'AUTH_MODE must be one of {", ".join(AUTH_MODES)} (got {AUTH_MODE!r})')

_STATE_FILE = os.environ.get('STATE_FILE', '/data/state.json')
DATA_DIR = os.environ.get('DATA_DIR') or os.path.dirname(_STATE_FILE) or '/data'
USERS_FILE = os.environ.get('USERS_FILE', os.path.join(DATA_DIR, 'users.json'))
SESSIONS_FILE = os.environ.get('SESSIONS_FILE', os.path.join(DATA_DIR, 'sessions.json'))
USERS_DIR = os.environ.get('USERS_DIR', os.path.join(DATA_DIR, 'users'))

SESSION_COOKIE = 'cw_session'
SESSION_TTL_S = int(float(os.environ.get('SESSION_TTL_DAYS', '30')) * 86400)
# Secure cookies: 'auto' marks them Secure when the request came in over HTTPS
# (directly or via X-Forwarded-Proto from a reverse proxy).
COOKIE_SECURE = (os.environ.get('COOKIE_SECURE', 'auto') or 'auto').strip().lower()
# Lax (default) blocks the cookie inside cross-site iframes. To show the app in
# a Home Assistant dashboard on another origin, use None (forces Secure, so it
# then only works over HTTPS).
COOKIE_SAMESITE = {'lax': 'Lax', 'strict': 'Strict', 'none': 'None'}.get(
    (os.environ.get('COOKIE_SAMESITE', 'lax') or 'lax').strip().lower(), 'Lax')

# Password sign-in: on (everyone) | admins (break-glass only, typical with SSO)
# | off. Default: on with AUTH_MODE=local, admins with AUTH_MODE=oidc.
LOCAL_LOGIN = (os.environ.get('LOCAL_LOGIN') or ('admins' if AUTH_MODE == 'oidc' else 'on')).strip().lower()
if LOCAL_LOGIN not in ('on', 'admins', 'off'):
    raise SystemExit(f'LOCAL_LOGIN must be on, admins or off (got {LOCAL_LOGIN!r})')

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '').strip()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')

PBKDF2_ITERATIONS = int(os.environ.get('PBKDF2_ITERATIONS', '310000'))
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 256
ROLES = ('admin', 'user')
_USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._@-]{1,63}$')
_UID_RE = re.compile(r'^[a-z0-9]{6,32}$')

# Login throttling: after LOGIN_MAX_FAILURES failures for a username or client
# within LOGIN_WINDOW_S, further attempts are refused until the window passes.
LOGIN_MAX_FAILURES = int(os.environ.get('LOGIN_MAX_FAILURES', '8'))
LOGIN_WINDOW_S = int(os.environ.get('LOGIN_WINDOW_S', '900'))

# The single implicit user when AUTH_MODE=none (and the identity background
# work uses when nobody in particular is current). Uses the legacy files.
OWNER_PSEUDO = {'id': 'owner', 'username': 'owner', 'display_name': 'Owner',
                'role': 'admin', 'legacy': True, 'disabled': False}

_lock = threading.RLock()
_ctx = threading.local()
_failures = {}          # key -> [timestamps]
_failures_lock = threading.Lock()


def enabled():
    """True when accounts/login are in force."""
    return AUTH_MODE != 'none'


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _atomic_write(path, obj):
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.users-', suffix='.json', dir=d)
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


def _read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception:
        return default


# ──────────────────────────────────────────────────────────────────────
# Current-user context + per-user file paths
# ──────────────────────────────────────────────────────────────────────
def current():
    """The user record whose data is being read/written right now. Defaults to
    the owner (legacy files), which is also every request in AUTH_MODE=none."""
    return getattr(_ctx, 'user', None) or OWNER_PSEUDO


def current_id():
    return current()['id']


@contextlib.contextmanager
def as_user(user):
    """Run a block as `user` (a user record). Nests and restores."""
    prev = getattr(_ctx, 'user', None)
    _ctx.user = user
    try:
        yield user
    finally:
        _ctx.user = prev


def spawn(target, *args, name=None, **kwargs):
    """Start a daemon thread that runs as the current user."""
    user = current()

    def _run():
        with as_user(user):
            target(*args, **kwargs)
    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()
    return t


def user_dir(uid):
    if not _UID_RE.match(uid or ''):
        raise ValueError('invalid user id')
    return os.path.join(USERS_DIR, uid)


def path(legacy_path, user=None):
    """Resolve a per-user data file. `legacy_path` is the single-user location
    (e.g. STATE_FILE); the owner keeps using it, everyone else gets the same
    file name inside their own /data/users/<uid>/ directory."""
    user = user or current()
    if user.get('legacy'):
        return legacy_path
    return os.path.join(user_dir(user['id']), os.path.basename(legacy_path))


# ──────────────────────────────────────────────────────────────────────
# Passwords
# ──────────────────────────────────────────────────────────────────────
def hash_password(password, iterations=None):
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}'


def verify_password(password, encoded):
    try:
        algo, iters, salt_hex, hash_hex = (encoded or '').split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# Constant-ish work for unknown usernames so timing doesn't reveal which exist.
_DUMMY_HASH = None


def _dummy_verify(password):
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password('not-a-real-password')
    verify_password(password, _DUMMY_HASH)


def check_password_policy(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f'Password must be at least {MIN_PASSWORD_LEN} characters.')
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError('Password is too long.')


# ──────────────────────────────────────────────────────────────────────
# Accounts
# ──────────────────────────────────────────────────────────────────────
def _load():
    data = _read_json(USERS_FILE, {})
    users = data.get('users') if isinstance(data, dict) else None
    return [u for u in (users or []) if isinstance(u, dict) and _UID_RE.match(u.get('id') or '')]


def _save(users):
    _atomic_write(USERS_FILE, {'version': 1, 'users': users})


def public(user):
    """A user record without secrets, for the API."""
    if not user:
        return None
    out = {k: user.get(k) for k in ('id', 'username', 'display_name', 'role', 'disabled',
                                    'created_at', 'last_login_at', 'legacy', 'email')}
    out['sso'] = bool(user.get('oidc_sub'))
    out['has_password'] = bool(user.get('password_hash'))
    return out


def list_users():
    with _lock:
        return _load()


def count():
    return len(list_users())


def get(uid):
    for u in list_users():
        if u['id'] == uid:
            return u
    return None


def by_username(username):
    username = (username or '').strip().lower()
    for u in list_users():
        if u.get('username') == username:
            return u
    return None


def active_users():
    """Users whose digests the pipeline should build. In AUTH_MODE=none that
    is just the implicit owner."""
    if not enabled():
        return [OWNER_PSEUDO]
    users = [u for u in list_users() if not u.get('disabled')]
    return users or [OWNER_PSEUDO]


def normalize_username(username):
    username = (username or '').strip().lower()
    if not _USERNAME_RE.match(username):
        raise ValueError('Username must be 2-64 characters: letters, digits, . _ @ -')
    return username


def _insert(username, role, display_name, unique_username=False, **extra):
    """Add an account record. The first account ever created becomes the
    owner of the existing single-user data (and is always an admin)."""
    if role not in ROLES:
        raise ValueError('Role must be admin or user.')
    with _lock:
        users = _load()
        taken = {u.get('username') for u in users}
        if username in taken:
            if not unique_username:
                raise ValueError('That username is taken.')
            base = username[:58]
            username = next(f'{base}-{i}' for i in range(2, 10000) if f'{base}-{i}' not in taken)
        first = not users
        user = {
            'id': secrets.token_hex(8),
            'username': username,
            'display_name': (display_name or '').strip()[:80] or username,
            'role': 'admin' if first else role,
            'password_hash': None,
            'disabled': False,
            'legacy': first,
            'created_at': _now_iso(),
            'last_login_at': None,
            **extra,
        }
        users.append(user)
        _save(users)
    if not user['legacy']:
        os.makedirs(user_dir(user['id']), exist_ok=True)
    return user


def create_user(username, password, role='user', display_name=None):
    """Create a password account (see _insert for the first-account rule)."""
    username = normalize_username(username)
    check_password_policy(password)
    if role not in ROLES:
        raise ValueError('Role must be admin or user.')
    return _insert(username, role, display_name, password_hash=hash_password(password))


# ── SSO-linked accounts (see oidc.py) ──────────────────────────────────
def _username_from(raw):
    """A valid local username derived from an IdP username/email/sub."""
    s = re.sub(r'[^a-z0-9._@-]+', '-', (raw or '').strip().lower()).strip('-._@')
    if len(s) < 2:
        s = 'user-' + secrets.token_hex(3)
    return s[:64] if _USERNAME_RE.match(s[:64]) else 'user-' + secrets.token_hex(3)


def by_oidc(issuer, sub):
    for u in list_users():
        if u.get('oidc_sub') == sub and u.get('oidc_iss') == issuer:
            return u
    return None


def by_email(email):
    email = (email or '').strip().lower()
    if not email:
        return None
    matches = [u for u in list_users() if (u.get('email') or '').lower() == email]
    return matches[0] if len(matches) == 1 else None


def create_oidc_user(issuer, sub, preferred, role='user', display_name=None, email=None):
    return _insert(_username_from(preferred), role, display_name, unique_username=True,
                   oidc_iss=issuer, oidc_sub=sub,
                   email=(email or '').strip().lower()[:200] or None)


def link_oidc(uid, issuer, sub):
    with _lock:
        users = _load()
        for u in users:
            if u['id'] == uid:
                u['oidc_iss'], u['oidc_sub'] = issuer, sub
        _save(users)


def unlink_oidc(uid):
    with _lock:
        users = _load()
        for u in users:
            if u['id'] == uid:
                u.pop('oidc_iss', None)
                u.pop('oidc_sub', None)
        _save(users)


def sync_oidc_profile(uid, profile, role=None):
    """After an SSO login: refresh name/email, apply the IdP-managed role
    (never demoting the last enabled admin), and stamp last_login_at."""
    with _lock:
        users = _load()
        user = next((u for u in users if u['id'] == uid), None)
        if user is None:
            raise KeyError(uid)
        if profile.get('display_name'):
            user['display_name'] = str(profile['display_name']).strip()[:80]
        if profile.get('email'):
            user['email'] = str(profile['email']).strip().lower()[:200]
        if role in ROLES and role != user.get('role'):
            prev = user.get('role')
            user['role'] = role
            if not _admins(users):
                user['role'] = prev
        user['last_login_at'] = _now_iso()
        _save(users)
    return user


def password_login_allowed(user):
    """LOCAL_LOGIN policy for an account that has a password."""
    if LOCAL_LOGIN == 'off':
        return False
    if LOCAL_LOGIN == 'admins':
        return user.get('role') == 'admin'
    return True


def _admins(users):
    return [u for u in users if u.get('role') == 'admin' and not u.get('disabled')]


def update_user(uid, patch):
    """Admin edit: display_name, role, disabled, password. Refuses changes that
    would leave no enabled admin."""
    with _lock:
        users = _load()
        user = next((u for u in users if u['id'] == uid), None)
        if user is None:
            raise KeyError(uid)
        before = dict(user)
        if 'display_name' in patch:
            user['display_name'] = str(patch['display_name'] or '').strip()[:80] or user['username']
        if 'role' in patch:
            if patch['role'] not in ROLES:
                raise ValueError('Role must be admin or user.')
            user['role'] = patch['role']
        if 'disabled' in patch:
            user['disabled'] = bool(patch['disabled'])
        if patch.get('password'):
            check_password_policy(patch['password'])
            user['password_hash'] = hash_password(patch['password'])
        if not _admins(users):
            user.clear()
            user.update(before)
            raise ValueError('At least one enabled admin is required.')
        _save(users)
    if user.get('disabled') or patch.get('password'):
        revoke_sessions(uid)
    return user


def set_password(uid, current_password, new_password):
    user = get(uid)
    if user is None:
        raise KeyError(uid)
    if not verify_password(current_password or '', user.get('password_hash')):
        raise PermissionError('Current password is incorrect.')
    return update_user(uid, {'password': new_password})


def delete_user(uid):
    """Delete an account and its private data. The owner account (which holds
    the original single-user data) cannot be deleted, only disabled."""
    with _lock:
        users = _load()
        user = next((u for u in users if u['id'] == uid), None)
        if user is None:
            raise KeyError(uid)
        if user.get('legacy'):
            raise ValueError('The owner account holds the original data and cannot be deleted. '
                             'Disable it instead.')
        rest = [u for u in users if u['id'] != uid]
        if not _admins(rest):
            raise ValueError('At least one enabled admin is required.')
        _save(rest)
    revoke_sessions(uid)
    shutil.rmtree(user_dir(uid), ignore_errors=True)
    return user


def authenticate(username, password):
    """Return the user for valid credentials, else None. Disabled accounts
    never authenticate."""
    try:
        username = normalize_username(username)
    except ValueError:
        _dummy_verify(password or '')
        return None
    user = by_username(username)
    if user is None or not user.get('password_hash'):
        _dummy_verify(password or '')
        return None
    if not verify_password(password or '', user['password_hash']) or user.get('disabled'):
        return None
    if not password_login_allowed(user):
        return None
    with _lock:
        users = _load()
        for u in users:
            if u['id'] == user['id']:
                u['last_login_at'] = _now_iso()
        _save(users)
    return user


def bootstrap():
    """On start with auth enabled: create the first admin from ADMIN_USERNAME /
    ADMIN_PASSWORD if there are no accounts yet. Returns a log line or None.
    Without those env vars the UI offers a one-time setup form instead."""
    if not enabled() or count():
        return None
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        u = create_user(ADMIN_USERNAME, ADMIN_PASSWORD, 'admin')
        return f'created admin account {u["username"]!r} (owns the existing data)'
    return 'no accounts yet - open the app to create the first admin'


def needs_setup():
    return enabled() and count() == 0


# ──────────────────────────────────────────────────────────────────────
# Login throttling
# ──────────────────────────────────────────────────────────────────────
def throttled(*keys):
    now = time.monotonic()
    with _failures_lock:
        for k in keys:
            ts = [t for t in _failures.get(k, []) if now - t < LOGIN_WINDOW_S]
            _failures[k] = ts
            if len(ts) >= LOGIN_MAX_FAILURES:
                return True
    return False


def record_failure(*keys):
    now = time.monotonic()
    with _failures_lock:
        for k in keys:
            _failures.setdefault(k, []).append(now)
        if len(_failures) > 10000:      # bound memory under a username spray
            _failures.clear()


def clear_failures(*keys):
    with _failures_lock:
        for k in keys:
            _failures.pop(k, None)


# ──────────────────────────────────────────────────────────────────────
# Sessions (opaque random token in a cookie; only its hash is stored)
# ──────────────────────────────────────────────────────────────────────
def _token_key(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _load_sessions():
    d = _read_json(SESSIONS_FILE, {})
    return d if isinstance(d, dict) else {}


def create_session(uid):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        sessions = {k: s for k, s in _load_sessions().items()
                    if isinstance(s, dict) and s.get('expires', 0) > now}
        sessions[_token_key(token)] = {'uid': uid, 'created': now, 'expires': now + SESSION_TTL_S,
                                       'seen': now}
        _atomic_write(SESSIONS_FILE, sessions)
    return token


def session_user(token):
    """The (enabled) user a session token belongs to, or None. Sliding
    expiry: a session used within its lifetime is extended (at most hourly, to
    keep writes rare)."""
    if not token or len(token) > 200:
        return None
    key = _token_key(token)
    now = time.time()
    with _lock:
        sessions = _load_sessions()
        s = sessions.get(key)
        if not isinstance(s, dict) or s.get('expires', 0) <= now:
            return None
        user = get(s.get('uid'))
        if user is None or user.get('disabled'):
            return None
        if now - s.get('seen', 0) > 3600:
            s['seen'] = now
            s['expires'] = now + SESSION_TTL_S
            _atomic_write(SESSIONS_FILE, sessions)
    return user


def revoke_session(token):
    if not token:
        return
    key = _token_key(token)
    with _lock:
        sessions = _load_sessions()
        if sessions.pop(key, None) is not None:
            _atomic_write(SESSIONS_FILE, sessions)


def revoke_sessions(uid):
    with _lock:
        sessions = _load_sessions()
        kept = {k: s for k, s in sessions.items() if not (isinstance(s, dict) and s.get('uid') == uid)}
        if len(kept) != len(sessions):
            _atomic_write(SESSIONS_FILE, kept)
