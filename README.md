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

Alle Einstellungen werden über Umgebungsvariablen gesetzt. Beim ersten Deployment trägst du sie in `docker-compose.yml` im `environment`-Block ein:

| Variable | Pflicht | Beschreibung |
| --- | --- | --- |
| `PLEX_URL` | Ja | URL des Plex-Servers, z. B. `http://192.168.1.10:32400` |
| `PLEX_TOKEN` | Ja | X-Plex-Token des Server-Administrators |
| `PLEX_HOME_USER_PIN` | Nein | PIN für geschützte Plex-Home-Benutzer |
| `WEBHOOK_PORT` | Nein | Externer Port (Standard: `5000`) |

### Beispiel `docker-compose.yml`

```yaml
services:
  watchlist-cleanarr:
    image: ghcr.io/tannerap/watchlistcleanarr:latest
    container_name: watchlist-cleanarr
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      PLEX_URL: "http://192.168.1.10:32400"
      PLEX_TOKEN: "dein-plex-token"
      PLEX_HOME_USER_PIN: ""
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

> **Hinweis:** Wenn Plex auf dem Docker-Host läuft, verwende `http://host.docker.internal:32400` oder die LAN-IP des Hosts. Der Container muss den Plex-Server erreichen können.

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
curl http://localhost:5000/health
# {"status":"ok"}
```

## Radarr-Webhook einrichten

1. Radarr → **Einstellungen** → **Connect** → **+** → **Webhook**
2. **Name:** `WatchlistCleanarr`
3. **URL:** `http://<host>:5000/webhook/radarr`
4. **Trigger:** **On Delete** aktivieren
5. Speichern

**Docker-Netzwerk:** Laufen Radarr und WatchlistCleanarr im selben Compose-Netzwerk, kann die URL `http://watchlist-cleanarr:5000/webhook/radarr` lauten.

## Unterstützte Nutzertypen

| Nutzertyp | Erkennung | Watchlist-Zugriff |
| --- | --- | --- |
| Administrator | Plex-Account des Token-Inhabers | Admin-Token |
| Plex-Home (lokal/verwaltet) | `/api/home/users` + User-Switch | Token via Admin-Switch |
| Eigener Plex-Account mit Bibliotheksfreigabe | `/api/servers/{id}/shared_servers` | `accessToken` aus Freigabe |

## API-Endpunkte

| Methode | Pfad | Beschreibung |
| --- | --- | --- |
| `GET` | `/health` | Health-Check |
| `POST` | `/webhook/radarr` | Radarr-Webhook-Empfang |

Unterstützte Events: `MovieDelete` (Standard) und `MovieDeleted` (Alias).

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
