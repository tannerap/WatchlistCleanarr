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
        ├─► Per user: load watchlist (Discover API)
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
| `PLEX_HOME_USER_PIN` | No* | PIN for protected Plex Home users (*required to remove items from managed home users' watchlists) |
| `WEBHOOK_API_KEY` | Recommended | API key to protect webhook endpoints |
| `CONFIG_DIR` | No | Path for persistent config (default: `/data`) |
| `WEBHOOK_PORT` | No | External port (default: `5000`) |

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
curl http://localhost:5000/ping
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
| Query parameter (recommended for *arr) | `http://host:5000/webhook/radarr?apikey=YOUR_KEY` |
| Header | `X-API-Key: YOUR_KEY` |
| Bearer token | `Authorization: Bearer YOUR_KEY` |

## Radarr Webhook Setup

1. Radarr → **Settings** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:5000/webhook/radarr?apikey=YOUR_KEY`
4. Enable **triggers**:
   - **On Delete** — when the movie is removed from Radarr entirely
   - **On Movie File Delete** — when you use **Unmonitor and Delete Files** (movie stays in Radarr, file is deleted)
5. Save

> **Note:** `MovieFileDelete` events caused by upgrades (better quality) are intentionally ignored so the watchlist is not cleaned up.

## Sonarr Webhook Setup

1. Sonarr → **Settings** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:5000/webhook/sonarr?apikey=YOUR_KEY`
4. Enable **triggers**:
   - **On Series Delete** — when the series is removed from Sonarr entirely
   - **On Episode File Delete** — when you use **Unmonitor show + seasons, delete all episodes** (series stays in Sonarr, episodes are deleted)
5. Save

> **Note:** `EpisodeFileDelete` events caused by upgrades are intentionally ignored. Enable **On Episode File Delete**, not **On Episode File Delete For Upgrade**.

**Docker network:** If *arr and WatchlistCleanarr run in the same Compose network, the URL can be `http://watchlist-cleanarr:5000/webhook/radarr?apikey=...`.

## Supported User Types

| User type | Discovery | Watchlist access |
| --- | --- | --- |
| Administrator | Plex account of the token owner | GraphQL (UUID) + admin token |
| Plex Home (local/managed) | `/api/home/users` + user switch | GraphQL or REST with Home token |
| Own Plex account with library share | `/api/servers/{id}/shared_servers` | GraphQL with admin token + friend UUID |

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
