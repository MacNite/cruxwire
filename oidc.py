#!/usr/bin/env python3
"""OpenID Connect single sign-on (tested against authentik; any standard
OIDC provider works).

Authorization-code flow with PKCE (S256), `state` bound to the browser by a
short-lived cookie, and `nonce` checked in the ID token. The ID token
signature is verified with the stdlib only: RS256/RS384/RS512 against the
provider's JWKS (authentik's default when a signing key is set), or HS256/384/512
with the client secret (authentik when no signing key is selected).

Configuration (env; OIDC is on when OIDC_ISSUER is set and accounts are on):

    OIDC_ISSUER          issuer URL, e.g. https://auth.example.com/application/o/cruxwire/
    OIDC_CLIENT_ID       client id of the authentik OAuth2/OpenID provider
    OIDC_CLIENT_SECRET   client secret (confidential client)
    OIDC_REDIRECT_URI    https://cruxwire.example.com/auth/oidc/callback
                         (default: PUBLIC_URL + /auth/oidc/callback, else
                         derived from the request's Host / X-Forwarded-*)
    OIDC_SCOPES          default "openid profile email"
    OIDC_ADMIN_GROUP     members of this group are admins, everyone else a
                         plain user (synced at each login). Unset = roles are
                         managed in the app.
    OIDC_GROUPS_CLAIM    default "groups"
    OIDC_USERNAME_CLAIM  default "preferred_username"
    OIDC_AUTO_CREATE     create an account on first SSO login (default true)
    OIDC_LINK_BY         link a first SSO login to an existing local account
                         with the same "username" or "email" (verified); default
                         "none"
    OIDC_BUTTON_LABEL    default "Sign in with SSO"

The provider is reached with plain urllib (not the SSRF guard): its URLs come
from the deploy config, and an identity provider on the LAN is the normal case.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import users

OIDC_ISSUER = (os.environ.get('OIDC_ISSUER') or '').strip()
OIDC_CLIENT_ID = (os.environ.get('OIDC_CLIENT_ID') or '').strip()
OIDC_CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET') or ''
OIDC_REDIRECT_URI = (os.environ.get('OIDC_REDIRECT_URI') or '').strip()
PUBLIC_URL = (os.environ.get('PUBLIC_URL') or '').strip().rstrip('/')
OIDC_SCOPES = (os.environ.get('OIDC_SCOPES') or 'openid profile email').strip()
OIDC_ADMIN_GROUP = (os.environ.get('OIDC_ADMIN_GROUP') or '').strip()
OIDC_GROUPS_CLAIM = (os.environ.get('OIDC_GROUPS_CLAIM') or 'groups').strip()
OIDC_USERNAME_CLAIM = (os.environ.get('OIDC_USERNAME_CLAIM') or 'preferred_username').strip()
OIDC_AUTO_CREATE = (os.environ.get('OIDC_AUTO_CREATE', 'true') or 'true').strip().lower() in ('1', 'true', 'yes')
OIDC_LINK_BY = (os.environ.get('OIDC_LINK_BY') or 'none').strip().lower()
OIDC_BUTTON_LABEL = (os.environ.get('OIDC_BUTTON_LABEL') or 'Sign in with SSO').strip()[:60]
OIDC_TIMEOUT = int(os.environ.get('OIDC_TIMEOUT', '10'))

CALLBACK_PATH = '/auth/oidc/callback'
STATE_COOKIE = 'cw_oidc'
PENDING_TTL_S = 600          # a login must finish within 10 minutes
CLOCK_SKEW_S = 120
DISCOVERY_TTL_S = 3600

_lock = threading.Lock()
_discovery = {'doc': None, 'at': 0.0}
_jwks = {'keys': None, 'at': 0.0}
_pending = {}               # state -> {verifier, nonce, next, created}


class OIDCError(Exception):
    """A login that must fail; the message is safe to show the user."""


def enabled():
    return users.enabled() and bool(OIDC_ISSUER and OIDC_CLIENT_ID)


def config_problems():
    """Startup check: a list of human-readable misconfigurations."""
    problems = []
    if users.AUTH_MODE == 'oidc' and not (OIDC_ISSUER and OIDC_CLIENT_ID):
        problems.append('AUTH_MODE=oidc needs OIDC_ISSUER and OIDC_CLIENT_ID')
    if OIDC_LINK_BY not in ('none', 'username', 'email'):
        problems.append('OIDC_LINK_BY must be none, username or email')
    return problems


# ──────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────
def _http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={'Accept': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=OIDC_TIMEOUT) as resp:
            return json.loads(resp.read(1024 * 1024).decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            body = json.loads(exc.read(64 * 1024).decode('utf-8'))
            detail = body.get('error_description') or body.get('error') or ''
        except Exception:
            pass
        raise OIDCError(f'Identity provider error ({exc.code}){": " + detail if detail else ""}')
    except OIDCError:
        raise
    except Exception as exc:
        raise OIDCError(f'Could not reach the identity provider: {getattr(exc, "reason", exc)}')


def discovery(force=False):
    """The provider's /.well-known/openid-configuration (cached)."""
    with _lock:
        doc = _discovery['doc']
        if doc and not force and time.time() - _discovery['at'] < DISCOVERY_TTL_S:
            return doc
    url = OIDC_ISSUER.rstrip('/') + '/.well-known/openid-configuration'
    doc = _http_json(url)
    for k in ('authorization_endpoint', 'token_endpoint', 'issuer'):
        if not isinstance(doc.get(k), str):
            raise OIDCError(f'Provider discovery document has no {k}')
    with _lock:
        _discovery.update(doc=doc, at=time.time())
    return doc


def _jwk_set(force=False):
    with _lock:
        keys = _jwks['keys']
        if keys is not None and not force and time.time() - _jwks['at'] < DISCOVERY_TTL_S:
            return keys
    uri = discovery().get('jwks_uri')
    keys = _http_json(uri).get('keys', []) if uri else []
    with _lock:
        _jwks.update(keys=keys, at=time.time())
    return keys


# ──────────────────────────────────────────────────────────────────────
# JWT verification (stdlib only)
# ──────────────────────────────────────────────────────────────────────
def b64url_decode(s):
    s = s.encode('ascii') if isinstance(s, str) else s
    return base64.urlsafe_b64decode(s + b'=' * (-len(s) % 4))


def b64url_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


_HASHES = {'256': hashlib.sha256, '384': hashlib.sha384, '512': hashlib.sha512}
# ASN.1 DigestInfo prefixes for EMSA-PKCS1-v1_5 (RFC 8017 §9.2, note 1).
_DIGEST_INFO = {
    '256': bytes.fromhex('3031300d060960864801650304020105000420'),
    '384': bytes.fromhex('3041300d060960864801650304020205000430'),
    '512': bytes.fromhex('3051300d060960864801650304020305000440'),
}


def rsa_pkcs1_verify(n, e, message, signature, bits):
    """RSASSA-PKCS1-v1_5 verification. True iff `signature` is valid."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k or n.bit_length() < 2048 - 8:
        return False
    s = int.from_bytes(signature, 'big')
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, 'big')
    t = _DIGEST_INFO[bits] + _HASHES[bits](message).digest()
    if k < len(t) + 11:
        return False
    expected = b'\x00\x01' + b'\xff' * (k - len(t) - 3) + b'\x00' + t
    return hmac.compare_digest(em, expected)


def _verify_signature(header, signing_input, sig):
    alg = header.get('alg') or ''
    if alg[:2] == 'HS' and alg[2:] in _HASHES:
        if not OIDC_CLIENT_SECRET:
            raise OIDCError('ID token is HMAC-signed but OIDC_CLIENT_SECRET is not set')
        mac = hmac.new(OIDC_CLIENT_SECRET.encode('utf-8'), signing_input, _HASHES[alg[2:]]).digest()
        return hmac.compare_digest(mac, sig)
    if alg[:2] == 'RS' and alg[2:] in _HASHES:
        kid = header.get('kid')
        for attempt in (False, True):        # refresh the key set once on a miss (key rotation)
            keys = [k for k in _jwk_set(force=attempt)
                    if k.get('kty') == 'RSA' and k.get('use', 'sig') == 'sig'
                    and (kid is None or k.get('kid') == kid)]
            if keys:
                break
        for k in keys:
            try:
                n = int.from_bytes(b64url_decode(k['n']), 'big')
                e = int.from_bytes(b64url_decode(k['e']), 'big')
            except Exception:
                continue
            if rsa_pkcs1_verify(n, e, signing_input, sig, alg[2:]):
                return True
        return False
    raise OIDCError(f'Unsupported ID token signing algorithm {alg or "(none)"}. '
                    'In authentik, pick an RSA signing key (RS256) for the provider.')


def verify_id_token(token, nonce, issuer, now=None):
    """Validate an ID token and return its claims, or raise OIDCError."""
    try:
        h64, p64, s64 = token.split('.')
        header = json.loads(b64url_decode(h64))
        claims = json.loads(b64url_decode(p64))
        sig = b64url_decode(s64)
    except Exception:
        raise OIDCError('Malformed ID token')
    if not _verify_signature(header, f'{h64}.{p64}'.encode('ascii'), sig):
        raise OIDCError('ID token signature is invalid')
    now = time.time() if now is None else now
    if claims.get('iss') != issuer:
        raise OIDCError('ID token was issued by a different provider')
    aud = claims.get('aud')
    auds = aud if isinstance(aud, list) else [aud]
    if OIDC_CLIENT_ID not in auds:
        raise OIDCError('ID token is not meant for this client')
    if len(auds) > 1 and claims.get('azp') not in (None, OIDC_CLIENT_ID):
        raise OIDCError('ID token has an unexpected authorized party')
    try:
        if float(claims['exp']) < now - CLOCK_SKEW_S:
            raise OIDCError('ID token has expired')
        if float(claims.get('iat', now)) > now + CLOCK_SKEW_S:
            raise OIDCError('ID token is from the future (check the server clocks)')
    except (KeyError, TypeError, ValueError):
        raise OIDCError('ID token has no valid expiry')
    if not nonce or not hmac.compare_digest(str(claims.get('nonce') or ''), nonce):
        raise OIDCError('ID token nonce does not match this login')
    if not isinstance(claims.get('sub'), str) or not claims['sub']:
        raise OIDCError('ID token has no subject')
    return claims


# ──────────────────────────────────────────────────────────────────────
# Flow
# ──────────────────────────────────────────────────────────────────────
def redirect_uri(request_base=None):
    if OIDC_REDIRECT_URI:
        return OIDC_REDIRECT_URI
    base = PUBLIC_URL or (request_base or '').rstrip('/')
    return base + CALLBACK_PATH


def _safe_next(nxt):
    """Only same-site relative paths, never //host or schemes."""
    if not isinstance(nxt, str) or not nxt.startswith('/') or nxt.startswith('//') or '\\' in nxt:
        return '/'
    return nxt[:500]


def begin(request_base, next_path='/'):
    """Start a login. Returns (authorization URL, state for the cookie)."""
    doc = discovery()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = b64url_encode(hashlib.sha256(verifier.encode('ascii')).digest())
    ruri = redirect_uri(request_base)
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if now - v['created'] > PENDING_TTL_S]:
            _pending.pop(k, None)
        if len(_pending) > 1000:      # bound memory if someone hammers /login
            _pending.clear()
        _pending[state] = {'verifier': verifier, 'nonce': nonce, 'created': now,
                           'redirect_uri': ruri, 'next': _safe_next(next_path)}
    params = {
        'response_type': 'code', 'client_id': OIDC_CLIENT_ID, 'redirect_uri': ruri,
        'scope': OIDC_SCOPES, 'state': state, 'nonce': nonce,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    }
    sep = '&' if '?' in doc['authorization_endpoint'] else '?'
    return doc['authorization_endpoint'] + sep + urllib.parse.urlencode(params), state


def _exchange(code, pending):
    doc = discovery()
    form = {'grant_type': 'authorization_code', 'code': code,
            'redirect_uri': pending['redirect_uri'], 'code_verifier': pending['verifier'],
            'client_id': OIDC_CLIENT_ID}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if OIDC_CLIENT_SECRET:
        basic = base64.b64encode(
            f'{urllib.parse.quote(OIDC_CLIENT_ID, safe="")}:'
            f'{urllib.parse.quote(OIDC_CLIENT_SECRET, safe="")}'.encode('utf-8')).decode('ascii')
        headers['Authorization'] = 'Basic ' + basic
    tok = _http_json(doc['token_endpoint'], urllib.parse.urlencode(form).encode('ascii'), headers)
    id_token = tok.get('id_token')
    if not isinstance(id_token, str):
        raise OIDCError('The identity provider returned no ID token (is the "openid" scope enabled?)')
    claims = verify_id_token(id_token, pending['nonce'], doc['issuer'])
    # Optional enrichment from userinfo (authentik puts groups in the ID token
    # with the "profile" scope; userinfo is a fallback for providers that don't).
    if OIDC_ADMIN_GROUP and OIDC_GROUPS_CLAIM not in claims and doc.get('userinfo_endpoint') \
            and tok.get('access_token'):
        try:
            info = _http_json(doc['userinfo_endpoint'],
                              headers={'Authorization': 'Bearer ' + tok['access_token']})
            if info.get('sub') == claims['sub'] and OIDC_GROUPS_CLAIM in info:
                claims[OIDC_GROUPS_CLAIM] = info[OIDC_GROUPS_CLAIM]
        except OIDCError:
            pass
    return claims


def finish(params, cookie_state):
    """Complete a login from the callback query. Returns (user, next_path)."""
    if params.get('error'):
        desc = params.get('error_description') or params['error']
        raise OIDCError(f'Sign-in was not completed: {str(desc)[:200]}')
    state, code = params.get('state') or '', params.get('code') or ''
    if not state or not code:
        raise OIDCError('Missing code or state in the sign-in response')
    if not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise OIDCError('This sign-in was started in another browser or tab. Please try again.')
    with _lock:
        pending = _pending.pop(state, None)
    if pending is None or time.time() - pending['created'] > PENDING_TTL_S:
        raise OIDCError('This sign-in link has expired. Please try again.')
    claims = _exchange(code, pending)
    return user_for_claims(claims), pending['next']


def _groups(claims):
    g = claims.get(OIDC_GROUPS_CLAIM)
    if isinstance(g, str):
        return [g]
    return [x for x in g if isinstance(x, str)] if isinstance(g, list) else []


def user_for_claims(claims):
    """Find, link or create the local account for verified claims, and sync
    the admin role from OIDC_ADMIN_GROUP. Raises OIDCError when not allowed."""
    issuer, sub = claims['iss'], claims['sub']
    role = None
    if OIDC_ADMIN_GROUP:
        role = 'admin' if OIDC_ADMIN_GROUP in _groups(claims) else 'user'
    profile = {
        'display_name': claims.get('name') or claims.get(OIDC_USERNAME_CLAIM) or '',
        'email': claims.get('email') if claims.get('email_verified') is not False else None,
    }
    user = users.by_oidc(issuer, sub)
    if user is None and OIDC_LINK_BY != 'none':
        user = _link_candidate(claims)
        if user is not None:
            users.link_oidc(user['id'], issuer, sub)
    if user is None:
        if not OIDC_AUTO_CREATE:
            raise OIDCError('No Cruxwire account is linked to this identity. Ask an admin to add you.')
        user = users.create_oidc_user(issuer, sub, claims.get(OIDC_USERNAME_CLAIM) or claims.get('email') or sub,
                                      role or 'user', profile['display_name'], profile['email'])
    if user.get('disabled'):
        raise OIDCError('This account is disabled.')
    return users.sync_oidc_profile(user['id'], profile, role)


def _link_candidate(claims):
    if OIDC_LINK_BY == 'username':
        try:
            cand = users.by_username(users.normalize_username(claims.get(OIDC_USERNAME_CLAIM) or ''))
        except ValueError:
            return None
    else:   # email — only a verified address may claim an account
        email = (claims.get('email') or '').strip().lower()
        if not email or claims.get('email_verified') is not True:
            return None
        cand = users.by_email(email)
    # Never re-point an account that is already linked to another identity.
    if cand is not None and cand.get('oidc_sub'):
        return None
    return cand
