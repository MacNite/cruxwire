#!/usr/bin/env python3
"""SSRF-safe outbound HTTP for Cruxwire (pure stdlib).

Every URL Cruxwire fetches on behalf of user input — feed URLs, the article
pages those feeds link to, og:image lookups, seed-article imports, TL;DR page
fetches — goes through `fetch()`. It enforces:

  * http(s) only, with a host; no credentials in the URL;
  * DNS resolution checked *before* connecting: every resolved address must be
    public (no loopback / private / link-local / CGNAT / multicast / reserved /
    unspecified, IPv4 or IPv6, incl. IPv4-mapped IPv6);
  * the *actual* peer address re-checked after the socket connects, so a DNS
    rebinding between the check and the connect is still caught;
  * every redirect hop re-validated the same way (max MAX_REDIRECTS);
  * connect/read timeouts and a hard byte limit on the body.

Self-hosters who deliberately want LAN feeds (e.g. a FreshRSS box on the same
network) can set ALLOW_PRIVATE_FETCH=1. Calls to the Ollama host are trusted
deploy-time config and do not use this module.
"""
import http.client
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = 'Mozilla/5.0 Cruxwire (news aggregator)'
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 10
DEFAULT_LIMIT = 2 * 1024 * 1024   # 2 MiB hard cap unless the caller asks for less

ALLOW_PRIVATE = os.environ.get('ALLOW_PRIVATE_FETCH', '').strip().lower() in ('1', 'true', 'yes')


class BlockedURL(Exception):
    """The URL (or a redirect / resolved address) is not allowed."""


def is_ip_allowed(ip_str):
    """True if an IP address is a public, routable unicast address."""
    if ALLOW_PRIVATE:
        return True
    try:
        ip = ipaddress.ip_address(ip_str.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        # CGNAT (100.64/10) and 0/8 aren't covered by is_private on every Python.
        if ip in ipaddress.ip_network('100.64.0.0/10') or ip in ipaddress.ip_network('0.0.0.0/8'):
            return False
    else:
        if ip in ipaddress.ip_network('64:ff9b::/96') or ip in ipaddress.ip_network('2002::/16'):
            # NAT64 / 6to4 can tunnel to private v4 space.
            return False
    return getattr(ip, 'is_global', True)


def _resolve(host, port):
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def check_url(url):
    """Validate a URL's syntax and resolved addresses. Returns the parsed URL.
    Raises BlockedURL with a human-readable reason."""
    if not isinstance(url, str) or not url.strip():
        raise BlockedURL('URL is required')
    url = url.strip()
    if len(url) > 2048:
        raise BlockedURL('URL is too long')
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https'):
        raise BlockedURL('Only http(s) URLs are allowed')
    if not p.hostname:
        raise BlockedURL('URL must include a host')
    if p.username or p.password:
        raise BlockedURL('URLs with embedded credentials are not allowed')
    try:
        port = p.port or (443 if p.scheme == 'https' else 80)
    except ValueError:
        raise BlockedURL('Invalid port')
    try:
        addrs = _resolve(p.hostname, port)
    except (socket.gaierror, UnicodeError) as exc:
        raise BlockedURL(f'Could not resolve host: {p.hostname} ({exc})')
    if not addrs:
        raise BlockedURL(f'Could not resolve host: {p.hostname}')
    for a in addrs:
        if not is_ip_allowed(a):
            raise BlockedURL(f'Destination address not allowed ({p.hostname})')
    return p


def _guarded_create_connection(address, *args, **kwargs):
    """socket.create_connection, then verify the real peer address."""
    sock = socket.create_connection(address, *args, **kwargs)
    try:
        peer = sock.getpeername()[0]
    except OSError:
        sock.close()
        raise
    if not is_ip_allowed(peer):
        sock.close()
        raise BlockedURL('Destination address not allowed (connected peer)')
    return sock


class _SafeHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = _guarded_create_connection


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = _guarded_create_connection


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SafeHTTPSConnection, req, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        check_url(newurl)   # raises BlockedURL → surfaces to the caller
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _proxy_configured(scheme):
    return bool(urllib.request.getproxies().get(scheme))


def _opener(scheme):
    handlers = [_SafeRedirectHandler()]
    if _proxy_configured(scheme):
        # Behind an outbound proxy the socket peer is the proxy itself, so the
        # peer check can't apply; the pre-connect DNS check (and per-hop
        # redirect check) still does.
        handlers.append(urllib.request.ProxyHandler())
    else:
        handlers += [urllib.request.ProxyHandler({}), _SafeHTTPHandler(), _SafeHTTPSHandler()]
    return urllib.request.build_opener(*handlers)


class Response:
    __slots__ = ('url', 'status', 'headers', 'body', 'truncated')

    def __init__(self, url, status, headers, body, truncated):
        self.url, self.status, self.headers = url, status, headers
        self.body, self.truncated = body, truncated

    @property
    def content_type(self):
        return (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()

    def text(self):
        charset = 'utf-8'
        ct = self.headers.get('Content-Type') or ''
        if 'charset=' in ct:
            charset = ct.split('charset=', 1)[1].split(';')[0].strip().strip('"') or 'utf-8'
        try:
            return self.body.decode(charset, errors='replace')
        except LookupError:
            return self.body.decode('utf-8', errors='replace')


def fetch(url, timeout=DEFAULT_TIMEOUT, limit=DEFAULT_LIMIT, accept='*/*', method='GET'):
    """Fetch a URL safely. Returns a Response. Raises BlockedURL for policy
    violations and urllib.error.URLError / HTTPError / OSError for network
    failures (HTTP errors >= 400 raise HTTPError, as with urllib)."""
    p = check_url(url)
    req = urllib.request.Request(url.strip(), method=method, headers={
        'User-Agent': USER_AGENT,
        'Accept': accept,
        'Accept-Language': 'en,*;q=0.5',
    })
    limit = max(1, int(limit or DEFAULT_LIMIT))
    try:
        with _opener(p.scheme).open(req, timeout=timeout) as resp:
            body = resp.read(limit + 1) if method != 'HEAD' else b''
            truncated = len(body) > limit
            return Response(resp.geturl(), getattr(resp, 'status', resp.getcode()),
                            resp.headers, body[:limit], truncated)
    except urllib.error.URLError as exc:
        # A BlockedURL raised inside a handler arrives wrapped in URLError.
        if isinstance(getattr(exc, 'reason', None), BlockedURL):
            raise exc.reason
        raise


def get_text(url, timeout=DEFAULT_TIMEOUT, limit=DEFAULT_LIMIT, accept='*/*'):
    """Convenience: decoded body text, or '' on any failure (blocked or not)."""
    try:
        return fetch(url, timeout=timeout, limit=limit, accept=accept).text()
    except Exception:
        return ''
