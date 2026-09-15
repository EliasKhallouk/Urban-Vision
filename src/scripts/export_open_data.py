"""Export open data des agrégats Urban Vision (CSV, lecture seule).

Régénère quatre datasets stables depuis les tables d'agrégation (jamais la
table brute des observations) : par ligne, par arrêt, par tranche horaire et
par commune. Utilisé par le collecteur (cron) et par le tableau de bord
(boutons de téléchargement « Données ouvertes »).

Intervalles demi-ouverts [since, end) sur `date_service` (AAAA-MM-JJ),
compatibles avec les bornes du dashboard (`_day_bounds`).
"""

import argparse
import csv
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "urban_vision.db"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "data" / "open_data"

MODE_LABELS = {0: "Tramway", 3: "Bus", 4: "Ferry", 2: "Rail", 5: "Câble",
               7: "Funiculaire", 11: "Trolleybus"}

DATASETS = ["lignes_journalier", "arrets_journalier", "horaire", "communes_journalier"]


def _median_seconds(hist_json):
    """Médiane exacte (à la seconde) d'un histogramme JSON, comme le dashboard."""
    if not hist_json:
        return None
    try:
        counts = json.loads(hist_json)
    except (TypeError, ValueError):
        return None
    items = sorted((int(k), int(v)) for k, v in counts.items() if int(v) > 0)
    if not items:
        return None
    total = sum(v for _, v in items)
    if total % 2 == 1:
        target = (total + 1) // 2
        cum = 0
        for sec, v in items:
            cum += v
            if cum >= target:
                return sec
    else:
        lower, upper = total // 2, total // 2 + 1
        cum, found = 0, []
        for sec, v in items:
            cum += v
            if len(found) == 0 and cum >= lower:
                found.append(sec)
            if cum >= upper:
                found.append(sec)
                break
        return (found[0] + found[1]) / 2.0


def _has_table(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _mode(route_type):
    return MODE_LABELS.get(route_type) if route_type is not None else ""


def _routes_join(conn):
    if _has_table(conn, "routes"):
        return ("LEFT JOIN routes r ON r.route_id = d.route_id",
                "r.route_type AS route_type",
                "COALESCE(r.route_short_name, d.route_id) AS ligne")
    return ("", "NULL AS route_type", "d.route_id AS ligne")


def _rows_lignes(conn, since, end):
    route_join, type_col, ligne_col = _routes_join(conn)
    rows = conn.execute(
        f"""
        SELECT d.date_service, d.route_id, {ligne_col}, {type_col},
               d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60,
               d.skipped, d.eligible, d.histogram
        FROM agg_daily d {route_join}
        WHERE d.date_service >= ? AND d.date_service < ?
        ORDER BY d.date_service, d.route_id
        """, (since, end),
    ).fetchall()
    out = []
    for r in rows:
        obs = int(r["obs"])
        eligible = int(r["eligible"])
        out.append({
            "date_service": r["date_service"],
            "route_id": r["route_id"],
            "ligne": r["ligne"],
            "route_type": r["route_type"],
            "mode": _mode(r["route_type"]),
            "observations": obs,
            "retard_moyen_s": round(r["sum_delay"] / obs, 2) if obs else None,
            "retard_median_s": _median_seconds(r["histogram"]),
            "pct_a_l_heure": round(r["cnt_le300"] / obs * 100, 2) if obs else None,
            "pct_retard_5min": round(r["cnt_gt300"] / obs * 100, 2) if obs else None,
            "pct_avance_1min": round(r["cnt_lt60"] / obs * 100, 2) if obs else None,
            "pct_arrets_sautes": round(r["skipped"] / eligible * 100, 2) if eligible else 0.0,
            "histogram": r["histogram"],
        })
    return out


def _directions(conn):
    if not _has_table(conn, "stop_direction"):
        return {}
    rows = conn.execute(
        "SELECT route_id, stop_id, terminus FROM stop_direction WHERE terminus IS NOT NULL"
    ).fetchall()
    return {(r["route_id"], r["stop_id"]): f"vers {r['terminus']}" for r in rows}


def _rows_arrets(conn, since, end):
    route_join, type_col, ligne_col = _routes_join(conn)
    if not _has_table(conn, "stops"):
        return []
    comm_ok = _has_table(conn, "stop_municipalities")
    commune_col = "sm.commune_name" if comm_ok else "NULL AS commune_name"
    joins = " JOIN stops s ON s.stop_id = d.stop_id"
    if comm_ok:
        joins += " LEFT JOIN stop_municipalities sm ON sm.stop_id = d.stop_id"
    rows = conn.execute(
        f"""
        SELECT d.date_service, d.route_id, {ligne_col}, {type_col},
               d.stop_id, s.stop_name, {commune_col}, s.stop_lat, s.stop_lon,
               d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60,
               d.skipped, d.eligible, d.histogram
        FROM agg_daily_stop d {route_join}{joins}
        WHERE d.date_service >= ? AND d.date_service < ?
        ORDER BY d.date_service, d.route_id, d.stop_id
        """, (since, end),
    ).fetchall()
    if not rows:
        return []
    directions = _directions(conn)
    out = []
    for r in rows:
        obs = int(r["obs"])
        eligible = int(r["eligible"])
        out.append({
            "date_service": r["date_service"],
            "route_id": r["route_id"],
            "ligne": r["ligne"],
            "route_type": r["route_type"],
            "mode": _mode(r["route_type"]),
            "stop_id": r["stop_id"],
            "stop_name": r["stop_name"],
            "commune": r["commune_name"],
            "direction": directions.get((r["route_id"], r["stop_id"]), ""),
            "stop_lat": r["stop_lat"],
            "stop_lon": r["stop_lon"],
            "observations": obs,
            "retard_moyen_s": round(r["sum_delay"] / obs, 2) if obs else None,
            "pct_a_l_heure": round(r["cnt_le300"] / obs * 100, 2) if obs else None,
            "pct_retard_5min": round(r["cnt_gt300"] / obs * 100, 2) if obs else None,
            "pct_avance_1min": round(r["cnt_lt60"] / obs * 100, 2) if obs else None,
            "pct_arrets_sautes": round(r["skipped"] / eligible * 100, 2) if eligible else 0.0,
            "histogram": r["histogram"],
        })
    return out


def _rows_horaire(conn, since, end):
    route_join, type_col, ligne_col = _routes_join(conn)
    rows = conn.execute(
        f"""
        SELECT d.date_service, d.route_id, {ligne_col}, {type_col}, d.heure,
               d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300
        FROM agg_hourly d {route_join}
        WHERE d.date_service >= ? AND d.date_service < ?
        ORDER BY d.date_service, d.route_id, d.heure
        """, (since, end),
    ).fetchall()
    out = []
    for r in rows:
        obs = int(r["obs"])
        out.append({
            "date_service": r["date_service"],
            "route_id": r["route_id"],
            "ligne": r["ligne"],
            "route_type": r["route_type"],
            "mode": _mode(r["route_type"]),
            "heure": int(r["heure"]),
            "observations": obs,
            "retard_moyen_s": round(r["sum_delay"] / obs, 2) if obs else None,
            "pct_a_l_heure": round(r["cnt_le300"] / obs * 100, 2) if obs else None,
            "pct_retard_5min": round(r["cnt_gt300"] / obs * 100, 2) if obs else None,
        })
    return out


def _rows_communes(conn, since, end):
    if not _has_table(conn, "stop_municipalities"):
        return []
    rows = conn.execute(
        """
        SELECT d.date_service, sm.commune_name, MIN(sm.insee_code) AS insee_code,
               SUM(d.obs) AS obs, SUM(d.sum_delay) AS sum_delay,
               SUM(d.cnt_le300) AS cnt_le300, SUM(d.cnt_gt300) AS cnt_gt300,
               SUM(d.skipped) AS skipped, SUM(d.eligible) AS eligible,
               COUNT(DISTINCT d.route_id) AS n_lignes
        FROM agg_daily_stop d
        JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
        WHERE d.date_service >= ? AND d.date_service < ?
        GROUP BY d.date_service, sm.commune_name
        ORDER BY d.date_service, sm.commune_name
        """, (since, end),
    ).fetchall()
    out = []
    for r in rows:
        obs = int(r["obs"])
        eligible = int(r["eligible"])
        out.append({
            "date_service": r["date_service"],
            "commune": r["commune_name"],
            "insee_code": r["insee_code"],
            "observations": obs,
            "retard_moyen_s": round(r["sum_delay"] / obs, 2) if obs else None,
            "pct_a_l_heure": round(r["cnt_le300"] / obs * 100, 2) if obs else None,
            "pct_retard_5min": round(r["cnt_gt300"] / obs * 100, 2) if obs else None,
            "pct_arrets_sautes": round(r["skipped"] / eligible * 100, 2) if eligible else 0.0,
            "n_lignes": int(r["n_lignes"]),
        })
    return out


_DATASET_FNS = {
    "lignes_journalier": _rows_lignes,
    "arrets_journalier": _rows_arrets,
    "horaire": _rows_horaire,
    "communes_journalier": _rows_communes,
}


def dataset_rows(conn, name, since="0000-00-00", end="9999-12-31"):
    """Lignes d'un dataset sous forme de listes de dictionnaires (colonnes stables)."""
    if name not in _DATASET_FNS:
        raise ValueError(f"dataset inconnu : {name} (attendu : {DATASETS})")
    return _DATASET_FNS[name](conn, since, end)


def write_datasets(conn, out_dir, since="0000-00-00", end="9999-12-31"):
    """Écrit les 4 CSVs (UTF-8 BOM) et le METADATA.json ; renvoie ce dernier."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in DATASETS:
        rows = dataset_rows(conn, name, since, end)
        counts[name] = len(rows)
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as fh:
            if rows:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
    meta = {
        "dataset": "urban-vision-fiabilite",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since_day": since,
        "end_day": end,
        "license": "projet Urban Vision (agrégats, sans données personnelles)",
        "row_counts": counts,
    }
    with open(out_dir / "METADATA.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return meta


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=str(DB_PATH), help="base SQLite (défaut : racine du projet)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="dossier de sortie")
    parser.add_argument("--since", default=None, help="AAAA-MM-JJ inclusif (défaut : tout l'historique)")
    parser.add_argument("--until", default=None, help="AAAA-MM-JJ inclusif (défaut : tout l'historique)")
    parser.add_argument("--print-datasets", action="store_true", help="liste les datasets disponibles")
    args = parser.parse_args(argv)

    if args.print_datasets:
        print("\n".join(DATASETS))
        return 0

    until_day = args.until if args.until else "9999-12-31"
    if args.since and until_day != "9999-12-31" and args.since > until_day:
        parser.error("--since doit précéder --until")
    end = (date.fromisoformat(until_day) + timedelta(days=1)).isoformat() if until_day != "9999-12-31" else "9999-12-31"
    since = args.since if args.since else "0000-00-00"

    if not Path(args.db).exists():
        parser.error(f"base introuvable : {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("BEGIN")
    try:
        meta = write_datasets(conn, args.out, since, end)
    finally:
        conn.execute("ROLLBACK")
        conn.close()

    total = sum(meta["row_counts"].values())
    parts = ", ".join(f"{n} {c}" for n, c in meta["row_counts"].items())
    print(f"{args.out} : {total} lignes au total ({parts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())