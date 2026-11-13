# Security Policy

Cruxwire is a **self-hosted** application. By default (`AUTH_MODE=none`) it is **single-user with no
authentication** and its security model is "the network is trusted" - it is built to run on a private
network (a homelab LAN, a Tailscale / WireGuard tailnet, or `localhost`), **not** on the public
internet. With `AUTH_MODE=local` it has user accounts (see the README's *User accounts* section).

## Threat model - read this before you deploy

- **No login, no authorization (default `AUTH_MODE=none`).** Every endpoint - including the ones that change settings, feeds, and
  categories and trigger pipeline runs - is reachable by anyone who can reach the port. **Do not expose
  port 8090 directly to the internet.** For remote access, put it behind a reverse proxy that adds
  authentication (Caddy/nginx basic auth, [Authelia](https://www.authelia.com),
  [Tailscale](https://tailscale.com), Cloudflare Access, …).
- **With accounts (`AUTH_MODE=local`).** Every endpoint except the login page and static icons needs a
  session. Sessions are random tokens in an `HttpOnly`, `SameSite=Lax` cookie (only their SHA-256 is
  stored), passwords are PBKDF2-SHA256, failed logins are throttled per username and client address,
  and global settings / user management are admin-only. The app itself serves plain HTTP - terminate
  TLS in a reverse proxy before exposing it beyond a trusted network. Users are isolated from each
  other's data, but all of them share the model, the fetch pipeline and the server, so any user can
  trigger refreshes and add feeds the server will fetch.
- **Single sign-on (OIDC).** Authorization-code flow with PKCE, a `state` bound to the browser by a
  short-lived cookie, and a `nonce`; the ID token's signature (RS256 via the provider's JWKS, or
  HS256 with the client secret), issuer, audience and expiry are verified before a session is
  created. Redirect targets after login are restricted to same-site paths. An SSO identity never
  takes over an existing account unless `OIDC_LINK_BY` allows it, and email linking requires a
  verified address. With `AUTH_MODE=oidc`, passwords default to admins-only break-glass
  (`LOCAL_LOGIN`). The identity provider is contacted directly (not through the SSRF guard), since
  its address comes from the deployment config.
- **CSRF guard, no rate limiting beyond refresh.** Mutating requests with a cross-site `Origin` are
  refused and CORS is off by default, but anything that can reach the port without a browser can still
  call the API. Manual refreshes can't overlap (409) and have a cooldown (429).
- **It fetches URLs you give it - through an SSRF guard.** Feeds, article pages, preview images and
  seed URLs are fetched via `netsafe.py`: http(s) only, private/loopback/link-local/reserved addresses
  refused (DNS and connected-peer checks, every redirect re-validated), with timeouts and byte caps.
  Behind an outbound proxy that resolves DNS itself, hostname checks fall back to the proxy. Still,
  don't give the container network access to internal services it has no reason to reach.
- **Runtime data is stored unencrypted** on the Docker volume (read history, Read Later, learned source
  preferences). Protect and back up the volume accordingly.

See the README's [Security & deployment](README.md#security--deployment) section for the full rundown.

## Supported versions

This is a personal open-source project with a rolling release - fixes land on `main`. There are no
maintained release branches; run the latest `main`.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue:

- Use GitHub's **Private vulnerability reporting** on this repository: **Security → Report a
  vulnerability** (<https://github.com/philoking/cruxwire/security/advisories/new>).

Issues that require an attacker to already be on your trusted network (e.g. "the API has no auth") are
**known and by design** - see the threat model above. Reports of problems *outside* that model are very
welcome, for example: path traversal, an SSRF beyond the documented feed-fetch behaviour, a way to
escape the data directory, or anything that lets a malicious **feed** compromise the host.

There's no formal SLA, but I'll make a good-faith effort to respond.
