# WatchlistCleanarr

Automatische Bereinigung von Plex-Watchlists, wenn Radarr einen Film löscht.

## Das Problem

In vielen Homelabs läuft folgender Ablauf:

1. Nutzer setzen Filme auf ihre **Plex-Watchlist** (Merkenliste).
2. Dienste wie Overseerr, Jellyseerr oder Watchlistarr laden diese Watchlist und triggern einen Download über **Radarr**.
3. Nach dem Schauen oder bei Speicherengpässen wird der Film in **Radarr gelöscht** — die Datei verschwindet vom Server.

**Das Problem:** Der Film bleibt auf der Plex-Watchlist aller betroffenen Nutzer stehen. Beim nächsten Sync kann erneut ein Download ausgelöst werden, obwohl der Film gar nicht mehr vorhanden ist.

WatchlistCleanarr schließt diese Lücke: Es empfängt Radarr-Lösch-Events und entfernt den Film aus den Watchlists **aller Nutzer, die Zugriff auf den betreffenden Plex-Server haben** — egal ob lokale Plex-Home-Benutzer oder externe Plex-Accounts mit Bibliotheksfreigabe.

## Die Lösung

```
Radarr (Film gelöscht)
        │
        ▼  Webhook: MovieDelete
WatchlistCleanarr
        │
        ├─► Plex-Server: alle Nutzer mit Serverzugriff ermitteln
        │     • Administrator
        │     • Plex-Home-Benutzer (lokal/verwaltet)
        │     • Externe Nutzer mit Bibliotheksfreigabe (eigener Plex-Account)
        │
        ├─► Pro Nutzer: Watchlist laden (Discover API)
        │
        └─► Film per TMDB-/IMDb-ID finden und entfernen
```

Der Service nutzt den **Administrator-Token** (`PLEX_TOKEN`), um über die Plex.tv-API alle Server-Nutzer und deren Zugriffstokens zu ermitteln. Anschließend wird für jeden Nutzer dessen Watchlist bereinigt.

## Voraussetzungen

- Docker und Docker Compose
- Plex Media Server (als Administrator konfiguriert)
- Radarr mit Webhook-Unterstützung
- Der `PLEX_TOKEN` muss vom **Server-Administrator** stammen

## Schnellstart mit veröffentlichtem Image

Nach dem ersten Release steht das Image auf GitHub Container Registry bereit:

```bash
docker pull ghcr.io/tannerap/watchlistcleanarr:latest
```

Alternativ mit `docker compose` (siehe unten) — beim ersten Start nur die Umgebungsvariablen setzen.

## Konfiguration

| Variable | Pflicht | Beschreibung |
| --- | --- | --- |
| `PLEX_URL` | Ja | Plex-URL, z. B. `http://plex:32400` (Docker-DNS) |
| `PLEX_TOKEN` | Ja* | X-Plex-Token des Administrators (*nur beim ersten Start in compose) |
| `PLEX_HOME_USER_PIN` | Nein | PIN für geschützte Plex-Home-Benutzer |
| `WEBHOOK_API_KEY` | Empfohlen | API-Key zum Schutz der Webhook-Endpunkte |
| `CONFIG_DIR` | Nein | Pfad für persistente Config (Standard: `/data`) |
| `WEBHOOK_PORT` | Nein | Externer Port (Standard: `5000`) |

### Token nach dem ersten Start aus compose entfernen

Beim **ersten Start** mit `PLEX_TOKEN` und `WEBHOOK_API_KEY` in `docker-compose.yml` schreibt der Container die Werte nach `/data/config.env` (Volume). Danach kannst du beide Secrets aus der compose-Datei entfernen — bei Neustarts werden sie aus dem Volume geladen.

```bash
# Einmalig starten (Token in compose gesetzt)
docker compose up -d

# Prüfen, ob config geschrieben wurde
docker compose exec watchlist-cleanarr cat /data/config.env

# PLEX_TOKEN aus docker-compose.yml entfernen, neu starten
docker compose up -d
```

### Beispiel `docker-compose.yml` (Plex im Docker-Netzwerk)

```yaml
services:
  watchlist-cleanarr:
    image: ghcr.io/tannerap/watchlistcleanarr:latest
    volumes:
      - watchlist-cleanarr-data:/data
    environment:
      PLEX_URL: "http://plex:32400"      # Hostname des Plex-Containers
      PLEX_TOKEN: "dein-token"           # nur beim ersten Start
      CONFIG_DIR: "/data"
    networks:
      - media

volumes:
  watchlist-cleanarr-data:

networks:
  media:
    external: true   # gleiches Netzwerk wie Plex/Radarr
```

> **Docker-DNS:** `PLEX_URL` muss vom Container aus erreichbar sein — typisch `http://<plex-service-name>:32400` im gemeinsamen Docker-Netzwerk, nicht `localhost`.

Für lokale Entwicklung ohne Docker: `.env`-Datei anlegen (siehe `.env.example`).

## X-Plex-Token finden

1. In der Plex-Web-Oberfläche anmelden: [https://app.plex.tv](https://app.plex.tv)
2. Einen beliebigen Film in der Bibliothek öffnen
3. **⋯** → **Informationen anzeigen** → **XML anzeigen**
4. In der URL den Parameter `X-Plex-Token=...` kopieren

Offizielle Anleitung: [Finding an authentication token / X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

## Starten

### Mit Docker Compose (empfohlen)

```bash
# 1. PLEX_URL und PLEX_TOKEN in docker-compose.yml eintragen
# 2. Service im Hintergrund starten
docker compose up -d

# Logs verfolgen
docker compose logs -f watchlist-cleanarr
```

### Health-Check

```bash
curl http://localhost:5000/ping
# {"status":"pong"}
```

Der Container prüft sich selbst per Docker-Healthcheck (`GET /ping` alle 30 Sekunden). Status anzeigen:

```bash
docker compose ps
docker inspect watchlist-cleanarr --format '{{.State.Health.Status}}'
```

## Webhook-Authentifizierung

Wenn `WEBHOOK_API_KEY` gesetzt ist, müssen alle Webhook-Requests den Key mitsenden:

| Methode | Beispiel |
| --- | --- |
| Query-Parameter (empfohlen für *arr) | `http://host:5000/webhook/radarr?apikey=DEIN_KEY` |
| Header | `X-API-Key: DEIN_KEY` |
| Bearer-Token | `Authorization: Bearer DEIN_KEY` |

## Radarr-Webhook einrichten

1. Radarr → **Einstellungen** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:5000/webhook/radarr?apikey=DEIN_KEY`
4. **Trigger** aktivieren:
   - **On Delete** — wenn der Film komplett aus Radarr entfernt wird
   - **On Movie File Delete** — wenn du **Unmonitor and Delete Files** nutzt (Film bleibt in Radarr, Datei wird gelöscht)
5. Speichern

> **Hinweis:** Bei Upgrades (bessere Qualität) wird `MovieFileDelete` absichtlich ignoriert, damit die Watchlist nicht bereinigt wird.

## Sonarr-Webhook einrichten

1. Sonarr → **Einstellungen** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:5000/webhook/sonarr?apikey=DEIN_KEY`
4. **Trigger:** **On Series Delete** aktivieren
5. Speichern

**Docker-Netzwerk:** Laufen *arr und WatchlistCleanarr im selben Compose-Netzwerk, kann die URL `http://watchlist-cleanarr:5000/webhook/radarr?apikey=...` lauten.

## Unterstützte Nutzertypen

| Nutzertyp | Erkennung | Watchlist-Zugriff |
| --- | --- | --- |
| Administrator | Plex-Account des Token-Inhabers | GraphQL (UUID) + Admin-Token |
| Plex-Home (lokal/verwaltet) | `/api/home/users` + User-Switch | GraphQL oder REST mit Home-Token |
| Eigener Plex-Account mit Bibliotheksfreigabe | `/api/servers/{id}/shared_servers` | GraphQL mit Admin-Token + Freund-UUID |

> **Wichtig für geteilte Nutzer:** Sie müssen mit dem Admin befreundet sein, und die Watchlist-Sichtbarkeit muss auf **Friends** oder **Anyone** stehen (Plex → Einstellungen → Konto → Datenschutz).

## API-Endpunkte

| Methode | Pfad | Beschreibung |
| --- | --- | --- |
| `GET` | `/ping` | Health-Check / Ping (ohne API-Key) |
| `GET` | `/health` | Alias für `/ping` |
| `POST` | `/webhook/radarr` | Radarr-Webhook (Filme) |
| `POST` | `/webhook/sonarr` | Sonarr-Webhook (Serien) |

Unterstützte Events:

| Quelle | Events |
| --- | --- |
| Radarr | `MovieDelete`, `MovieDeleted`, `MovieFileDelete`, `MovieFileDeleted` |
| Sonarr | `SeriesDelete`, `SeriesDeleted` |

## CI/CD: Docker-Image bauen und veröffentlichen

Der Workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) baut und veröffentlicht das Image automatisch:

| Trigger | Aktion |
| --- | --- |
| Push auf `main` | Image → `ghcr.io/<owner>/watchlistcleanarr:latest` |
| Git-Tag `v*` | Zusätzlich semver-Tags (`v1.0.0`, `1.0`) |
| Pull Request | Build-Test ohne Push |

Manuell auslösbar über **Actions → Docker Build and Publish → Run workflow**.

## Robustheit

- API- und Netzwerkfehler werden in die Konsole geloggt
- Der Container beendet sich bei Fehlern **nicht**
- Fehlende Nutzer-Tokens oder nicht erreichbare Watchlists werden übersprungen und geloggt

## Lokale Entwicklung

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Werte anpassen
python app.py
```
