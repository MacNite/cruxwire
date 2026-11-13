# Security Policy

Cruxwire is a **self-hosted, single-user** application with **no authentication**. Its security model
is "the network is trusted" - it is built to run on a private network (a homelab LAN, a Tailscale /
WireGuard tailnet, or `localhost`), **not** on the public internet.

## Threat model - read this before you deploy

- **No login, no authorization.** Every endpoint - including the ones that change settings, feeds, and
  categories and trigger pipeline runs - is reachable by anyone who can reach the port. **Do not expose
  port 8090 directly to the internet.** For remote access, put it behind a reverse proxy that adds
  authentication (Caddy/nginx basic auth, [Authelia](https://www.authelia.com),
  [Tailscale](https://tailscale.com), Cloudflare Access, …).
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
