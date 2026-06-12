# WatchlistCleanarr

Webhook-Service (Flask), der Radarr-Lösch-Events empfängt und den betroffenen Film automatisch aus den Plex-Watchlists aller Plex-Home-Benutzer entfernt.

## Funktionsweise

1. Radarr sendet bei gelöschten Filmen ein `MovieDelete`-Webhook.
2. Der Service liest TMDB- und/oder IMDb-ID aus dem Payload.
3. Über den Administrator-Token werden der Admin-Account und alle Plex-Home-Benutzer angesprochen.
4. Auf jeder Watchlist wird der passende Film gesucht und entfernt.

## Voraussetzungen

- Docker und Docker Compose
- Plex Media Server mit Administrator-Zugang
- Radarr mit Webhook-Unterstützung

## X-Plex-Token finden

1. Melde dich in der Plex-Web-Oberfläche an: [https://app.plex.tv](https://app.plex.tv)
2. Öffne einen beliebigen Film oder eine Serie in deiner Bibliothek.
3. Klicke auf die drei Punkte (**…**) und wähle **Informationen anzeigen**.
4. Klicke unten links auf **XML anzeigen**.
5. In der geöffneten XML-Seite findest du in der URL den Parameter `X-Plex-Token=...` — das ist dein Token.

Offizielle Anleitung: [Finding an authentication token / X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

> Verwende den Token des **Plex-Home-Administrators**. Nur dieser kann die Watchlists aller Home-Benutzer verwalten.

## Konfiguration

Alle Einstellungen werden über Umgebungsvariablen gesetzt. Beim ersten Start trägst du sie in `docker-compose.yml` im `environment`-Block ein:

| Variable | Beschreibung |
| --- | --- |
| `PLEX_URL` | URL deines Plex-Servers, z. B. `http://192.168.1.10:32400` |
| `PLEX_TOKEN` | X-Plex-Token des Administrators |
| `PLEX_HOME_USER_PIN` | Optional: PIN für geschützte Plex-Home-Benutzer |
| `WEBHOOK_PORT` | Externer Port für den Webhook (Standard: `5000`) |

Für lokale Tests ohne Docker kannst du alternativ eine `.env`-Datei anlegen (siehe `.env.example`).

## Starten

```bash
# 1. Token und URL in docker-compose.yml eintragen
# 2. Service im Hintergrund starten
docker compose up -d

# Logs anzeigen
docker compose logs -f
```

Health-Check: `GET http://localhost:5000/health`

## Radarr-Webhook einrichten

1. Radarr öffnen → **Einstellungen** → **Connect** → **+** → **Webhook**
2. Name: z. B. `WatchlistCleanarr`
3. URL: `http://<server-ip>:5000/webhook/radarr`
4. Unter **Trigger** aktivieren: **On Delete**
5. Speichern

> Wenn Radarr in Docker läuft, muss der Webhook die erreichbare Adresse des Hosts oder des Docker-Netzwerks verwenden (z. B. `http://watchlist-cleanarr:5000/webhook/radarr` im selben Compose-Netzwerk).

## API-Endpunkte

| Methode | Pfad | Beschreibung |
| --- | --- | --- |
| `GET` | `/health` | Health-Check |
| `POST` | `/webhook/radarr` | Radarr-Webhook-Empfang |

## Hinweise

- Unterstützte Events: `MovieDelete` (und `MovieDeleted` als Alias)
- Freunde mit geteiltem Server-Zugang (ohne Plex Home) werden nicht automatisch erfasst — dafür wären eigene Tokens nötig
- API-Fehler werden geloggt; der Container bleibt dabei aktiv
