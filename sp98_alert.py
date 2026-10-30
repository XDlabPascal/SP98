#!/usr/bin/env python3
"""Alerte push (ntfy) quand le SP98 est disponible ou change de prix
dans les stations TotalEnergies proches d'une adresse.

Sources (gratuites, sans clé) :
  - Prix des carburants en France, flux instantané v2 (data.economie.gouv.fr)
  - API Adresse (api-adresse.data.gouv.fr) pour géocoder l'adresse
  - OpenStreetMap / Overpass pour savoir quelles stations sont des TotalEnergies
    (la marque n'existe pas dans le flux officiel)

Dépendances : aucune (bibliothèque standard Python 3.9+).

Variables d'environnement :
  ADDRESS       adresse de référence (obligatoire)          ex. "rue Molière, 72190 Coulaines"
  NTFY_TOPIC    nom du "topic" ntfy (obligatoire sauf --dry-run/--list)
  RADIUS_KM     rayon de recherche en km            (défaut 15)
  STATION_IDS   ids de stations à surveiller, séparés par des virgules.
                Si renseigné, la détection automatique TotalEnergies est ignorée.
  BRAND_REGEX   marque recherchée dans OSM          (défaut "total")
  PRICE_DELTA   variation de prix minimale en €/L   (défaut 0.001)
  ALERT_EMAIL   adresse mail qui reçoit aussi chaque alerte (relais par ntfy, optionnel)
  KEEP_MOTORWAY mettre 1 pour inclure aussi les stations d'autoroute (exclues par défaut)
  NTFY_SERVER   serveur ntfy                        (défaut https://ntfy.sh)
  NTFY_TOKEN    jeton d'accès ntfy (optionnel, topics protégés)
  STATE_FILE    fichier d'état                      (défaut state.json à côté du script)

Options : --list (affiche les stations du rayon), --dry-run (n'envoie rien,
n'écrit rien), --test-notify (envoie une notification de test).
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "sp98-alert/1.0 (+usage personnel)"
DATASET = "prix-des-carburants-en-france-flux-instantane-v2"
API_FLUX = f"https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/{DATASET}/records"
API_ADRESSE = "https://api-adresse.data.gouv.fr/search/"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
TOTAL_CACHE_DAYS = 7
MATCH_MAX_M = 100  # rattachement OSM -> flux par proximité si pas de ref:FR:prix-carburants


# --------------------------------------------------------------------------- HTTP
def http(url, data=None, headers=None, timeout=30, retries=3):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if 400 <= e.code < 500 and e.code != 429:  # erreur définitive : inutile de réessayer
                raise RuntimeError(f"HTTP {e.code} sur {url.split('?')[0]} : {body}")
            last = f"HTTP {e.code} {body}"
            time.sleep(2 * (i + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Échec requête {url.split('?')[0]} : {last}")


# --------------------------------------------------------------------------- géo
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode(address):
    url = API_ADRESSE + "?" + urllib.parse.urlencode({"q": address, "limit": 1})
    feats = http(url).get("features") or []
    if not feats:
        raise RuntimeError(f"Adresse introuvable : {address!r}")
    lon, lat = feats[0]["geometry"]["coordinates"]
    return lat, lon, feats[0]["properties"].get("label", address)


def coords(rec):
    g = rec.get("geom")
    if isinstance(g, dict) and g.get("lat") is not None:
        return float(g["lat"]), float(g["lon"])
    lat, lon = rec.get("latitude"), rec.get("longitude")
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    if abs(lat) > 90 or abs(lon) > 180:  # ancien format en 1e-5 degré
        lat, lon = lat / 1e5, lon / 1e5
    return lat, lon


# --------------------------------------------------------------------------- flux carburants
def fetch_stations(lat, lon, radius_km):
    out, offset, limit = [], 0, 100
    where = f"within_distance(geom, geom'POINT({lon} {lat})', {radius_km}km)"
    while True:
        q = urllib.parse.urlencode({"where": where, "limit": limit, "offset": offset})
        d = http(f"{API_FLUX}?{q}")
        res = d.get("results", [])
        out.extend(res)
        offset += limit
        if not res or offset >= d.get("total_count", 0) or offset >= 9900:
            break
    return out


def names(v):
    """Normalise une liste de carburants (liste ou chaîne) en ensemble de noms MAJUSCULES."""
    if v is None:
        return set()
    if isinstance(v, str):
        v = re.split(r"[;,|]", v)
    return {str(x).strip().upper() for x in v if str(x).strip()}


MOTORWAY_RX = re.compile(r"^\s*A\s?\d{1,3}\b|\bAUTOROUTE\b|\bAIRE\s+(DE|DU|DES|D')\b|\bAIRE\s+SARTHE\b", re.I)


def is_motorway(rec):
    """Station d'autoroute : champ officiel 'pop' = 'A', sinon adresse du type 'A11 - AIRE ...'."""
    if str(rec.get("pop", "")).strip().upper() == "A":
        return True
    return bool(MOTORWAY_RX.search(str(rec.get("adresse", ""))))


def sp98_info(rec):
    """Retourne {'available': bool, 'price': float|None, 'maj': str|None, 'status': str}."""
    price = rec.get("sp98_prix", rec.get("prix_sp98"))
    price = float(price) if price not in (None, "") else None
    maj = rec.get("sp98_maj", rec.get("prix_sp98_maj"))
    dispo = names(rec.get("carburants_disponibles"))
    rupt = (
        names(rec.get("carburants_indisponibles"))
        | names(rec.get("carburants_rupture_temporaire"))
        | names(rec.get("carburants_rupture_definitive"))
    )
    if "SP98" in rupt:
        status = "rupture"
    elif price is None:
        status = "non proposé"
    elif dispo and "SP98" not in dispo:
        status = "rupture"
    else:
        status = "disponible"
    return {"available": status == "disponible", "price": price, "maj": maj, "status": status}


# --------------------------------------------------------------------------- détection TotalEnergies
def total_ids_from_osm(lat, lon, radius_km, flux_by_id, brand_regex):
    rx = re.compile(brand_regex, re.I)
    around = f"(around:{int((radius_km + 1) * 1000)},{lat},{lon})"
    b = brand_regex.replace("\\", "\\\\").replace('"', "")
    # filtre côté serveur (requête légère) : seules les stations dont marque/opérateur/nom correspondent
    q = (
        "[out:json][timeout:25];("
        + "".join(f'nwr["amenity"="fuel"]["{k}"~"{b}",i]{around};' for k in ("brand", "operator", "name"))
        + ");out center tags;"
    )
    body = urllib.parse.urlencode({"data": q}).encode()
    data, err = None, None
    for _ in range(2):  # deux tours sur l'ensemble des serveurs
        for ep in OVERPASS:
            try:
                data = http(ep, data=body, timeout=40, retries=1)
                break
            except RuntimeError as e:
                err = e
        if data is not None:
            break
        time.sleep(5)
    if data is None:
        raise RuntimeError(f"Overpass indisponible : {err}")

    positions = {i: coords(r) for i, r in flux_by_id.items() if coords(r)}
    ids = {}
    for el in data.get("elements", []):
        t = el.get("tags", {})
        if not any(rx.search(t.get(k, "")) for k in ("brand", "operator", "name")):
            continue
        ref = t.get("ref:FR:prix-carburants")
        if ref and ref in flux_by_id:
            ids[ref] = "osm-ref"
            continue
        c = (el.get("lat"), el.get("lon")) if "lat" in el else (
            el.get("center", {}).get("lat"), el.get("center", {}).get("lon"))
        if None in c:
            continue
        best = min(
            ((haversine_km(c[0], c[1], *p) * 1000, i) for i, p in positions.items()),
            default=None,
        )
        if best and best[0] <= MATCH_MAX_M:
            ids.setdefault(best[1], "osm-proche")
    return sorted(ids)


def resolve_total_ids(state, lat, lon, radius_km, flux_by_id, brand_regex):
    """`state` est le dictionnaire du cache des stations (fichier total_cache.json, séparé de state.json)."""
    manual = [x.strip() for x in os.environ.get("STATION_IDS", "").split(",") if x.strip()]
    if manual:
        return manual
    cache = state.get("total", {})
    fresh = False
    if cache.get("ts"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cache["ts"])).days
        fresh = age < TOTAL_CACHE_DAYS and cache.get("key") == f"{lat:.4f},{lon:.4f},{radius_km},{brand_regex}"
    if fresh:
        return cache["ids"]
    try:
        ids = total_ids_from_osm(lat, lon, radius_km, flux_by_id, brand_regex)
    except RuntimeError as e:
        if cache.get("ids"):
            print(f"[warn] {e} — utilisation de la liste en cache", file=sys.stderr)
            return cache["ids"]
        raise
    state["total"] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key": f"{lat:.4f},{lon:.4f},{radius_km},{brand_regex}",
        "ids": ids,
    }
    return ids


# --------------------------------------------------------------------------- logique d'alerte
def fmt_price(p):
    return "—" if p is None else f"{p:.3f} €/L".replace(".", ",")


def compute_events(prev, current, delta):
    """prev/current : {id: {available, price, ...}}. Retourne [(id, kind, prev_info, info)]."""
    events = []
    for sid, cur in current.items():
        p = prev.get(sid)
        if p is None:
            if cur["available"]:
                events.append((sid, "dispo", None, cur))
            continue
        if cur["available"] and not p["available"]:
            events.append((sid, "dispo", p, cur))
        elif not cur["available"] and p["available"]:
            events.append((sid, "rupture", p, cur))
        elif cur["available"] and p["available"]:
            if cur["price"] is not None and p["price"] is not None and abs(cur["price"] - p["price"]) >= delta - 1e-9:
                events.append((sid, "prix", p, cur))
    return events


def maps_url(lat, lon):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def build_notification(kind, rec, prev, cur, dist_km):
    place = f"{rec.get('adresse', '?')}, {rec.get('ville', '')}".strip(", ")
    base = f"{place}\nà {dist_km:.1f} km".replace(".", ",")
    if kind == "dispo":
        return ("SP98 disponible – TotalEnergies", f"{base}\nSP98 : {fmt_price(cur['price'])}", 4, ["fuelpump", "white_check_mark"])
    if kind == "rupture":
        return ("SP98 en rupture – TotalEnergies", base, 2, ["fuelpump", "x"])
    arrow = "baisse" if cur["price"] < prev["price"] else "hausse"
    return (
        f"SP98 en {arrow} – TotalEnergies",
        f"{base}\nSP98 : {fmt_price(prev['price'])} → {fmt_price(cur['price'])}",
        3 if arrow == "baisse" else 2,
        ["fuelpump", "chart_with_downwards_trend" if arrow == "baisse" else "chart_with_upwards_trend"],
    )


def notify(title, message, priority=3, tags=None, click=None):
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if "://" in topic:  # URL collée par erreur : on garde le nom du topic
        topic = topic.rstrip("/").split("/")[-1]
    if not topic:
        raise RuntimeError("NTFY_TOPIC manquant")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        bad = [f"position {i + 1} : U+{ord(c):04X}" for i, c in enumerate(topic) if not re.match(r"[A-Za-z0-9_-]", c)]
        raise RuntimeError(
            f"NTFY_TOPIC invalide (longueur {len(topic)}, 64 max ; lettres sans accent, chiffres, '-' et '_' uniquement). "
            f"Caractères refusés : {'; '.join(bad[:5]) or 'aucun (trop long ?)'}"
        )
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]

    email = (os.environ.get("ALERT_EMAIL") or "").strip()
    if email:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise RuntimeError("ALERT_EMAIL invalide (format attendu : nom@domaine.fr)")
        try:  # ntfy relaie aussi la notification par mail (quota ntfy.sh : ~16 mails/jour)
            http(server, data=json.dumps({**payload, "email": email}).encode("utf-8"), headers=headers)
            return
        except RuntimeError as e:
            print(f"[warn] envoi avec mail refusé ({e}) — nouvel essai sans mail", file=sys.stderr)
    http(server, data=json.dumps(payload).encode("utf-8"), headers=headers)


# --------------------------------------------------------------------------- état
def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="liste les stations du rayon et leur statut SP98")
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien et n'écrit pas l'état")
    ap.add_argument("--test-notify", action="store_true", help="envoie une notification de test")
    args = ap.parse_args()

    if args.test_notify:
        notify("Test alerte SP98", "Si tu lis ceci, les notifications fonctionnent.", 3, ["fuelpump", "tada"])
        print("Notification de test envoyée.")
        return 0

    address = os.environ.get("ADDRESS", "").strip()
    if not address:
        print("ADDRESS manquant", file=sys.stderr)
        return 2
    radius = float(os.environ.get("RADIUS_KM") or 15)
    delta = float(os.environ.get("PRICE_DELTA") or 0.001)
    brand = os.environ.get("BRAND_REGEX") or "total"
    state_path = os.environ.get("STATE_FILE") or str(Path(__file__).with_name("state.json"))

    lat, lon, label = geocode(address)
    stations = fetch_stations(lat, lon, radius)
    flux = {str(r["id"]): r for r in stations if r.get("id") is not None}
    print(f"Adresse : {label} ({lat:.5f}, {lon:.5f}) — {len(flux)} stations dans {radius:g} km")

    state = load_state(state_path)
    cache_path = os.environ.get("TOTAL_CACHE_FILE") or str(Path(state_path).with_name("total_cache.json"))
    cache = load_state(cache_path)
    if "total" not in cache and "total" in state:  # ancien emplacement du cache
        cache["total"] = state["total"]
    total_before = json.dumps(cache.get("total"), sort_keys=True)
    total_ids = resolve_total_ids(cache, lat, lon, radius, flux, brand)
    if not args.dry_run and json.dumps(cache.get("total"), sort_keys=True) != total_before:
        save_state(cache_path, cache)
    state.pop("total", None)
    total_ids = [i for i in total_ids if i in flux]
    keep_motorway = os.environ.get("KEEP_MOTORWAY", "").strip().lower() in ("1", "true", "oui", "yes")
    if not keep_motorway:
        total_ids = [i for i in total_ids if not is_motorway(flux[i])]

    def dist(rec):
        c = coords(rec)
        return haversine_km(lat, lon, *c) if c else float("nan")

    if args.list:
        for sid, rec in sorted(flux.items(), key=lambda kv: dist(kv[1])):
            info = sp98_info(rec)
            mark = "TOTAL" if sid in total_ids else ("AUTO " if is_motorway(rec) else "     ")
            print(f"{mark} {sid:>9} {dist(rec):5.1f} km  {rec.get('adresse','?')[:38]:38} {rec.get('ville','')[:18]:18} "
                  f"SP98 {fmt_price(info['price']):>11}  {info['status']}")
        return 0

    if not total_ids:
        print("Aucune station TotalEnergies identifiée dans le rayon (voir README : STATION_IDS).", file=sys.stderr)
        return 1

    current = {i: sp98_info(flux[i]) for i in total_ids}
    prev = state.get("stations", {})
    first_run = not prev

    if first_run:
        lines = []
        for i in sorted(total_ids, key=lambda i: dist(flux[i])):
            r, c = flux[i], current[i]
            lines.append(f"• {r.get('adresse','?')}, {r.get('ville','')} ({dist(r):.1f} km) : {fmt_price(c['price'])} – {c['status']}")
        title = f"Surveillance SP98 active ({len(total_ids)} stations)"
        msg = "\n".join(lines)
        print(title + "\n" + msg)
        if not args.dry_run:
            notify(title, msg, 3, ["fuelpump", "eyes"])
    else:
        events = compute_events(prev, current, delta)
        for sid, kind, p, c in events:
            rec = flux[sid]
            title, msg, prio, tags = build_notification(kind, rec, p, c, dist(rec))
            print(f"[{kind}] {sid} {msg.replace(chr(10), ' | ')}")
            if not args.dry_run:
                cc = coords(rec)
                notify(title, msg, prio, tags, maps_url(*cc) if cc else None)
        if not events:
            print("Aucun changement.")

    if not args.dry_run:
        state["stations"] = {i: {"available": c["available"], "price": c["price"]} for i, c in current.items()}
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)
