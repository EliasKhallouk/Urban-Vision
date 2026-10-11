"""Faisabilité de la carte des points de contention : temps perdu par tronçon (lecture seule).

Un passage de tronçon est un couple (arrêt i, arrêt i+1) du même trajet, de rangs consécutifs.
Temps perdu = retard au départ de i+1 − retard au départ de i ; il se décompose en temps perdu en
roulant (retard à l'arrivée en i+1 − retard au départ de i) et à l'arrêt (départ − arrivée en i+1).
Ne modifie jamais la base : connexion en lecture seule, sans import de db.py.
"""

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import indicateurs as ind

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "urban_vision.db"
SCHOOL_START = "2026-09-01"
OUTLIER_SECONDS = 900
BIG_LOSS_SECONDS = 120
MIN_PASSAGES = 20
MIN_DAYS = 3
BOOTSTRAP_DRAWS = 300
DAY_TYPES = {0: "semaine", 1: "semaine", 2: "semaine", 3: "semaine", 4: "semaine", 5: "samedi", 6: "dimanche"}

PASSAGES_SQL = """
WITH bounds AS (
    SELECT trip_id, start_date, MIN(stop_sequence) first_seq, MAX(stop_sequence) last_seq
    FROM observations
    WHERE route_id IN ({routes}) AND departure_time >= ? AND departure_time < ?
    GROUP BY trip_id, start_date
),
s AS (
    SELECT o.trip_id, o.start_date, o.route_id, o.direction_id, o.stop_sequence seq, o.stop_id,
           o.departure_time dep_time, o.departure_delay dep, o.arrival_delay arr,
           LAG(o.stop_id) OVER w prev_stop_id, LAG(o.stop_sequence) OVER w prev_seq,
           LAG(o.departure_delay) OVER w prev_dep, LAG(o.departure_time) OVER w prev_dep_time
    FROM observations o
    WHERE o.route_id IN ({routes}) AND +o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL AND o.departure_time >= ? AND o.departure_time < ?
    WINDOW w AS (PARTITION BY o.trip_id, o.start_date ORDER BY o.stop_sequence)
)
SELECT s.*, b.first_seq, b.last_seq
FROM s JOIN bounds b ON b.trip_id = s.trip_id AND b.start_date = s.start_date
WHERE s.prev_seq IS NOT NULL
"""


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp())


def route_ids(conn, short_names) -> dict:
    out = {}
    for name in short_names:
        row = conn.execute("SELECT route_id FROM routes WHERE route_short_name = ? OR route_id = ?",
                           (name, name)).fetchone()
        if row is None:
            raise SystemExit(f"Ligne inconnue : {name}")
        out[row[0]] = name
    return out


def witness_bus(conn, since: str, end: str, top: int = 10) -> str:
    """Ligne de bus fréquente au temps réel le plus complet : la moins de départs nuls parmi les `top` plus fréquentées."""
    stats = {}
    for route_id, histogram in conn.execute(
            "SELECT d.route_id, d.histogram FROM agg_daily d JOIN routes r ON r.route_id = d.route_id "
            "WHERE r.route_type = 3 AND d.date_service >= ? AND d.date_service < ?", (since, end)):
        counts = json.loads(histogram or "{}")
        total, zeros = stats.get(route_id, (0, 0))
        stats[route_id] = (total + sum(counts.values()), zeros + counts.get("0", 0))
    frequent = sorted(stats.items(), key=lambda kv: kv[1][0], reverse=True)[:top]
    return min(frequent, key=lambda kv: kv[1][1] / kv[1][0])[0]


def segment_passages(conn, routes, since: str, end: str) -> pd.DataFrame:
    """Passages de tronçon bruts (avant nettoyage), avec jour de service, type de jour et heure d'entrée."""
    marks = ", ".join("?" for _ in routes)
    sql = PASSAGES_SQL.format(routes=marks)
    params = [*routes, _ts(since), _ts(end), *routes, _ts(since), _ts(end)]
    df = pd.read_sql_query(sql, conn, params=params)
    entry = pd.to_datetime(df["prev_dep_time"], unit="s", utc=True).dt.tz_convert("Europe/Paris")
    df["date_service"] = entry.dt.strftime("%Y-%m-%d")
    df["day_type"] = entry.dt.weekday.map(DAY_TYPES)
    df["heure"] = entry.dt.hour
    df["gain"] = df["dep"] - df["prev_dep"]
    df["run"] = df["arr"] - df["prev_dep"]
    df["dwell"] = df["dep"] - df["arr"]
    df["consecutive"] = df["seq"] - df["prev_seq"] == 1
    df["terminus"] = (df["prev_seq"] == df["first_seq"]) | (df["seq"] == df["last_seq"])
    df["zero"] = (df["dep"] == 0) | (df["prev_dep"] == 0)
    df["arr_ok"] = df["arr"].notna() & (df["arr"] != 0)
    df["outlier"] = df["gain"].abs() > OUTLIER_SECONDS
    return df


def clean(df: pd.DataFrame, excluded_days=()) -> tuple[pd.DataFrame, dict]:
    """Garde les passages mesurables ; renvoie aussi la part écartée par chaque filtre, dans l'ordre."""
    steps = [("jours incomplets", ~df["date_service"].isin(excluded_days)),
             ("arrêts non consécutifs", df["consecutive"]),
             ("début ou fin de ligne", ~df["terminus"]),
             ("retard exactement nul", ~df["zero"]),
             ("valeur aberrante", ~df["outlier"])]
    effect, kept = {}, df
    for label, mask in steps:
        before = len(kept)
        kept = kept[mask.loc[kept.index]]
        effect[label] = (before - len(kept)) / max(len(df), 1) * 100
    return kept, effect


def _median(values) -> float:
    return float(np.median(values)) if len(values) else float("nan")


def summarize(df: pd.DataFrame, keys=("prev_stop_id", "stop_id")) -> pd.DataFrame:
    """Par tronçon (et `keys` éventuelles) : médianes, part > 2 min, passages, jours."""
    g = df.groupby(list(keys))
    out = g.agg(passages=("gain", "size"), jours=("date_service", "nunique"),
                mediane=("gain", "median"),
                part_2min=("gain", lambda v: (v > BIG_LOSS_SECONDS).mean() * 100)).reset_index()
    run = df[df["arr_ok"]].groupby(list(keys)).agg(roulage=("run", "median"), arret=("dwell", "median"))
    out = out.merge(run.reset_index(), on=list(keys), how="left")
    out["assez"] = (out["passages"] >= MIN_PASSAGES) & (out["jours"] >= MIN_DAYS)
    return out


def bootstrap_halfwidth(df: pd.DataFrame, rng) -> pd.DataFrame:
    """Demi-largeur de l'intervalle à 90 % de la médiane, en tirant les jours avec remise."""
    rows = []
    for (a, b), sub in df.groupby(["prev_stop_id", "stop_id"]):
        days = {d: v["gain"].to_numpy() for d, v in sub.groupby("date_service")}
        if len(days) < MIN_DAYS or len(sub) < MIN_PASSAGES:
            continue
        keys = list(days)
        meds = [_median(np.concatenate([days[k] for k in rng.choice(keys, len(keys))]))
                for _ in range(BOOTSTRAP_DRAWS)]
        lo, hi = np.percentile(meds, [5, 95])
        rows.append({"prev_stop_id": a, "stop_id": b, "demi_ic90": (hi - lo) / 2})
    return pd.DataFrame(rows, columns=["prev_stop_id", "stop_id", "demi_ic90"])


def day_spread(df: pd.DataFrame) -> pd.DataFrame:
    """Écart-type, d'un jour à l'autre, de la médiane journalière de chaque tronçon (jours d'au moins 5 passages)."""
    daily = df.groupby(["prev_stop_id", "stop_id", "date_service"])["gain"].agg(["median", "size"]).reset_index()
    daily = daily[daily["size"] >= 5]
    spread = daily.groupby(["prev_stop_id", "stop_id"])["median"].agg(["std", "size"]).reset_index()
    return spread.rename(columns={"std": "ecart_jours", "size": "jours_mesures"})


def tier(value, thresholds) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "gris"
    low, high = thresholds
    return "vert" if value < low else "ocre" if value < high else "cuivre"


def split_concordance(df: pd.DataFrame, thresholds) -> tuple[float, int]:
    """Part des tronçons classés au même palier sur les jours pairs et impairs (rang du jour dans l'échantillon)."""
    days = sorted(df["date_service"].unique())
    half = {d: i % 2 for i, d in enumerate(days)}
    tiers = []
    for part in (0, 1):
        s = summarize(df[df["date_service"].map(half) == part])
        s = s[(s["passages"] >= MIN_PASSAGES // 2) & (s["jours"] >= 2)]
        tiers.append(s.set_index(["prev_stop_id", "stop_id"])["mediane"].map(lambda v: tier(v, thresholds)))
    both = pd.concat(tiers, axis=1, join="inner")
    return (float((both.iloc[:, 0] == both.iloc[:, 1]).mean() * 100) if len(both) else float("nan")), len(both)


def hourly_concordance(df: pd.DataFrame, thresholds) -> tuple[float, int]:
    """Comme `split_concordance`, par tronçon et par heure (cases d'au moins 10 passages de chaque côté)."""
    days = sorted(df["date_service"].unique())
    half = df["date_service"].map({d: i % 2 for i, d in enumerate(days)})
    keys = ["prev_stop_id", "stop_id", "heure"]
    parts = [df[half == p].groupby(keys)["gain"].agg(["median", "size"]) for p in (0, 1)]
    both = parts[0].join(parts[1], lsuffix="_a", rsuffix="_b", how="inner")
    both = both[(both["size_a"] >= 10) & (both["size_b"] >= 10)]
    if both.empty:
        return float("nan"), 0
    same = [tier(a, thresholds) == tier(b, thresholds) for a, b in zip(both["median_a"], both["median_b"])]
    return float(np.mean(same) * 100), len(both)


def zero_diagnosis(conn, route_id: str, since: str, end: str) -> dict:
    """D'où viennent les retards nuls : trajets entiers sans temps réel, ou certains rangs d'arrêt ?"""
    rows = pd.read_sql_query(
        """SELECT trip_id, start_date, stop_sequence, departure_delay FROM observations
           WHERE route_id = ? AND +schedule_relationship = 'SCHEDULED' AND departure_delay IS NOT NULL
                 AND departure_time >= ? AND departure_time < ?""",
        conn, params=[route_id, _ts(since), _ts(end)])
    rows["zero"] = rows["departure_delay"] == 0
    trips = rows.groupby(["trip_id", "start_date"]).agg(n=("zero", "size"), zeros=("zero", "sum"),
                                                         first=("stop_sequence", "min"), last=("stop_sequence", "max"))
    rows = rows.join(trips[["first", "last"]], on=["trip_id", "start_date"])
    at_first = rows["stop_sequence"] == rows["first"]
    at_last = rows["stop_sequence"] == rows["last"]
    middle = ~at_first & ~at_last
    nonzero = rows.loc[~rows["zero"], "departure_delay"]
    return {
        "departs_nuls": rows["zero"].mean() * 100,
        "trajets_entierement_nuls": (trips["zeros"] == trips["n"]).mean() * 100,
        "trajets_sans_aucun_nul": (trips["zeros"] == 0).mean() * 100,
        "nuls_au_premier_arret": rows.loc[at_first, "zero"].mean() * 100,
        "nuls_au_dernier_arret": rows.loc[at_last, "zero"].mean() * 100,
        "nuls_en_ligne": rows.loc[middle, "zero"].mean() * 100,
        "part_des_nuls_hors_trajets_entiers": (rows["zero"] & ~rows.join(
            (trips["zeros"] == trips["n"]).rename("full"), on=["trip_id", "start_date"])["full"]).sum()
        / max(rows["zero"].sum(), 1) * 100,
        "retards_non_nuls_multiples_de_60": (nonzero % 60 == 0).mean() * 100 if len(nonzero) else float("nan"),
        "trajets": len(trips),
    }


def stop_names(conn) -> dict:
    return dict(conn.execute("SELECT stop_id, stop_name FROM stops"))


def segment_order(df: pd.DataFrame) -> pd.DataFrame:
    """Rang moyen et direction dominante de chaque tronçon, pour l'afficher dans l'ordre de la ligne."""
    return (df.groupby(["prev_stop_id", "stop_id"])
            .agg(rang=("seq", "mean"), direction=("direction_id", lambda v: v.mode().iloc[0]))
            .reset_index())


def _project(points: np.ndarray, p: np.ndarray) -> float:
    """Abscisse curviligne (en degrés) du point du tracé le plus proche de p."""
    best, best_s, s = float("inf"), 0.0, 0.0
    for a, b in zip(points[:-1], points[1:]):
        ab = b - a
        length = float(np.hypot(*ab))
        t = 0.0 if length == 0 else max(0.0, min(1.0, float(np.dot(p - a, ab) / length ** 2)))
        d = float(np.hypot(*(a + t * ab - p)))
        if d < best:
            best, best_s = d, s + t * length
        s += length
    return best_s


def cut_shape(coords, s0: float, s1: float) -> np.ndarray:
    """Portion du tracé entre deux abscisses curvilignes."""
    pts = np.asarray(coords, dtype=float)
    seg = np.hypot(*np.diff(pts, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    lo, hi = min(s0, s1), max(s0, s1)

    def at(s):
        i = int(np.clip(np.searchsorted(cum, s) - 1, 0, len(seg) - 1))
        t = 0.0 if seg[i] == 0 else (s - cum[i]) / seg[i]
        return pts[i] + t * (pts[i + 1] - pts[i])

    inner = pts[(cum > lo) & (cum < hi)]
    return np.vstack([at(lo), inner, at(hi)])


def draw_line(conn, df_all: pd.DataFrame, summary_day: pd.DataFrame, summary_peak: pd.DataFrame,
              route_id: str, label: str, thresholds, peak: int, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sys.path.insert(0, str(ROOT / "reports"))
    from palette import COPPERWOOD, OLIVE_LEAF, SUNLIT_CLAY

    colors = {"vert": OLIVE_LEAF, "ocre": SUNLIT_CLAY, "cuivre": COPPERWOOD, "gris": "#B8B8B8"}
    shapes = {int(d): json.loads(c) for d, c in conn.execute(
        "SELECT direction_id, coords FROM route_shapes WHERE route_id = ?", (route_id,))}
    coords_by_stop = {sid: np.array([lon, lat]) for sid, lat, lon in conn.execute(
        "SELECT stop_id, stop_lat, stop_lon FROM stops")}
    line = df_all[(df_all["route_id"] == route_id) & df_all["consecutive"]]
    segs = line.groupby(["prev_stop_id", "stop_id"]).agg(
        direction=("direction_id", lambda v: v.mode().iloc[0]), terminus=("terminus", "mean"),
        passages=("gain", "size")).reset_index()
    segs = segs[segs["passages"] >= 0.02 * segs["passages"].max()]
    fig, axes = plt.subplots(len(shapes) or 1, 2, figsize=(12, 5.5 * max(len(shapes), 1)), squeeze=False)
    for col, (summary, title) in enumerate(((summary_peak, f"{peak} h"), (summary_day, "toute la journée"))):
        values = summary.set_index(["prev_stop_id", "stop_id"])
        for row, (direction, coords) in enumerate(sorted(shapes.items())):
            ax = axes[row][col]
            pts = np.asarray(coords, dtype=float)
            ax.plot(pts[:, 0], pts[:, 1], color="#E6E6E6", lw=7, solid_capstyle="round", zorder=1)
            drawn = segs[segs["direction"] == direction].assign(
                _ok=lambda x: [k in values.index and bool(values.loc[k, "assez"])
                               for k in zip(x["prev_stop_id"], x["stop_id"])]).sort_values("_ok")
            for seg in drawn.itertuples():
                if seg.prev_stop_id not in coords_by_stop or seg.stop_id not in coords_by_stop:
                    continue
                part = cut_shape(coords, _project(pts, coords_by_stop[seg.prev_stop_id]),
                                 _project(pts, coords_by_stop[seg.stop_id]))
                key = (seg.prev_stop_id, seg.stop_id)
                if seg.terminus > 0.5:
                    ax.plot(part[:, 0], part[:, 1], color="#B8B8B8", lw=4, ls=(0, (1, 1.5)), zorder=2)
                    continue
                ok = key in values.index and bool(values.loc[key, "assez"])
                c = colors[tier(values.loc[key, "mediane"], thresholds) if ok else "gris"]
                ax.plot(part[:, 0], part[:, 1], color=c, lw=5, solid_capstyle="butt", zorder=3)
            ax.set_title(f"{label}, sens {direction} : semaine type, {title}", fontsize=10, color="#283618")
            ax.set_aspect(1 / math.cos(math.radians(44.84)))
            ax.axis("off")
    low, high = thresholds
    fig.text(0.5, 0.02, f"Vert : perd moins de {low} s ; ocre : de {low} à {high} s ; cuivre : {high} s ou plus ; "
             "gris : pas assez de données ; pointillé : début ou fin de ligne", ha="center", fontsize=9, color="#283618")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--lignes", default="A", help="noms courts séparés par des virgules (défaut : A)")
    ap.add_argument("--temoin", default="auto", help="ligne de bus témoin (auto : la plus complète des fréquentes)")
    ap.add_argument("--depuis", default=SCHOOL_START)
    ap.add_argument("--jusqu-a", dest="until", default=None, help="fin exclue (défaut : aujourd'hui)")
    ap.add_argument("--seuils", default="15,40", help="seuils vert/ocre et ocre/cuivre en secondes")
    ap.add_argument("--reseau-jours", type=int, default=5, help="jours de semaine pour la distribution réseau")
    ap.add_argument("--sortie", default=str(ROOT / "data" / "analyses" / "troncons"))
    args = ap.parse_args(argv)
    thresholds = tuple(int(v) for v in args.seuils.split(","))
    until = args.until or datetime.now().strftime("%Y-%m-%d")
    out = Path(args.sortie)
    out.mkdir(parents=True, exist_ok=True)
    conn = connect(Path(args.db))
    excluded = set(ind.excluded_days(conn, args.depuis, until))
    names = stop_names(conn)
    lines = route_ids(conn, [n.strip() for n in args.lignes.split(",")])
    witness = witness_bus(conn, args.depuis, until) if args.temoin == "auto" else next(iter(route_ids(conn, [args.temoin])))
    lines.setdefault(witness, conn.execute("SELECT route_short_name FROM routes WHERE route_id = ?",
                                           (witness,)).fetchone()[0])
    rng = np.random.default_rng(1)
    report = [f"# Faisabilité des tronçons ({args.depuis} → {until}, base {Path(args.db).name})", "",
              f"Seuils provisoires : {thresholds[0]} s et {thresholds[1]} s. Jours incomplets écartés : "
              f"{', '.join(sorted(excluded)) or 'aucun'}.", ""]
    for route_id, label in lines.items():
        raw = segment_passages(conn, [route_id], args.depuis, until)
        diag = zero_diagnosis(conn, route_id, args.depuis, until)
        kept, effect = clean(raw, excluded)
        week = kept[kept["day_type"] == "semaine"]
        whole = summarize(week)
        hourly = summarize(week, ("prev_stop_id", "stop_id", "heure"))
        red = hourly[hourly["assez"]].assign(red=lambda x: x["mediane"] >= thresholds[1]).groupby("heure")["red"].sum()
        peak = int(red.idxmax()) if len(red) and red.max() > 0 else 8
        at_peak = hourly[hourly["heure"] == peak].drop(columns="heure")
        ci = bootstrap_halfwidth(week, rng)
        spread = day_spread(week)
        conc, n_conc = split_concordance(week, thresholds)
        conc_h, n_conc_h = hourly_concordance(week, thresholds)
        order = segment_order(kept)
        table = (whole.merge(at_peak[["prev_stop_id", "stop_id", "mediane", "passages"]],
                             on=["prev_stop_id", "stop_id"], how="left", suffixes=("", "_pointe"))
                 .merge(ci, on=["prev_stop_id", "stop_id"], how="left")
                 .merge(spread, on=["prev_stop_id", "stop_id"], how="left")
                 .merge(order, on=["prev_stop_id", "stop_id"], how="left")
                 .sort_values(["direction", "rang"]))
        table.insert(0, "troncon", [f"{names.get(a, a)} → {names.get(b, b)}"
                                    for a, b in zip(table["prev_stop_id"], table["stop_id"])])
        table.to_csv(out / f"troncons_{label}.csv", index=False)
        draw_line(conn, raw, whole, at_peak, route_id, f"Ligne {label}", thresholds, peak, out / f"ligne_{label}.png")
        per_hour = hourly["passages"]
        days = week["date_service"].nunique()
        report += [f"## Ligne {label} (route_id {route_id})", "",
                   "**Temps réel publié**", "",
                   *[f"- {k.replace('_', ' ')} : {v:.1f}" if isinstance(v, float) else f"- {k} : {v}"
                     for k, v in diag.items()], "",
                   "**Effet des filtres (part des passages bruts écartés, dans l'ordre)**", "",
                   *[f"- {k} : {v:.1f} %" for k, v in effect.items()],
                   f"- passages gardés : {len(kept)} sur {len(raw)} ; semaine type : {len(week)} passages, {days} jours", "",
                   "**Bruit (semaine type)**", "",
                   f"- tronçons : {len(whole)}, dont {int(whole['assez'].sum())} avec assez de données toute la journée",
                   f"- passages par tronçon et par heure : médiane {per_hour.median():.0f}, 10e centile "
                   f"{per_hour.quantile(.1):.0f}, part des cases avec assez de données "
                   f"{hourly['assez'].mean() * 100:.0f} %",
                   f"- demi-intervalle à 90 % de la médiane toute la journée : médiane {ci['demi_ic90'].median():.1f} s, "
                   f"90e centile {ci['demi_ic90'].quantile(.9):.1f} s",
                   f"- écart-type d'un jour à l'autre de la médiane journalière : médiane "
                   f"{spread['ecart_jours'].median():.1f} s, 90e centile {spread['ecart_jours'].quantile(.9):.1f} s",
                   f"- concordance des paliers entre jours pairs et impairs : {conc:.0f} % sur {n_conc} tronçons "
                   f"(toute la journée), {conc_h:.0f} % sur {n_conc_h} cases tronçon × heure",
                   f"- tronçons cuivre par heure : {red.astype(int).to_dict()}",
                   f"- heure la plus chargée en temps perdu : {peak} h", "",
                   "**Médianes des tronçons (semaine type, toute la journée)**", "",
                   f"- temps perdu : {whole.loc[whole['assez'], 'mediane'].describe(percentiles=[.1, .5, .9]).round(1).to_dict()}",
                   f"- en roulant : {whole.loc[whole['assez'], 'roulage'].describe(percentiles=[.1, .5, .9]).round(1).to_dict()}",
                   f"- à l'arrêt : {whole.loc[whole['assez'], 'arret'].describe(percentiles=[.1, .5, .9]).round(1).to_dict()}",
                   ""]
    if args.reseau_jours:
        days = [d for (d,) in conn.execute(
            "SELECT DISTINCT date_service FROM agg_daily WHERE date_service >= ? AND date_service < ? ORDER BY 1",
            (args.depuis, until)) if d not in excluded and datetime.strptime(d, "%Y-%m-%d").weekday() < 5]
        sample = days[-args.reseau_jours:]
        all_routes = [r for (r,) in conn.execute(
            f"SELECT route_id FROM routes WHERE route_id NOT IN (SELECT route_id FROM routes WHERE route_long_name LIKE '%Flex%')")]
        frames = []
        for d in sample:
            nxt = (datetime.strptime(d, "%Y-%m-%d") + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            frames.append(clean(segment_passages(conn, all_routes, d, nxt), excluded)[0])
        net = pd.concat(frames)
        s = summarize(net, ("prev_stop_id", "stop_id", "heure"))
        s = s[(s["passages"] >= 8)]
        q = s["mediane"].quantile([.1, .25, .5, .75, .9, .95]).round(1).to_dict()
        whole_net = summarize(net)
        report += [f"## Réseau ({len(sample)} jours de semaine : {', '.join(sample)})", "",
                   f"- passages gardés : {len(net)} ; tronçons : {net.groupby(['prev_stop_id', 'stop_id']).ngroups}",
                   f"- médianes par tronçon et par heure (≥ 8 passages) : centiles {q}",
                   f"- médianes par tronçon toute la journée : centiles "
                   f"{whole_net.loc[whole_net['passages'] >= 20, 'mediane'].quantile([.1, .25, .5, .75, .9, .95]).round(1).to_dict()}",
                   *[f"- part des tronçons × heures au-dessus de {t} s : {(s['mediane'] >= t).mean() * 100:.1f} %"
                     for t in (10, 15, 20, 30, 45, 60, 90)], ""]
        s.to_csv(out / "reseau_troncons_heures.csv", index=False)
    (out / "rapport.md").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
