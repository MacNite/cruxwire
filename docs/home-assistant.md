# Cruxwire in Home Assistant

Cruxwire's compact view is a single-column, touch-friendly feed designed for phones and for the
Home Assistant **Webpage** card (an iframe).

```
http://<cruxwire-host>:8090/?view=compact
```

It shows each story with a 16:9 picture (or a local illustration), headline, publisher/time, summary,
an optional *"Why this story?"* line, and ▲ Like / ▼ Dislike / Save / Hide / Open ↗ actions with 44px
tap targets. **Refresh feeds** runs the normal pipeline (it never starts a second run while one is
active), and **Discover more** pages further down the same ranking without repeating stories. The
compact view always opens on Home, even if you last used Settings on another device.

## Dashboard card

```yaml
type: iframe            # "Webpage" card in the UI editor
url: https://news.home.example/?view=compact
aspect_ratio: 160%      # tall card; or use a panel/sidebar view for full height
title: News
```

For a full-screen experience add it as a **panel view** (Dashboard → Edit → Add view → View type:
Panel) containing just the Webpage card, or as a sidebar entry via the `panel_iframe`-style
"Webpage" dashboard (Settings → Dashboards → Add dashboard → Webpage).

## HTTPS and mixed content

Browsers refuse to load an `http://` iframe inside an `https://` page. If Home Assistant is served
over HTTPS (including the Nabu Casa remote URL), serve Cruxwire over HTTPS too - e.g. behind the same
reverse proxy (Caddy, nginx, Traefik, the NGINX add-on) on its own subdomain.

Remote images in the feed are loaded by the browser directly from publishers; some are `http://` only
and will be blocked inside an HTTPS page. Cruxwire swaps any image that fails to load for its local
topic illustration, so cards never show a broken image.

## Framing policy

Cruxwire allows framing by default. To allow *only* your Home Assistant to embed it, set:

```bash
FRAME_ANCESTORS="'self' https://ha.home.example"
```

This adds `Content-Security-Policy: frame-ancestors …`. If your reverse proxy adds
`X-Frame-Options: DENY/SAMEORIGIN` (many hardened defaults do), remove it for this host or the card
stays blank.

## Keep it private

Cruxwire has no login. Keep it on your LAN/VPN (Tailscale, WireGuard) or behind an authenticated
reverse proxy. Note that an auth proxy in front of an iframe needs a session the browser already has
(e.g. log in to the proxy once in the same browser/app); forward-auth with cookies scoped to your
domain works well, HTTP basic auth prompts inside iframes are unreliable on mobile.

Cross-site write requests are refused by Cruxwire's Origin check, so a page on another site can't
vote or trigger refreshes through your browser.

## Checklist (real phone)

- [ ] Card scrolls smoothly inside the HA companion app; no horizontal scrolling.
- [ ] ▲ / ▼ / Save / Hide respond on first tap; a liked story stays visible (votes never hide).
- [ ] **Open ↗** opens the publisher page (the companion app opens external links in the browser).
- [ ] Back navigation returns to the dashboard.
- [ ] Works on the remote (HTTPS) URL as well as on the LAN.
