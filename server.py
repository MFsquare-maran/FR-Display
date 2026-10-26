"""Lokaler Server: liefert die Anzeige aus, holt Live-Flugdaten und Flughafen-Geometrie (alles kostenlos, ohne API-Key).

Start:   python server.py        ->  http://localhost:8765
Umgebungsvariablen:  PORT (Standard 8765), START_ICAO (Flughafen beim Laden der Seite, Standard LSZH), CACHE_DIR

/api/aircraft?lat=..&lon=..&r=..   Live-Flugzeuge (adsb.fi, Reserve adsb.lol); r in nautischen Meilen
/api/airport?icao=EDDF             Flughafen per ICAO/IATA: Koordinaten (OurAirports) + Pisten/Rollwege/Terminals (OpenStreetMap)
                                   -> wird in ./cache gespeichert, beim zweiten Mal sofort da.
Später eigener Empfänger (RTL-SDR + readsb am RPi): URL in SOURCES als erste Quelle eintragen, dann unbegrenzt.
"""
import csv, io, json, math, os, queue, re, sys, time, threading, urllib.request, urllib.parse
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("PORT", "8765"))
START_ICAO = os.environ.get("START_ICAO", "LSZH").strip().upper() or "LSZH"
STATIC = {"/", "/index.html", "/lszh_data.js"}      # alles andere ausser /assets/ wird nicht ausgeliefert
CACHE_SEC = 2                    # so oft fragen wir die Flugdaten-Quelle höchstens
STALE_SEC = 30                   # bei Totalausfall alte Daten so lange weiterliefern
GEO_KM = 6.5                     # Umkreis der Flughafen-Geometrie (Anzeige zeigt 5 km Radius)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(HERE, "cache")
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
except OSError as e:
    print("Cache-Ordner nicht anlegbar:", e, flush=True)

SOURCES = [
    # {"name": "eigener Empfänger", "tpl": "http://localhost:8080/data/aircraft.json", "pause": 5},   # später
    {"name": "adsb.fi",  "tpl": "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{r}", "pause": 30},
    {"name": "adsb.lol", "tpl": "https://api.adsb.lol/v2/point/{lat}/{lon}/{r}",                 "pause": 600},
]
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_ac_cache = {}                   # (lat,lon,r) -> {"t","body","src","lock"}
_blocked = {}                    # Quelle -> pausiert bis
_lock = threading.Lock()


def http_get(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "fr-display/0.2", "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def write_atomic(path, text):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:               # Cache nicht beschreibbar (z. B. Rechte am gemounteten Ordner): weiter ohne Speichern
        print("WARNUNG: Cache nicht beschreibbar, Daten werden nicht gespeichert:", path, e, flush=True)


# ---------------------------------------------------------------- Flugzeuge
def fetch_aircraft(lat, lon, r):
    key = (round(lat, 3), round(lon, 3), r)
    with _lock:
        if len(_ac_cache) > 40:
            _ac_cache.clear()
        c = _ac_cache.setdefault(key, {"t": 0, "body": b'{"ac":[]}', "src": "", "lock": threading.Lock()})
    with c["lock"]:                       # pro Ort nur eine Anfrage gleichzeitig, andere warten auf deren Ergebnis
        now = time.time()
        if now - c["t"] < CACHE_SEC:
            return c["body"], c["src"]
        for s in SOURCES:
            if _blocked.get(s["name"], 0) > now:
                continue
            try:
                body = http_get(s["tpl"].format(lat=lat, lon=lon, r=r), timeout=6)
                json.loads(body)
                c.update(t=time.time(), body=body, src=s["name"])
                return body, s["name"]
            except Exception as e:
                _blocked[s["name"]] = now + s["pause"]
                print(f"{s['name']} fehlgeschlagen ({e}) -> pausiert {s['pause']} s", flush=True)
        if c["t"] and now - c["t"] < STALE_SEC:
            return c["body"], c["src"] + " (alt)"
        return None, ""


# ---------------------------------------------------------------- Strecke / Airline (adsbdb.com, kostenlos)
_routes = {}                     # Callsign -> (gültig bis, Ergebnis)


def fetch_route(cs):
    now = time.time()
    hit = _routes.get(cs)
    if hit and hit[0] > now:
        return hit[1]
    res, ttl = {}, 600           # unbekannte Flüge nach 10 min erneut versuchen
    try:
        j = json.loads(http_get("https://api.adsbdb.com/v0/callsign/" + urllib.parse.quote(cs), timeout=8))
        fr = j.get("response")
        if isinstance(fr, dict) and fr.get("flightroute"):
            fr = fr["flightroute"]
            ap = lambda x: {"iata": x.get("iata_code", ""), "icao": x.get("icao_code", ""),
                            "name": x.get("name", ""), "city": x.get("municipality", "")} if x else None
            al = fr.get("airline") or {}
            res = {"airline": {"name": al.get("name", ""), "iata": al.get("iata", ""), "icao": al.get("icao", "")},
                   "origin": ap(fr.get("origin")), "dest": ap(fr.get("destination"))}
            ttl = 6 * 3600
    except urllib.error.HTTPError as e:
        if e.code != 404:
            ttl = 60
    except Exception as e:
        print("Route fehlgeschlagen:", cs, e, flush=True)
        ttl = 60
    if len(_routes) > 2000:
        _routes.clear()
    _routes[cs] = (now + ttl, res)
    return res


# ---------------------------------------------------------------- Flughäfen
_index = None
_index_lock = threading.Lock()
IDX_FILE = os.path.join(CACHE_DIR, "airports_index.json")
CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def load_index():
    global _index
    with _index_lock:
        if _index is not None:
            return _index
        if os.path.exists(IDX_FILE):
            try:
                with open(IDX_FILE, encoding="utf-8") as f:
                    _index = json.load(f)
                return _index
            except Exception as e:
                print("Flughafenliste defekt, lade neu:", e, flush=True)
        print("Lade Flughafenliste (einmalig, ~12 MB) ...", flush=True)
        raw = http_get(CSV_URL, timeout=90).decode("utf-8")
        idx = {}
        for row in csv.DictReader(io.StringIO(raw)):
            if row["type"] not in ("large_airport", "medium_airport", "small_airport"):
                continue
            a = {"icao": row["ident"], "iata": row["iata_code"], "name": row["name"], "city": row["municipality"],
                 "lat": float(row["latitude_deg"]), "lon": float(row["longitude_deg"])}
            for k in {row["ident"], row["icao_code"], row["gps_code"], row["iata_code"]}:
                if k:
                    idx.setdefault(k.upper(), a)
        write_atomic(IDX_FILE, json.dumps(idx, separators=(",", ":")))
        _index = idx
        return idx


def overpass(q):
    """Fragt die Overpass-Server gestaffelt an (nach 4 s kommt der nächste dazu); der erste Treffer gewinnt."""
    res = queue.Queue()
    body = urllib.parse.urlencode({"data": q}).encode()

    def run(host):
        try:
            res.put(("ok", json.loads(http_get(host, data=body, timeout=45))))
        except Exception as e:
            res.put(("err", f"{host}: {e}"))

    started, failed, deadline = 0, 0, time.time() + 60
    while time.time() < deadline:
        if started < len(OVERPASS) and (started == 0 or failed >= started or time.time() >= nxt):
            threading.Thread(target=run, args=(OVERPASS[started],), daemon=True).start()
            started += 1
            nxt = time.time() + 4
        try:
            kind, val = res.get(timeout=0.25)
        except queue.Empty:
            continue
        if kind == "ok":
            return val
        failed += 1
        print("Overpass fehlgeschlagen:", val, flush=True)
        if failed >= len(OVERPASS):
            break
    raise RuntimeError("OpenStreetMap-Server nicht erreichbar")


_build_locks = {}


def build_airport(code):
    ap = load_index().get(code.upper())
    if not ap:
        return None
    path = os.path.join(CACHE_DIR, ap["icao"] + ".json")
    with _lock:
        bl = _build_locks.setdefault(ap["icao"], threading.Lock())
    with bl:                               # doppelte Anfragen für denselben Flughafen warten auf das erste Ergebnis
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print("Cache defekt, lade neu:", path, e, flush=True)
        lat0, lon0 = ap["lat"], ap["lon"]
        dlat = GEO_KM / 111.19
        dlon = dlat / max(0.05, math.cos(math.radians(lat0)))
        q = ('[out:json][timeout:40];way["aeroway"~"^(runway|taxiway|taxilane|apron|terminal)$"]'
             f'({lat0 - dlat:.5f},{lon0 - dlon:.5f},{lat0 + dlat:.5f},{lon0 + dlon:.5f});out geom qt;')
        data = overpass(q)
        k, R = math.pi / 180, 6371
        out = {"runway": [], "taxiway": [], "apron": [], "terminal": []}
        for w in data.get("elements", []):
            t = w.get("tags", {}).get("aeroway")
            t = "taxiway" if t == "taxilane" else t
            if t not in out or "geometry" not in w:
                continue
            pts = [[round((g["lon"] - lon0) * k * R * math.cos(lat0 * k), 3), round((g["lat"] - lat0) * k * R, 3)]
                   for g in w["geometry"]]
            s = [pts[0]]
            for p in pts[1:]:
                if math.hypot(p[0] - s[-1][0], p[1] - s[-1][1]) > 0.005:
                    s.append(p)
            if len(s) >= 2:
                out[t].append({"r": w["tags"].get("ref", ""), "p": s})
        res = {**ap, **out}
        write_atomic(path, json.dumps(res, separators=(",", ":")))
        return res


# ---------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    def send_json(self, code, obj=None, raw=None, src=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            if src:
                self.send_header("X-Source", src)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError):
            pass                              # Browser hat die Anfrage abgebrochen

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")      # Browser prüft index.html immer neu
        super().end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/aircraft":
                lat, lon = float(q.get("lat", 47.4647)), float(q.get("lon", 8.5492))
                r = int(min(250, max(1, float(q.get("r", 8)))))
                body, src = fetch_aircraft(lat, lon, r)
                if body is None:
                    return self.send_json(502, {"error": "keine Quelle erreichbar"})
                return self.send_json(200, raw=body, src=src)
            if u.path == "/api/config":
                return self.send_json(200, {"icao": START_ICAO})
            if u.path == "/api/route":
                cs = q.get("callsign", "").strip().upper()
                if not (2 <= len(cs) <= 8 and cs.isalnum()):
                    return self.send_json(400, {"error": "ungültiger Callsign"})
                return self.send_json(200, fetch_route(cs))
            if u.path == "/api/airport":
                code = q.get("icao", "").strip()
                if not (2 <= len(code) <= 4 and code.isalnum()):
                    return self.send_json(400, {"error": "Bitte ICAO- oder IATA-Code eingeben"})
                res = build_airport(code)
                if res is None:
                    return self.send_json(404, {"error": f"Flughafen '{code.upper()}' nicht gefunden"})
                return self.send_json(200, res)
            if re.fullmatch(r"/[A-Za-z0-9]{2,4}/?", u.path) and u.path not in STATIC:       # /lszh, /eddf, ... -> Anzeige mit diesem Flughafen
                self.path = "/index.html" + ("?" + u.query if u.query else "")
            elif u.path not in STATIC and not (u.path.startswith("/assets/") and ".." not in u.path):
                return self.send_error(404)
        except Exception as e:
            print("Fehler:", self.path, e, flush=True)
            return self.send_json(500, {"error": str(e)})
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionError):
            pass

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), partial(Handler, directory=HERE))
    except OSError as e:
        sys.exit(f"Port {PORT} ist belegt ({e}). Läuft schon ein server.py? Dann zuerst beenden.")
    srv.daemon_threads = True
    threading.Thread(target=lambda: load_index(), daemon=True).start()     # Flughafenliste vorladen
    print(f"Anzeige: http://localhost:{PORT}   (Simulation: http://localhost:{PORT}/?sim)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
