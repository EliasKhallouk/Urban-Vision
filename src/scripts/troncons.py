"""Carte des points de contention : temps que les bus perdent entre deux arrêts consécutifs.

Un passage de tronçon est un couple (arrêt i, arrêt i+1) du même trajet, de rangs consécutifs.
Temps perdu = retard au départ de i+1 − retard au départ de i (circulation, feux ou montée des
voyageurs), aussi rapporté au temps prévu entre les deux arrêts. Chaque nuit :
1. les passages nettoyés de chaque jour manquant vont dans `troncons_jour` ;
2. les médianes par jour type (semaine, samedi, dimanche) et par heure, sur la période scolaire,
   vont dans `troncons_carte`, avec le statut de chaque tronçon ;
3. `troncons_lignes` liste les lignes de chaque tronçon et `troncons_geo` sa géométrie.
Le tram n'est pas mesuré (temps réel publié par paliers, section 10.5 de la documentation).
"""

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import indicateurs as ind

_REPORTS = str(Path(__file__).resolve().parents[2] / "reports")
if _REPORTS not in sys.path:
    sys.path.insert(0, _REPORTS)
from palette import troncon_tier  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "urban_vision.db"
LOG_PATH = ROOT / "data" / "collect.log"
PERIOD_START = os.environ.get("URBAN_VISION_TRONCONS_DEPUIS", "2026-09-01")
TRAM = 0
LOOKBACK_SECONDS = 3 * 3600
OUTLIER_SECONDS = 900
MAX_SCHEDULED_SECONDS = 1800
BIG_LOSS_SECONDS = 120
MIN_PASSAGES = 20
MIN_DAYS = 3
MAX_ZERO_SHARE = ind.DOUBTFUL_ZERO_SHARE * 100
MAX_PLATEAU_SHARE = 15.0
WHOLE_DAY = -1
DAY_TYPES = {0: "semaine", 1: "semaine", 2: "semaine", 3: "semaine", 4: "semaine", 5: "samedi", 6: "dimanche"}
MEASURED, FEW, TERMINUS, DOUBTFUL = "mesure", "peu", "terminus", "douteux"
SNAP_METERS = 150.0
BACKFILL_PAUSE_SECONDS = 1.0
PARTITIONS = [f"GLOB '*{d}'" for d in "0123456789"] + ["NOT GLOB '*[0-9]'"]

logger = logging.getLogger("troncons")

DAY_SQL = """
SELECT o.trip_id, o.start_date, o.route_id, o.stop_sequence AS seq, o.stop_id,
       o.schedule_relationship AS rel, o.departure_time, o.departure_delay AS dep
FROM observations o
WHERE o.departure_time >= ? AND o.departure_time < ?
  AND o.route_id IN (SELECT route_id FROM routes
                     WHERE route_type <> 0 AND COALESCE(route_long_name, '') NOT LIKE '%Flex%')
"""


def day_bounds(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d")
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def passages_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """Passages de tronçon d'un lot d'observations (colonnes de DAY_SQL), avec leurs drapeaux.

    Colonnes ajoutées : prev_stop_id, gain (s), sched (temps prévu, s), date_service et heure
    d'entrée sur le tronçon (heure locale du départ de l'arrêt i), consecutive, terminus (première
    ou dernière paire du trajet), zero (un des deux retards vaut exactement 0), outlier, kept.
    """
    cols = ["trip_id", "start_date", "route_id", "prev_stop_id", "stop_id", "gain", "sched", "pct",
            "date_service", "heure", "consecutive", "terminus", "zero", "outlier", "kept"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    bounds = rows.groupby(["trip_id", "start_date"])["seq"].agg(first_seq="min", last_seq="max")
    s = rows[(rows["rel"] == "SCHEDULED") & rows["dep"].notna() & rows["departure_time"].notna()]
    s = s.sort_values(["trip_id", "start_date", "seq"])
    g = s.groupby(["trip_id", "start_date"], sort=False)
    s = s.assign(prev_stop_id=g["stop_id"].shift(), prev_seq=g["seq"].shift(), prev_dep=g["dep"].shift(),
                 prev_time=g["departure_time"].shift())
    s = s[s["prev_seq"].notna()].join(bounds, on=["trip_id", "start_date"])
    entry = pd.to_datetime(s["prev_time"], unit="s", utc=True).dt.tz_convert("Europe/Paris")
    out = pd.DataFrame({
        "trip_id": s["trip_id"], "start_date": s["start_date"], "route_id": s["route_id"],
        "prev_stop_id": s["prev_stop_id"].astype(str), "stop_id": s["stop_id"].astype(str),
        "gain": (s["dep"] - s["prev_dep"]).astype(int),
        "sched": ((s["departure_time"] - s["dep"]) - (s["prev_time"] - s["prev_dep"])).astype(int),
        "date_service": entry.dt.strftime("%Y-%m-%d"), "heure": entry.dt.hour.astype(int),
        "consecutive": (s["seq"] - s["prev_seq"]) == 1,
        "terminus": (s["prev_seq"] == s["first_seq"]) | (s["seq"] == s["last_seq"]),
        "zero": (s["dep"] == 0) | (s["prev_dep"] == 0),
    })
    out["outlier"] = out["gain"].abs() > OUTLIER_SECONDS
    valid_sched = (out["sched"] > 0) & (out["sched"] <= MAX_SCHEDULED_SECONDS)
    out["pct"] = np.where(valid_sched, out["gain"] / out["sched"].where(valid_sched, 1) * 100, np.nan).round()
    out["kept"] = out["consecutive"] & ~out["terminus"] & ~out["zero"] & ~out["outlier"] & valid_sched
    return out[cols].reset_index(drop=True)


def day_rows(passages: pd.DataFrame, day: str) -> pd.DataFrame:
    """Lignes de `troncons_jour` d'un jour : une par tronçon, ligne et heure, valeurs gardées en texte."""
    cols = ["date_service", "from_stop_id", "to_stop_id", "route_id", "heure", "n", "n_zero", "n_terminus",
            "n_plateau", "gains", "pcts"]
    p = passages[(passages["date_service"] == day) & passages["consecutive"]]
    if p.empty:
        return pd.DataFrame(columns=cols)
    keys = ["prev_stop_id", "stop_id", "route_id", "heure"]
    counts = p.assign(n_zero=p["zero"] & ~p["terminus"], n_plateau=p["kept"] & (p["gain"] == 0)).groupby(keys).agg(
        n=("kept", "sum"), n_zero=("n_zero", "sum"), n_terminus=("terminus", "sum"), n_plateau=("n_plateau", "sum"))
    kept = p[p["kept"]].groupby(keys).agg(
        gains=("gain", lambda v: " ".join(str(int(x)) for x in v)),
        pcts=("pct", lambda v: " ".join(str(int(x)) for x in v)))
    out = counts.join(kept).fillna({"gains": "", "pcts": ""}).reset_index()
    out = out.rename(columns={"prev_stop_id": "from_stop_id", "stop_id": "to_stop_id"})
    out.insert(0, "date_service", day)
    return out[cols].astype({"n": int, "n_zero": int, "n_terminus": int, "n_plateau": int})


def refresh_day(conn, day: str) -> int:
    """(Re)calcule `troncons_jour` pour un jour de service ; renvoie le nombre de passages gardés."""
    start, end = day_bounds(day)
    rows = pd.read_sql_query(DAY_SQL, conn, params=(start - LOOKBACK_SECONDS, end))
    out = day_rows(passages_frame(rows), day)
    conn.execute("DELETE FROM troncons_jour WHERE date_service = ?", (day,))
    conn.executemany(
        "INSERT INTO troncons_jour (date_service, from_stop_id, to_stop_id, route_id, heure, n, n_zero, "
        "n_terminus, n_plateau, gains, pcts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        out.itertuples(index=False, name=None))
    kept = int(out["n"].sum()) if not out.empty else 0
    conn.execute("INSERT OR REPLACE INTO troncons_jours_faits VALUES (?, ?, ?)", (day, kept, int(time.time())))
    conn.commit()
    return kept


def missing_days(conn, since: str, today: str) -> list[str]:
    """Jours de `agg_daily` depuis `since`, avant aujourd'hui, que `troncons_jour` n'a pas encore traités."""
    done = {d for (d,) in conn.execute("SELECT date_service FROM troncons_jours_faits")}
    return [d for (d,) in conn.execute(
        "SELECT DISTINCT date_service FROM agg_daily WHERE date_service >= ? AND date_service < ? ORDER BY 1",
        (since, today)) if d not in done]


def line_quality(conn, since: str, end: str) -> dict:
    """Qualité du temps réel de chaque ligne mesurée sur la période : part de départs nuls et de paliers.

    Une ligne est exploitable si moins de MAX_ZERO_SHARE % de ses retards valent exactement 0 et
    moins de MAX_PLATEAU_SHARE % de ses passages gardés ont un temps perdu de 0 s pile (retard
    recopié d'un arrêt à l'autre au lieu d'être mesuré).
    """
    zeros: dict = {}
    for route_id, histogram in conn.execute(
            "SELECT route_id, histogram FROM agg_daily WHERE date_service >= ? AND date_service < ?", (since, end)):
        counts = json.loads(histogram or "{}")
        total, zero = zeros.get(route_id, (0, 0))
        zeros[route_id] = (total + sum(counts.values()), zero + counts.get("0", 0))
    out = {}
    for route_id, n, plateau in conn.execute(
            "SELECT route_id, SUM(n), SUM(n_plateau) FROM troncons_jour WHERE date_service >= ? AND date_service < ? "
            "GROUP BY route_id", (since, end)):
        total, zero = zeros.get(route_id, (0, 0))
        zero_share = zero / total * 100 if total else 100.0
        plateau_share = plateau / n * 100 if n else 100.0
        out[route_id] = {"zeros": round(zero_share, 1), "paliers": round(plateau_share, 1), "passages": int(n),
                         "ok": zero_share < MAX_ZERO_SHARE and plateau_share < MAX_PLATEAU_SHARE}
    return out


def _values(column: pd.Series) -> pd.Series:
    return column.str.split().explode().dropna().astype(int)


def summarize_cells(rows: pd.DataFrame, good_routes: set) -> pd.DataFrame:
    """Statut et médianes de chaque case (jour type, heure, tronçon), plus la journée entière (heure = -1).

    `rows` : lignes de `troncons_jour` (avec jour_type). Les lignes hors `good_routes` ne comptent
    que pour signaler un tronçon « douteux » quand elles sont seules à le parcourir.
    """
    cols = ["jour_type", "heure", "from_stop_id", "to_stop_id", "statut", "palier", "mediane_s", "mediane_pct",
            "passages", "part_2min", "jours"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    rows = pd.concat([rows, rows.assign(heure=WHOLE_DAY)], ignore_index=True)
    keys = ["jour_type", "heure", "from_stop_id", "to_stop_id"]
    good = rows["route_id"].isin(good_routes)
    presence = rows.assign(good_rows=good, term=rows["n_terminus"]).groupby(keys).agg(
        any_good=("good_rows", "any"), terminus=("term", "sum"), measurable=("n", "sum"))
    measured = rows[good & (rows["n"] > 0)]
    if measured.empty:
        stats = pd.DataFrame(index=presence.index, columns=["mediane_s", "mediane_pct", "passages", "part_2min", "jours"],
                             dtype=float)
    else:
        vals = pd.DataFrame({"gain": _values(measured["gains"]), "pct": _values(measured["pcts"])})
        vals = vals.join(measured[keys + ["date_service"]])
        g = vals.groupby(keys)
        stats = pd.DataFrame({"mediane_s": g["gain"].median(), "mediane_pct": g["pct"].median(),
                              "passages": g["gain"].size(),
                              "part_2min": g["gain"].apply(lambda v: float((v > BIG_LOSS_SECONDS).mean() * 100)),
                              "jours": g["date_service"].nunique()})
    out = presence.join(stats).reset_index()
    out["passages"] = out["passages"].fillna(0).astype(int)
    out["jours"] = out["jours"].fillna(0).astype(int)
    enough = (out["passages"] >= MIN_PASSAGES) & (out["jours"] >= MIN_DAYS)
    out["statut"] = np.select(
        [(out["measurable"] == 0) & (out["terminus"] > 0), ~out["any_good"], ~enough],
        [TERMINUS, DOUBTFUL, FEW], default=MEASURED)
    out["palier"] = [troncon_tier(s, p) if st == MEASURED else None
                     for s, p, st in zip(out["mediane_s"], out["mediane_pct"], out["statut"])]
    return out[cols]


def build_map(conn, since: str, end: str) -> dict:
    """Recalcule `troncons_carte`, `troncons_lignes` et la qualité des lignes (`troncons_meta`)."""
    excluded = set(ind.excluded_days(conn, since, end))
    quality = line_quality(conn, since, end)
    good = {r for r, q in quality.items() if q["ok"]}
    conn.execute("DELETE FROM troncons_carte")
    conn.execute("DELETE FROM troncons_lignes")
    for part in PARTITIONS:
        rows = pd.read_sql_query(
            f"SELECT date_service, from_stop_id, to_stop_id, route_id, heure, n, n_terminus, gains, pcts "
            f"FROM troncons_jour WHERE date_service >= ? AND date_service < ? AND from_stop_id {part}",
            conn, params=(since, end))
        rows = rows[~rows["date_service"].isin(excluded)]
        if rows.empty:
            continue
        rows["jour_type"] = pd.to_datetime(rows["date_service"]).dt.weekday.map(DAY_TYPES)
        cells = summarize_cells(rows, good)
        conn.executemany(
            "INSERT INTO troncons_carte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(None if (isinstance(v, float) and math.isnan(v)) else v for v in r)
             for r in cells.itertuples(index=False, name=None)])
        lines = rows.groupby(["from_stop_id", "to_stop_id", "route_id"])["n"].sum().reset_index()
        conn.executemany("INSERT INTO troncons_lignes VALUES (?, ?, ?, ?)", lines.itertuples(index=False, name=None))
    meta = {"depuis": since, "jusqu_a": end, "calcule_le": int(time.time()), "jours_exclus": sorted(excluded),
            "lignes": quality}
    conn.executemany("INSERT OR REPLACE INTO troncons_meta VALUES (?, ?)",
                     [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
    conn.commit()
    return meta


def _xy(lon: float, lat: float, lat0: float) -> tuple[float, float]:
    return lon * 111_320.0 * math.cos(math.radians(lat0)), lat * 111_320.0


def project(points: list, p: tuple) -> tuple[float, float]:
    """(abscisse curviligne, distance) en mètres du point du tracé le plus proche de p (lon, lat)."""
    lat0 = p[1]
    xy = [_xy(lon, lat, lat0) for lon, lat in points]
    px, py = _xy(p[0], p[1], lat0)
    best_d, best_s, s = float("inf"), 0.0, 0.0
    for (ax, ay), (bx, by) in zip(xy[:-1], xy[1:]):
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length ** 2))
        d = math.hypot(ax + t * dx - px, ay + t * dy - py)
        if d < best_d:
            best_d, best_s = d, s + t * length
        s += length
    return best_s, best_d


def cut(points: list, s0: float, s1: float, lat0: float) -> list:
    """Portion du tracé entre deux abscisses curvilignes (s0 < s1), en [lon, lat]."""
    out, s = [], 0.0
    for (alon, alat), (blon, blat) in zip(points[:-1], points[1:]):
        ax, ay = _xy(alon, alat, lat0)
        bx, by = _xy(blon, blat, lat0)
        length = math.hypot(bx - ax, by - ay)
        if length > 0 and s + length >= s0 and s <= s1:
            t0, t1 = max(0.0, (s0 - s) / length), min(1.0, (s1 - s) / length)
            start = [alon + t0 * (blon - alon), alat + t0 * (blat - alat)]
            stop = [alon + t1 * (blon - alon), alat + t1 * (blat - alat)]
            if not out:
                out.append(start)
            out.append(stop)
        s += length
    return [[round(x, 6), round(y, 6)] for x, y in out]


def segment_geometry(shapes: list, a: tuple, b: tuple) -> list:
    """Tracé d'un tronçon : portion du premier tracé de ligne qui passe près des deux arrêts dans le bon
    ordre (à moins de SNAP_METERS), sinon segment droit entre les deux arrêts."""
    best = None
    for points in shapes:
        if len(points) < 2:
            continue
        s0, d0 = project(points, a)
        s1, d1 = project(points, b)
        if d0 <= SNAP_METERS and d1 <= SNAP_METERS and s1 > s0 and (best is None or d0 + d1 < best[0]):
            best = (d0 + d1, points, s0, s1)
    if best is not None:
        part = cut(best[1], best[2], best[3], a[1])
        if len(part) >= 2:
            return part
    return [[round(a[0], 6), round(a[1], 6)], [round(b[0], 6), round(b[1], 6)]]


def build_geometry(conn) -> int:
    """Recalcule `troncons_geo` pour tous les tronçons de `troncons_lignes`, depuis `route_shapes`."""
    stops = {sid: (lon, lat) for sid, lat, lon in conn.execute("SELECT stop_id, stop_lat, stop_lon FROM stops")}
    shapes: dict = {}
    for route_id, coords in conn.execute("SELECT route_id, coords FROM route_shapes"):
        shapes.setdefault(route_id, []).append(json.loads(coords))
    route_types = dict(conn.execute("SELECT route_id, route_type FROM routes"))
    main = pd.read_sql_query(
        "SELECT from_stop_id, to_stop_id, route_id, passages FROM troncons_lignes", conn)
    conn.execute("DELETE FROM troncons_geo")
    if main.empty:
        conn.commit()
        return 0
    main = main.sort_values("passages", ascending=False)
    rows = []
    for (a, b), sub in main.groupby(["from_stop_id", "to_stop_id"], sort=False):
        if a not in stops or b not in stops:
            continue
        routes = list(sub["route_id"])
        candidate = [pts for r in routes for pts in shapes.get(r, [])]
        rows.append((a, b, int(route_types.get(routes[0], 3)),
                     json.dumps(segment_geometry(candidate, stops[a], stops[b]), separators=(",", ":"))))
    conn.executemany("INSERT INTO troncons_geo VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def run(conn, since: str, today: str, pause: float = BACKFILL_PAUSE_SECONDS) -> dict:
    days = missing_days(conn, since, today)
    if days:
        logger.info("Tronçons : %d jour(s) à calculer…", len(days))
    start = time.monotonic()
    for day in days:
        refresh_day(conn, day)
        time.sleep(pause)
    meta = build_map(conn, since, today)
    n_geo = build_geometry(conn)
    logger.info("Tronçons : carte recalculée en %.0f s (%d jour(s) ajouté(s), %d tronçons)",
                time.monotonic() - start, len(days), n_geo)
    return meta


def configure_logging(log_path: Path) -> None:
    handlers = [logging.StreamHandler()]
    if log_path.parent.exists():
        handlers.append(logging.FileHandler(str(log_path)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers, force=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Recalcule la carte des points de contention (tronçons).")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--log", default=str(LOG_PATH))
    ap.add_argument("--depuis", default=PERIOD_START, help="début de la période (AAAA-MM-JJ, défaut : rentrée)")
    args = ap.parse_args(argv)
    configure_logging(Path(args.log))
    import db as dbio

    conn = sqlite3.connect(args.db, timeout=120)
    conn.execute("PRAGMA busy_timeout = 120000")
    try:
        conn.executescript(dbio.TRONCONS_DDL)
        run(conn, args.depuis, date.today().isoformat())
    except Exception as e:
        logger.warning("Tronçons : calcul échoué : %s", e)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
