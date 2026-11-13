# Cruxwire

**The crux of the news, none of the noise.**
A self-hosted, local-AI news reader · **[cruxwire.app](https://cruxwire.app)**

[![CI](https://github.com/MacNite/cruxwire/actions/workflows/ci.yml/badge.svg)](https://github.com/MacNite/cruxwire/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A personal news dashboard in a **single container**. A background pipeline fetches RSS/Atom feeds,
uses a local LLM (via [Ollama](https://ollama.com)) to score, summarise, and de-duplicate articles,
clusters stories covered by multiple sources, learns what you actually read, and renders a ranked,
magazine-style digest. Read state (read / later / history) is shared across your devices.

<img width="1355" height="1125" alt="hero-screenshot" src="https://github.com/user-attachments/assets/c2c3cf38-5f7d-4631-b40d-0a699496e571" />

No build step, **no Python dependencies** (pure stdlib), no external services other than Ollama.

---

## Features

- **LLM-ranked digest** - every article gets a 0-10 relevance score, a 1-2 sentence summary, and a
  category from your local Ollama model.
- **Your own categories** - categories aren't hardcoded. Each carries an *interest description* you
  write (e.g. "Cooking - recipes, technique, equipment, restaurants"), and that sentence is woven into
  the scoring prompt - so "relevant" means what *you* actually care about. Editable in-app.
- **Source-agnostic de-duplication** - articles about the same story (across outlets) are clustered
  by embedding similarity and shown as one card, with the other coverage collapsed under it.
- **First-run onboarding** - a phone-friendly wizard: pick RSS feeds (or Google News searches) with
  instant validation and sample headlines, then rate image-preview cards (Like / Dislike / Skip) until
  you have 10 likes. Resume anytime; retrain from Settings → Personalize.
- **Explicit likes & dislikes** - ▲ / ▼ on every card. Each candidate is compared against *several* of
  your liked and disliked examples (not one averaged vector), so multiple interests coexist; boosts are
  calibrated and capped, publisher affinity is tracked separately, and ~25% of slots stay exploratory.
  A cheap "Why this story?" line names the liked story it resembles. Votes never mark anything read or
  saved, and Done (hide) is never a dislike.
- **Implicit personalization** - a per-source affinity multiplier and an embedding-based "taste" boost
  float the sources and topics you open/save upward, and sink ones you dismiss. Learned automatically.
- **Reliable previews** - every card shows a 16:9 picture: RSS media → inline image → publisher
  `og:image` → a local topic illustration. Never a broken image icon.
- **Compact phone / Home Assistant view** - `/?view=compact` is a single-column, touch-friendly feed
  with Refresh and Discover more. See [docs/home-assistant.md](docs/home-assistant.md).
- **CPU-friendly** - one model job at a time, a per-run budget of new articles to score (default 40),
  and already-scored articles / embeddings are reused instead of recomputed.
- **Sticky retention** - unread stories are carried forward across runs and pruned by a rank-weighted
  lifespan inside a floor/ceiling band, so the inbox never goes dry or floods.
- **Balanced magazine layout** - a hero + "Latest" grid with an "Earlier this week" rail that stays
  height-matched; dismissing promotes the next-ranked story so sections never sit empty.
- **On-demand TL;DR** - save an article to Read Later and the local model reads the full page and
  distills it to a few bullet points plus a one-line bottom line, generated in the background so it's
  ready when you come back. Regenerate anytime; the TL;DR text is searchable too.
- **Semantic search** - filter the Home view to stories *about* a topic by meaning, not just keyword
  match ("did you hear about X?"), powered by the same local embeddings. It's a filter, like the
  category chips - no cloud, no generated answers.
- **Blocklists** - literal title keywords plus a semantic "topics to avoid" filter, on top of a
  built-in deal/spam regex.
- **Runtime settings** - ranking, ingestion, schedule, retention, blocklist, and model choices are
  all editable in-app and apply on the next run, no restart.
- **Multi-device state** - read / Read Later / History sync through the server; the app is fully
  usable offline from local cache.
- **Optional user accounts** - `AUTH_MODE=local` adds login and an admin-managed user list; every
  user gets private feeds, categories, likes and their own ranked digest, while each article is
  still fetched and scored by the model only once. See [User accounts](#user-accounts).

---

## How it works

```
┌──────────────────── single container ─────────────────────┐
│  server.py    HTTP: UI, /digest.json, /state, /settings,  │
│               /feeds, /status, /refresh                   │
│  pipeline.py  background scheduler (cron-like):           │
│     fetch feeds → carry forward unread → score + embed    │
│     (Ollama) → cluster → retain → write digest.json       │
└───────────────────────────┬───────────────────────────────┘
                            │ HTTP
                      Ollama (OLLAMA_HOST)
       local volume /data → state.json, feeds.json,
                            digest.json, settings.json
```

**Pipeline** ([pipeline.py](pipeline.py)) runs on a schedule (default every 2h, 06:00-22:00) and on
start. Each run:

1. Fetches every feed, parses RSS/Atom, drops items older than `lookback_hours` and anything matching
   the blocklists, and de-dupes by URL.
2. **Carries forward** unread stories from the previous digest (so a story you didn't read doesn't
   vanish when its feed rotates it out); read stories are vacated, stale ones are cut.
3. Scores + summarises + embeds each fresh article via Ollama (carried stories reuse their score and
   only re-embed).
4. **Clusters** same-story coverage by cosine similarity and boosts a story by how many sources cover
   it.
5. **Retains** the pool to a rank-weighted, floor/ceiling-banded keep set.
6. Atomically writes `digest.json`.

**Frontend** ([digest.html](digest.html)) is vanilla JS - Home (magazine), Read Later, History, and
Settings views; category filters; light/dark; in-app feed management. It applies the per-device
source-affinity multiplier when ordering, and handles the layout balance, backfill, and promotion.

**State** ([server.py](server.py)) is served by the same process and pruned/retained server-side:
`readIds` persist (capped) so a dismissed story stays dismissed even after it's vacated from the
digest, History ages out after `history_retention_days`, Read Later is never aged, and `sourceStats`
persist independently so inbox hygiene never wipes your learned preferences.

See [TUNING.md](TUNING.md) for every adjustable knob.

---

## Quick start

```bash
cp .env.example .env          # then set OLLAMA_HOST to your Ollama server
docker compose pull           # prebuilt image from ghcr.io/macnite/cruxwire
docker compose up -d
```

Prefer building from source? Use `docker compose up -d --build` instead. Images are published to
GitHub Container Registry for `linux/amd64` and `linux/arm64`: `:latest` tracks `main`, and version
tags (`v1.2.3`) publish `:1.2.3`, `:1.2` and `:1`.

Open `http://<host>:8090/`.

> ⚠️ **By default there is no login.** Keep this on a trusted private network and don't expose port
> 8090 to the internet, or turn on [user accounts](#user-accounts) - see
> [Security & deployment](#security--deployment).

On first open a **setup wizard** walks you through choosing sources (the bundled
[feeds.sample.json](feeds.sample.json) list is pre-loaded and editable) and rating ~10 stories so the
first digest is already personalized; you can skip it and retrain later from **Settings →
Personalize**. Upgrading an existing install skips the wizard automatically. The pipeline generates
the digest in the background (on a CPU-only host give it a few minutes). Manage feeds in the UI's
**Feeds** screen, or trigger a refresh with `curl -X POST http://<host>:8090/refresh`.

On a phone or in Home Assistant use `http://<host>:8090/?view=compact` - see
[docs/home-assistant.md](docs/home-assistant.md).

### Requirements

- Docker + Docker Compose
- An [Ollama](https://ollama.com) server reachable over HTTP, with the models pulled:
  ```bash
  ollama pull qwen3.5:4b    # chat: score / summarise / categorise (CPU-friendly default)
  ollama pull nomic-embed-text    # embeddings: clustering / taste / semantic block
  ```

### Running on a laptop with Docker Desktop

Cruxwire runs fine on a personal Mac/Windows machine via Docker Desktop. One gotcha: if Ollama runs
**natively on the same machine**, set `OLLAMA_HOST` to **`http://host.docker.internal:11434`**, not
`localhost` - inside the container, `localhost` is the container itself, and `host.docker.internal` is
how Docker Desktop reaches your host. (On Linux that name isn't automatic: add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service, or point `OLLAMA_HOST` at the
host's LAN IP.)

> On macOS, run the **native Ollama app** - Docker Desktop's Linux VM can't use the Mac GPU, so
> running Ollama *inside* Docker would fall back to slow CPU. Cruxwire-in-Docker talking to a native
> Ollama is the right setup.

---

## User accounts

By default (`AUTH_MODE=none`) Cruxwire is single-user with no login, exactly as before. Set
`AUTH_MODE=local` to turn on accounts:

```yaml
    environment:
      AUTH_MODE: local
      # Optional: create the first admin on start instead of via the setup page.
      ADMIN_USERNAME: alice
      ADMIN_PASSWORD: change-me-please
```

- **First start** - with no accounts yet, the app shows a one-time *Create the admin account* page
  (or uses `ADMIN_USERNAME` / `ADMIN_PASSWORD`). That first admin **owns the existing data**: your
  current feeds, categories, likes and history stay theirs, with no migration. Switching back to
  `AUTH_MODE=none` returns you to that same data.
- **Everything personal is private per user** - feeds, categories (and their interest descriptions),
  likes/dislikes, onboarding, read/Read Later/History, keyword and topic blocklists, retention and
  personalization settings, and the ranked digest itself. New users start in the onboarding wizard.
- **Admins** manage users in *Settings → Users* (add, reset password, promote, disable, delete) and
  own the global settings: models, clustering, ingestion budget and schedule, plus the run log.
  Everyone can change their password and sign out in *Settings → Account*.
- **The model works once, not once per user.** Each run fetches the union of everyone's feeds once
  and scores/summarises/embeds each new article once. With accounts on, that LLM score is a
  reader-neutral *newsworthiness* score; each user's relevance is then
  `(1 - w) × newsworthiness + w × 10 × interest match`, where *interest match* is the embedding
  similarity to that user's category interests and `w` is their *Interest match weight*
  (Settings → Personalize). Likes/dislikes, taste and publisher affinity apply on top, per user.
  Adding users costs only cheap embedding math, not chat-model time.
- **Sessions** are random tokens in an `HttpOnly`, `SameSite=Lax` cookie (marked `Secure` behind
  HTTPS - set `X-Forwarded-Proto` in your proxy, or force it with `COOKIE_SECURE=true`). Only a hash
  of each token is stored. Passwords are PBKDF2-SHA256 hashed. Repeated failed logins are throttled
  per username and per client address. Disabling a user or changing a password signs out their
  sessions.
- **Home Assistant** - browsers don't send `SameSite=Lax` cookies inside a cross-site iframe. To
  embed the app in an HA dashboard on another origin, serve it over HTTPS and set
  `COOKIE_SAMESITE=none`.

Data layout with accounts: `/data/users.json` (accounts), `/data/sessions.json`, and
`/data/users/<id>/` for each user other than the owner (the owner keeps the original files in `/data`).

---

## Security & deployment

With the default `AUTH_MODE=none`, Cruxwire has **no authentication**. Every endpoint - including
the ones that change settings, feeds, and categories and trigger pipeline runs - is reachable by
anyone who can reach the port. That mode is built for a **single trusted user on a private network**:
a homelab LAN, a Tailscale/WireGuard tailnet, or `localhost`. With `AUTH_MODE=local` every endpoint
except the login page requires a signed-in user (see [User accounts](#user-accounts)).

**Do not expose port 8090 directly to the internet.** Even with accounts on, the app speaks plain
HTTP: for remote access, put it behind a TLS-terminating reverse proxy, and without accounts behind a
reverse proxy that adds authentication - Caddy/nginx with basic auth, [Authelia](https://www.authelia.com),
[Tailscale](https://tailscale.com), Cloudflare Access, etc.

Also worth knowing:

- **The pipeline fetches URLs you give it.** It requests every feed you add and, for TL;DRs, fetches
  the linked article pages. Treat your feed list as trusted input, and don't give the container
  network access to internal services it has no reason to reach.
- **Outbound fetches are SSRF-guarded.** Feed, article, image and seed-URL fetches (`netsafe.py`) are
  http(s)-only, refuse loopback/private/link-local/reserved destinations (checked at DNS time *and* on
  the connected socket), re-check every redirect, and cap time and size. Set `ALLOW_PRIVATE_FETCH=1`
  only if you deliberately subscribe to feeds on your LAN.
- **Cross-site requests are refused.** Mutating requests whose `Origin` doesn't match the host are
  rejected (CSRF guard); CORS is off unless you set `CORS_ALLOW_ORIGIN`. Request bodies are size-capped
  and manual refreshes can't overlap or queue (409 while running, 429 within a cooldown). Without
  accounts the trust model is "the network is trusted".
- **Embedding in Home Assistant** - framing is allowed by default; set `FRAME_ANCESTORS` (e.g.
  `'self' https://ha.example.lan`) to restrict which sites may embed the app.
- **Runtime data is stored unencrypted** on the Docker volume (read history, Read Later, learned
  source preferences). Back up - and protect - the volume accordingly.
- **The container runs unprivileged.** The entrypoint starts as root only to hand `/data` (and the
  optional corpus directory) to the `cruxwire` user (uid/gid 1000), then drops privileges. Volumes
  written by older images are re-owned automatically on first start.
- **Slow clients are bounded.** Each connection gets `REQUEST_TIMEOUT_S` (default 30) seconds of
  inactivity before it is dropped, and at most `MAX_CONNECTIONS` (default 64) requests are served at
  once. Outbound fetches also have a total deadline, not just a per-read timeout.
- **Bind address.** Compose publishes the port on all host interfaces. To keep it on the host only,
  change the mapping to `"127.0.0.1:${PORT:-8090}:8090"`. Running outside Docker, set `BIND_ADDR`
  (default `0.0.0.0`). With `AUTH_MODE=none` the server logs a warning at startup as a reminder.

---

## Configuration & tuning

Two tiers:

- **Settings UI** - ranking, ingestion, schedule, retention, blocklist, and model choices are edited
  live in the **Settings** view, persisted to `settings.json` on the volume, and applied on the next
  run **without a restart**.
- **Deploy env** - infrastructure wiring (`OLLAMA_HOST`, `PORT`, file paths, timeouts) is set in
  `docker-compose.yaml` / `.env` and needs a container restart. Env vars also seed the **defaults**
  for every Settings knob.

A few common knobs:

| Var / setting | Default | Purpose |
|-----|---------|---------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama base URL (chat + embeddings) |
| Chat / Embedding model | `qwen3:8b` / `nomic-embed-text` | Ollama models |
| Merge similarity (`SIM_THRESHOLD`) | `0.74` | Cosine threshold to collapse same-topic articles |
| Lookback (`LOOKBACK_HOURS`) | `36` | How far back fresh items are first discovered |
| Retention floor / ceiling (`RETAIN_FLOOR`/`RETAIN_CEILING`) | `25` / `60` | Keep at least / at most this many unread stories |
| Schedule (`ACTIVE_START_HOUR`/`ACTIVE_END_HOUR`/`INTERVAL_HOURS`) | `6` / `22` / `2` | When runs fire |
| `TZ` | `America/Los_Angeles` | Timezone the active-hours window is measured in (else UTC) |
| `PORT` | `8090` | Container HTTP port |

**→ [TUNING.md](TUNING.md) documents every knob**, what it does, how the knobs interact, and recipes
("it goes dry on weekends", "I'm seeing duplicate cards", "surface more of what I read", …). Full env
list in [.env.example](.env.example).

### Make it about *your* interests - categories

The categories (their labels, colors, and the **interest descriptions the scorer ranks against**) are
data, not code. Edit `categories.json` on the data volume - each entry is
`{ "key", "label", "color", "interest" }`, in priority order:

```json
[ { "key": "cooking", "label": "Cooking", "color": "#ff8800",
    "interest": "Cooking -- recipes, technique, equipment, restaurants" } ]
```

The `interest` line is the important part: it's woven into the LLM's scoring prompt, so write a real
sentence about what you actually want in that bucket - that's what makes relevance scoring good. Keys
must be lowercase alphanumeric. A fresh deploy seeds the file from
[categories.sample.json](categories.sample.json) (the maintainer's defaults); changes apply on the
next run. The pipeline, feed validation, and UI all read from this one place.

---

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | The dashboard (`digest.html`); redirects to `/login` when accounts are on and you're signed out |
| `GET` | `/auth/me` | Auth mode, the signed-in user (or `null`), and whether first-run setup is needed |
| `POST` | `/auth/login` · `/auth/logout` · `/auth/setup` | Sign in `{username, password}` · sign out · create the first admin (once) |
| `PUT` | `/auth/password` | Change your own password `{current, new}` |
| `GET` / `POST` / `PUT` / `DELETE` | `/users` (`?id=`) | Admin: list / create `{username, password, role, display_name}` / update `{role, disabled, password, display_name}` / delete users |
| `GET` | `/digest.json` | Current ranked digest |
| `GET` / `PUT` | `/state` | Read / save user state (read, later, history, sourceStats) |
| `GET` / `PUT` | `/settings` | Read schema + values / save runtime settings |
| `GET` / `POST` / `DELETE` | `/feeds` | List / add / remove feeds |
| `GET` | `/feeds/check?url=` | Validate a feed URL before adding |
| `GET` | `/models` | Ollama models available on the host (admin) |
| `GET` | `/status` | Live pipeline run status (for the "updating…" pill) |
| `GET` | `/runs` | Recent pipeline run log (powers Settings → Runs; admin) |
| `GET` | `/categories` | Configured categories (key / label / color / interest) |
| `GET` | `/search?q=&threshold=` | Semantic search - ranked digest matches by embedding similarity |
| `POST` | `/refresh` | Trigger a pipeline run now (202 started · 409 already running · 429 cooldown) |
| `GET` | `/articles?offset=&limit=&category=` | Ranked, diversified page of unread stories (powers *Discover more*) |
| `GET` / `POST` | `/feedback` | List votes (`?full=1` for details) / vote `{id, vote: 1 \| -1 \| 0 \| null}` by server-known id |
| `GET` | `/onboarding` | Wizard status, progress, feeds and a balanced candidate batch (`?n=&exclude=&force=1`) |
| `POST` | `/onboarding/sources` | Validate (with sample headlines) and add a feed, or a Google News search `{query, lang, country}` |
| `POST` | `/onboarding/status` · `/skip` · `/complete` · `/seeds` | Start/skip/reopen · skip a card · finish (needs 10 likes) · import liked URLs |
| `GET` | `/image?id=` | Resolved preview image for a known article (cached; falls back to a local illustration) |
| `GET` | `/static/fallback/<topic>.svg` | Local fallback illustrations |

---

## Data & persistence

Mutable data lives on the `cruxwire-data` Docker volume (`/data` in the container):
`state.json`, `feeds.json`, `digest.json`, `settings.json`, `runs.json`, `categories.json`,
`embeddings.json` (article vectors for semantic search, rewritten each run), `preferences.json`
(likes/dislikes + onboarding status), and three bounded caches - `embed_cache.json`,
`image_cache.json`, `onboarding_pool.json` - that are safe to delete. With accounts on there is also
`users.json`, `sessions.json` and a `users/<id>/` directory per additional user (see
[User accounts](#user-accounts)). The app code is baked into the image, so
rebuilds preserve your data; new settings keys fall back to their defaults automatically. Back up the
volume to preserve your feed list, read history, and learned preferences.

---

## Development

No dependencies - run the server directly against local files:

```bash
OLLAMA_HOST=http://localhost:11434 \
DIGEST_FILE=./digest.json STATE_FILE=./state.json FEEDS_FILE=./feeds.json \
SETTINGS_FILE=./settings.json \
STATIC_DIR=. SEED_FEEDS=./feeds.sample.json \
python server.py
```

Then open `http://localhost:8090/`. To work on the layout without a live Ollama/feed setup, point
`FEEDS_FILE` at a nonexistent path (the scheduled run no-ops) and drop a real `digest.json` in place.

Tests are stdlib `unittest` (no Ollama or network needed - a fake Ollama runs in-process):

```bash
python -m unittest discover -s tests
```

Benchmark scoring speed on your hardware before tuning *New articles scored / run*:

```bash
python pipeline.py --benchmark 5
```

---

## Project layout

| File | Role |
|------|------|
| [server.py](server.py) | HTTP server: UI, state/settings/feeds API, digest serving, state pruning |
| [pipeline.py](pipeline.py) | Ingestion pipeline + scheduler: fetch → carry-forward → score → cluster → retain |
| [settings.py](settings.py) | Runtime-settings schema (drives the Settings form), validation, persistence |
| [users.py](users.py) | Accounts, password hashing, sessions, per-user data paths (`AUTH_MODE`) |
| [preferences.py](preferences.py) | Likes/dislikes + onboarding state (`preferences.json`) |
| [onboarding.py](onboarding.py) | Wizard backend: feed inspection, candidate pool/rotation, seeds, completion |
| [images.py](images.py) | Preview-image resolution, URL normalisation, Google News decoding, fallbacks |
| [netsafe.py](netsafe.py) | SSRF-safe outbound HTTP used for every user/feed-supplied URL |
| [static/fallback/](static/fallback/) | Local topic illustrations (original artwork, MIT) |
| [tests/](tests/) | Unit, API and image/HTML contract tests |
| [digest.html](digest.html) | Single-file frontend: views, ranking display, affinity learning, layout |
| [login.html](login.html) | Sign-in / first-admin setup page (accounts mode) |
| [TUNING.md](TUNING.md) | Every adjustable knob, interactions, and recipes |

---

## Contributing & security

It's a personal project, but issues and pull requests are welcome - see
[CONTRIBUTING.md](CONTRIBUTING.md). To report a security issue, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
