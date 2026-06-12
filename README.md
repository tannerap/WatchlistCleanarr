# WatchlistCleanarr

Automatically cleans Plex watchlists when Radarr or Sonarr removes movies or shows.

## The Problem

In many homelabs, the workflow looks like this:

1. Users add movies and shows to their **Plex watchlist**.
2. Services like Overseerr, Jellyseerr, or Watchlistarr read those watchlists and trigger downloads via **Radarr** or **Sonarr**.
3. After watching or when disk space is tight, media is removed in **Radarr/Sonarr** — the files disappear from the server.

**The problem:** The title stays on every affected user's Plex watchlist. The next sync can trigger another download even though the media is no longer on the server.

WatchlistCleanarr closes that gap: it receives Radarr/Sonarr deletion events and removes the title from the watchlists of **all users with access to the Plex server** — whether they are local Plex Home users or external Plex accounts with a library share.

## The Solution

```
Radarr/Sonarr (media removed)
        │
        ▼  Webhook: MovieDelete / SeriesDelete / …
WatchlistCleanarr
        │
        ├─► Plex server: discover all users with server access
        │     • Administrator
        │     • Plex Home users (local/managed)
        │     • External users with a library share (own Plex account)
        │
        ├─► Per user: load watchlist via python-plexapi
        │
        └─► Find and remove the title by TMDB/IMDb/TVDB ID
```

The service uses the **administrator token** (`PLEX_TOKEN`) to discover all server users and their access via the Plex.tv API. It then cleans each user's watchlist.

## Requirements

- Docker and Docker Compose
- Plex Media Server (configured as administrator)
- Radarr and/or Sonarr with webhook support
- `PLEX_TOKEN` must belong to the **server administrator**

## Quick Start with the Published Image

After the first release, the image is available on GitHub Container Registry:

```bash
docker pull ghcr.io/tannerap/watchlistcleanarr:latest
```

Or use `docker compose` (see below) — set environment variables on the first start only.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `PLEX_URL` | Yes | Plex URL, e.g. `http://plex:32400` (Docker DNS) |
| `PLEX_TOKEN` | Yes* | Administrator X-Plex-Token (*first start in compose only) |
| `PLEX_HOME_USER_PIN` | No | Optional fallback PIN for a **single** PIN-protected Plex Home profile (not one PIN per user) |
| `PLEX_USER_TOKENS` | No | Optional JSON map of Plex username → Plex.tv token for friends with library shares (see below) |
| `WEBHOOK_API_KEY` | Recommended | API key to protect webhook endpoints |
| `CONFIG_DIR` | No | Path for persistent config (default: `/data`) |
| `WEBHOOK_PORT` | No | Host port for Docker mapping (default: `8788`) |

### Remove tokens from compose after the first start

On the **first start** with `PLEX_TOKEN` and `WEBHOOK_API_KEY` in `docker-compose.yml`, the container writes the values to `/data/config.env` (volume). After that, you can remove both secrets from the compose file — restarts load them from the volume.

```bash
# Start once (token set in compose)
docker compose up -d

# Verify config was written
docker compose exec watchlist-cleanarr cat /data/config.env

# Remove PLEX_TOKEN from docker-compose.yml, restart
docker compose up -d
```

### Example `docker-compose.yml` (Plex on the Docker network)

```yaml
services:
  watchlist-cleanarr:
    image: ghcr.io/tannerap/watchlistcleanarr:latest
    volumes:
      - watchlist-cleanarr-data:/data
    environment:
      PLEX_URL: "http://plex:32400"      # Plex container hostname
      PLEX_TOKEN: "your-token"           # first start only
      CONFIG_DIR: "/data"
    networks:
      - media

volumes:
  watchlist-cleanarr-data:

networks:
  media:
    external: true   # same network as Plex/Radarr/Sonarr
```

> **Docker DNS:** `PLEX_URL` must be reachable from inside the container — typically `http://<plex-service-name>:32400` on the shared Docker network, not `localhost`.

For local development without Docker: create a `.env` file (see `.env.example`).

### Per-user Plex tokens (library-share friends)

Friends with their **own Plex account** and a library share cannot be cleaned with the admin `PLEX_TOKEN` alone. Each of them needs their own **Plex.tv X-Plex-Token** so WatchlistCleanarr can call `MyPlexAccount` on their behalf.

Configure tokens in either place:

1. **File (recommended):** `/data/user_tokens.env` on the config volume  
2. **Environment:** `PLEX_USER_TOKENS` as JSON on first start (persisted to the file automatically)

```bash
# /data/user_tokens.env
micha.65=their-plex-token-here
noemi.92=their-plex-token-here
```

Keys can be the Plex **username**, **display name**, or numeric **user ID** (case-insensitive). Each person finds their token the same way as the admin (Plex web app → View XML → copy `X-Plex-Token` from the URL).

```yaml
# docker-compose.yml (first start only)
environment:
  PLEX_USER_TOKENS: '{"micha.65":"token-a","noemi.92":"token-b"}'
```

After the first start you can edit `/data/user_tokens.env` directly and restart — no need to keep tokens in compose.

> This is a **Plex.tv token**, not the Radarr/Sonarr `WEBHOOK_API_KEY`. The webhook key only protects incoming delete events.

## Finding Your X-Plex-Token

1. Sign in to the Plex web app: [https://app.plex.tv](https://app.plex.tv)
2. Open any movie in your library
3. **⋯** → **View info** → **View XML**
4. Copy the `X-Plex-Token=...` parameter from the URL

Official guide: [Finding an authentication token / X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

## Getting Started

### With Docker Compose (recommended)

```bash
# 1. Set PLEX_URL and PLEX_TOKEN in docker-compose.yml
# 2. Start the service in the background
docker compose up -d

# Follow logs
docker compose logs -f watchlist-cleanarr
```

### Health Check

```bash
curl http://localhost:8788/ping
# {"status":"pong"}
```

The container self-checks via Docker healthcheck (`GET /ping` every 30 seconds). Check status:

```bash
docker compose ps
docker inspect watchlist-cleanarr --format '{{.State.Health.Status}}'
```

## Webhook Authentication

If `WEBHOOK_API_KEY` is set, all webhook requests must include the key:

| Method | Example |
| --- | --- |
| Query parameter (recommended for *arr) | `http://host:8788/webhook/radarr?apikey=YOUR_KEY` |
| Header | `X-API-Key: YOUR_KEY` |
| Bearer token | `Authorization: Bearer YOUR_KEY` |

## Radarr Webhook Setup

1. Radarr → **Settings** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:8788/webhook/radarr?apikey=YOUR_KEY`
4. Enable **triggers**:
   - **On Delete** — when the movie is removed from Radarr entirely
   - **On Movie File Delete** — when you use **Unmonitor and Delete Files** (movie stays in Radarr, file is deleted)
5. Save

> **Note:** `MovieFileDelete` events caused by upgrades (better quality) are intentionally ignored so the watchlist is not cleaned up.

## Sonarr Webhook Setup

1. Sonarr → **Settings** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:8788/webhook/sonarr?apikey=YOUR_KEY`
4. Enable **triggers**:
   - **On Series Delete** — when the series is removed from Sonarr entirely
   - **On Episode File Delete** — when you use **Unmonitor show + seasons, delete all episodes** (series stays in Sonarr, episodes are deleted)
5. Save

> **Note:** `EpisodeFileDelete` events caused by upgrades are intentionally ignored. Enable **On Episode File Delete**, not **On Episode File Delete For Upgrade**.

**Docker network:** If *arr and WatchlistCleanarr run in the same Compose network, the URL can be `http://watchlist-cleanarr:8788/webhook/radarr?apikey=...`.

### Delete vs. file-delete webhooks

| Action | Radarr event | Sonarr event | Required webhook trigger |
| --- | --- | --- | --- |
| Remove movie/show from *arr (with or without files) | `MovieDelete` | `SeriesDelete` | **On Delete** / **On Series Delete** |
| Unmonitor and delete files, keep entry in *arr | `MovieFileDelete` | `EpisodeFileDelete` | **On Movie File Delete** / **On Episode File Delete** |

Tools like **Maintainerr** use the second path for *unmonitor and delete* rules: they call the Radarr/Sonarr file-delete API, not the library-delete API. If only **On Delete** is enabled, WatchlistCleanarr will never see those actions.

Check WatchlistCleanarr logs after an action:

- `eventType=MovieDelete` / `SeriesDelete` → full library delete
- `eventType=MovieFileDelete` / `EpisodeFileDelete` → unmonitor + delete files
- `Ignoring unsupported ... eventType=...` → enable the matching file-delete trigger in Radarr/Sonarr

## Supported User Types

| User type | Discovery | Read + remove watchlist |
| --- | --- | --- |
| Administrator | Plex account of the token owner | `MyPlexAccount(token=admin)` |
| Plex Home (managed profile) | `/api/home/users` | `admin.switchHomeUser(profile)` via python-plexapi |
| Own Plex account with library share | `/api/servers/{id}/shared_servers` | GraphQL read-only, or read+write when their token is in `user_tokens.env` / `PLEX_USER_TOKENS` |

Watchlist removal uses the same **python-plexapi** flow as other community tools: `account.watchlist()` to load items and `account.removeFromWatchlist(item)` to remove them.

> **One `PLEX_TOKEN` is enough** for the admin account and **Plex Home profiles** (managed users). Friends with their own Plex account need an entry in `user_tokens.env` (or `PLEX_USER_TOKENS`) with **their** Plex.tv token. `PLEX_HOME_USER_PIN` is only needed when a Plex Home profile has its own PIN.
>
> **Important for shared users:** They must be friends with the admin, and watchlist visibility must be set to **Friends** or **Anyone** (Plex → Settings → Account → Privacy).

## API Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/ping` | Health check / ping (no API key) |
| `GET` | `/health` | Alias for `/ping` |
| `POST` | `/webhook/radarr` | Radarr webhook (movies) |
| `POST` | `/webhook/sonarr` | Sonarr webhook (shows) |

Supported events:

| Source | Events |
| --- | --- |
| Radarr | `MovieDelete`, `MovieDeleted`, `MovieFileDelete`, `MovieFileDeleted` |
| Sonarr | `SeriesDelete`, `SeriesDeleted`, `EpisodeFileDelete`, `EpisodeFileDeleted` |

## CI/CD: Build and Publish Docker Image

The workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) builds and publishes the image automatically:

| Trigger | Action |
| --- | --- |
| Push to `main` | Image → `ghcr.io/<owner>/watchlistcleanarr:latest` |
| Git tag `v*` | Additional semver tags (`v1.0.0`, `1.0`) |
| Pull request | Build test without push |

Can be triggered manually via **Actions → Docker Build and Publish → Run workflow**.

## Reliability

- Webhooks return **immediately** (`status: accepted`) and run Plex watchlist cleanup in the background so Radarr/Sonarr and tools like Maintainerr are not blocked by slow Plex API calls
- Background results are logged (`removedFromWatchlists=…` or error details)
- API and network errors are logged to the console
- The container does **not** exit on errors
- Missing user tokens or unreachable watchlists are skipped and logged

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # adjust values
python app.py
```
