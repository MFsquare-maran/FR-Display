# FR-Display – Flughafen-Radar

Minimalistisches Schwarzweiss-Display: zeigt Pisten, Rollwege, Vorfelder und Terminals eines Flughafens (5 km Radius, bei Bedarf 6 km) und darauf die Flugzeuge live. Kostenlose Datenquellen, kein API-Key, nur Python-Standardbibliothek.

## Starten mit Docker / Portainer

```bash
docker compose up -d --build
```

Dann `http://<host>:8765` öffnen (nur HTTP).

**Flughafen per URL wählen:** `http://<host>:8765/lszh`, `/eddf`, `/kjfk`, `/vie` … (ICAO oder IATA, 2–4 Zeichen). Die Hauptseite `/` startet mit `START_ICAO`.

Einstellungen in der `.env` (oder als Umgebungsvariablen in Portainer):

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8765` | Port, auf dem die Anzeige per HTTP erreichbar ist |
| `CACHE_PATH` | `/home/maran/serverdata/server/fr-display/cache` | Ordner auf dem Host für den Flughafen-Cache |
| `START_ICAO` | `LSZH` | Flughafen, der beim Laden der Hauptseite `/` startet (ICAO oder IATA) |

**Portainer:** Stacks → Add stack → *Repository* → `https://github.com/MFsquare-maran/FR-Display`, Compose-Pfad `docker-compose.yml`. Variablen (`PORT`, `START_ICAO`) unter *Environment variables* setzen oder die `.env` laden. Der Flughafen-Cache liegt auf dem Host in `CACHE_PATH` (Standard `/home/maran/serverdata/server/fr-display/cache`) und bleibt bei Updates und beim Löschen des Stacks erhalten.

## Ohne Docker

```bash
python server.py        # -> http://localhost:8765
```

`PORT` und `START_ICAO` lassen sich auch hier als Umgebungsvariablen setzen. `http://localhost:8765/eddf` (oder `?icao=EDDF`) überschreibt den Start-Flughafen, `?sim` startet die Simulation, `#kiosk` blendet den Mauszeiger aus (Raspberry Pi / Chromium `--kiosk`).

## Aufbau

| Datei | Zweck |
|---|---|
| `index.html` | Gesamte Anzeige (HTML, CSS, JS, Canvas), keine Abhängigkeiten |
| `server.py` | Liefert die Seite aus; `/api/aircraft`, `/api/airport`, `/api/route`, `/api/config` |
| `lszh_data.js` | Eingebettete Geometrie von Zürich (läuft auch offline) |
| `assets/` | Logos der Flugzeughersteller (Airbus, Boeing, Bombardier) |
| `Dockerfile`, `docker-compose.yml`, `.env` | Container-Betrieb |

### Datenquellen

- **Flugzeuge:** adsb.fi (Hauptquelle), adsb.lol (Reserve, 500 Anfragen/Tag). Bodenfahrzeuge (Kategorie C, Typ `GND`/`EMER`) werden herausgefiltert.
- **Strecke und Airline:** adsbdb.com (über `/api/route`, gecacht). Fehlt bei Privat-/Militärflügen und unbekannten Callsigns.
- **Airline-Logos:** pics.avs.io (der Browser braucht Internet). Hersteller-Logos liegen lokal in `assets/`.
- **Flughäfen:** Koordinaten aus OurAirports, Geometrie per Overpass/OSM. Die erste Abfrage eines neuen Flughafens dauert ca. 30–45 s (abhängig von Overpass), danach kommt sie aus dem Cache.

### Anzeige

- Radius 5 km um den Flughafen; ragt die Geometrie in den unteren Beschriftungsstreifen, werden 6 km genommen.
- Flugzeuge sind im ganzen Bildschirm sichtbar; die Anzeige läuft ca. 5,5 s hinter der Echtzeit und interpoliert zwischen den Meldungen (keine Sprünge). Der Schweif blendet über 45 s aus.
- Das ausgewählte Flugzeug wechselt alle 7 s mit Überblenden; unten im äusseren Ring stehen Airline-Logo, Callsign, Strecke, Typ und Hersteller-Logo.

## Hinweise für Änderungen

- Nur Python-Standardbibliothek, kein Build-Schritt im Frontend.
- Nach Änderungen an `index.html` im Browser hart neu laden; bei leerer Seite zuerst die Konsole prüfen.
- Nur ein Server pro Port; unter Windows können sonst alte Instanzen antworten.
