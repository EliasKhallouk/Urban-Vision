import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

METHOD_VERSION = "2.0"
EARLY_TOLERANCE_SECONDS = 60
LATE_TOLERANCE_SECONDS = 300
Z95 = 1.96
T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)
DOUBTFUL_ZERO_SHARE = 0.25
DOUBTFUL_MIN_PASSAGES = 200
MIN_PASSAGES = 50
FLEX_FILTER = "route_id NOT IN (SELECT route_id FROM routes WHERE route_long_name LIKE '%Flex%')"
DAY_TYPES = {0: "semaine", 1: "semaine", 2: "semaine", 3: "semaine", 4: "semaine", 5: "samedi", 6: "dimanche"}


@dataclass
class Indicateurs:
    score: float | None = None
    marge: float | None = None
    ponctualite: float | None = None
    marge_ponctualite: float | None = None
    service: float | None = None
    avance: float | None = None
    passages: int = 0
    attendus: float = 0.0
    jours: int = 0
    jours_exclus: list = field(default_factory=list)
    lignes_ecartees: dict = field(default_factory=dict)

    @property
    def disponible(self) -> bool:
        return self.score is not None


def student_quantile(n: int) -> float:
    return T975[n - 2] if n - 1 <= len(T975) else Z95


def ratio_estimate(ys, xs):
    total = sum(xs)
    if total <= 0:
        return None, None
    ratio = sum(ys) / total
    n = len(xs)
    if n < 2:
        return 100 * ratio, None
    variance = n / (n - 1) * sum((y - ratio * x) ** 2 for y, x in zip(ys, xs)) / total ** 2
    return 100 * ratio, 100 * student_quantile(n) * math.sqrt(variance)


def route_day_components(obs, on_time, eligible, skipped, cancelled=0, added=0, scheduled=0, early=0) -> dict:
    lost = max(cancelled - added, 0)
    operated = scheduled + added
    cancelled_passages = lost * eligible / operated if operated > 0 else 0.0
    assured = max(eligible - skipped, 0)
    return {
        "obs": obs,
        "on_time": on_time,
        "early": early,
        "expected": eligible + cancelled_passages,
        "assured": assured,
        "assured_on_time": on_time / obs * assured if obs > 0 else 0.0,
    }


def combine(days: dict, excluded=(), discarded=None) -> Indicateurs:
    kept = {d: v for d, v in days.items() if d not in excluded and v["expected"] > 0}
    result = Indicateurs(jours=len(kept), jours_exclus=sorted(d for d in days if d in excluded),
                         lignes_ecartees=dict(discarded or {}))
    if not kept:
        return result
    values = list(kept.values())
    result.score, result.marge = ratio_estimate([v["assured_on_time"] for v in values], [v["expected"] for v in values])
    result.ponctualite, result.marge_ponctualite = ratio_estimate([v["on_time"] for v in values], [v["obs"] for v in values])
    expected = sum(v["expected"] for v in values)
    result.service = 100 * sum(v["assured"] for v in values) / expected
    observed = sum(v["obs"] for v in values)
    result.avance = 100 * sum(v["early"] for v in values) / observed if observed else None
    result.passages = int(sum(v["obs"] for v in values))
    result.attendus = expected
    return result


def compare(current: Indicateurs, previous: Indicateurs) -> dict | None:
    if not current.disponible or not previous.disponible:
        return None
    diff = current.score - previous.score
    if current.marge is None or previous.marge is None:
        return {"ecart": diff, "marge": None, "significatif": None}
    margin = math.sqrt(current.marge ** 2 + previous.marge ** 2)
    return {"ecart": diff, "marge": margin, "significatif": abs(diff) > margin}


def regularity(sum_h2_act, sum_h_act, sum_h2_sch, sum_h_sch) -> dict | None:
    if not sum_h_act or not sum_h_sch:
        return None
    actual = sum_h2_act / (2 * sum_h_act)
    planned = sum_h2_sch / (2 * sum_h_sch)
    return {"attente_reelle": actual, "attente_prevue": planned, "attente_excedentaire": actual - planned}


def day_type(day: str) -> str:
    return DAY_TYPES[datetime.strptime(day, "%Y-%m-%d").weekday()]


def excluded_days(conn, since_day: str, end_day: str) -> list[str]:
    try:
        return [d for (d,) in conn.execute(
            "SELECT date_service FROM quality_days WHERE flag = 'incomplet' "
            "AND date_service >= ? AND date_service < ? ORDER BY 1", (since_day, end_day))]
    except sqlite3.Error:
        return []


def quality_flags(conn, since_day: str, end_day: str) -> dict:
    try:
        return dict(conn.execute(
            "SELECT date_service, flag FROM quality_days WHERE date_service >= ? AND date_service < ?",
            (since_day, end_day)))
    except sqlite3.Error:
        return {}


def doubtful_routes(conn, since_day: str, end_day: str) -> dict:
    zeros: dict = {}
    totals: dict = {}
    for route_id, histogram in conn.execute(
        "SELECT route_id, histogram FROM agg_daily WHERE date_service >= ? AND date_service < ?",
        (since_day, end_day),
    ):
        counts = json.loads(histogram or "{}")
        totals[route_id] = totals.get(route_id, 0) + sum(counts.values())
        zeros[route_id] = zeros.get(route_id, 0) + counts.get("0", 0)
    return {
        route_id: round(100 * zeros[route_id] / total, 1)
        for route_id, total in totals.items()
        if total >= DOUBTFUL_MIN_PASSAGES and zeros[route_id] / total >= DOUBTFUL_ZERO_SHARE
    }


def _scope_sql(routes=None, communes=None, stop_ids=None):
    params: list = []
    if communes or stop_ids:
        table = "agg_daily_stop"
        where = ""
        if communes:
            where += f" AND a.stop_id IN (SELECT stop_id FROM stop_municipalities WHERE commune_name IN ({', '.join('?' for _ in communes)}))"
            params += list(communes)
        if stop_ids:
            where += f" AND a.stop_id IN ({', '.join('?' for _ in stop_ids)})"
            params += list(stop_ids)
    else:
        table, where = "agg_daily", ""
    if routes:
        where += f" AND a.route_id IN ({', '.join('?' for _ in routes)})"
        params += list(routes)
    return table, where, params


def route_day_rows(conn, since_day, end_day, routes=None, communes=None, stop_ids=None) -> pd.DataFrame:
    table, where, params = _scope_sql(routes, communes, stop_ids)
    sql = f"""
        SELECT a.date_service, a.route_id,
               SUM(a.obs) obs, SUM(a.cnt_le300 - a.cnt_lt60) on_time, SUM(a.cnt_lt60) early,
               SUM(a.eligible) eligible, SUM(a.skipped) skipped,
               COALESCE(t.cancelled, 0) cancelled, COALESCE(t.added, 0) added, COALESCE(t.scheduled, 0) scheduled
        FROM {table} a
        LEFT JOIN agg_daily_trips t ON t.date_service = a.date_service AND t.route_id = a.route_id
        WHERE a.date_service >= ? AND a.date_service < ? AND a.{FLEX_FILTER} {where}
        GROUP BY a.date_service, a.route_id
    """
    return pd.read_sql_query(sql, conn, params=[since_day, end_day, *params])


def _by_day(rows: pd.DataFrame) -> dict:
    days: dict = {}
    for r in rows.itertuples(index=False):
        comp = route_day_components(r.obs, r.on_time, r.eligible, r.skipped, r.cancelled, r.added, r.scheduled, r.early)
        acc = days.setdefault(r.date_service, {k: 0.0 for k in comp})
        for k, v in comp.items():
            acc[k] += v
    return days


def indicators(conn, since_day, end_day, routes=None, communes=None, stop_ids=None, day_types=None) -> Indicateurs:
    rows = route_day_rows(conn, since_day, end_day, routes, communes, stop_ids)
    present = set(rows["route_id"])
    discarded = {r: z for r, z in doubtful_routes(conn, since_day, end_day).items() if r in present}
    if discarded:
        rows = rows[~rows["route_id"].isin(discarded)]
    if day_types:
        rows = rows[rows["date_service"].map(day_type).isin(day_types)]
    return combine(_by_day(rows), excluded_days(conn, since_day, end_day), discarded)


def route_indicators(conn, since_day, end_day, communes=None, min_passages: int = MIN_PASSAGES) -> pd.DataFrame:
    rows = route_day_rows(conn, since_day, end_day, communes=communes)
    excluded = excluded_days(conn, since_day, end_day)
    discarded = doubtful_routes(conn, since_day, end_day)
    regularity_by_route = route_regularity(conn, since_day, end_day, communes=communes)
    records = []
    for route_id, group in rows.groupby("route_id"):
        ind = combine(_by_day(group), excluded)
        if not ind.disponible or ind.passages < min_passages:
            continue
        reg = regularity_by_route.get(route_id)
        records.append({
            "route_id": route_id, "score_v2": ind.score, "marge_v2": ind.marge,
            "ponctualite_stricte": ind.ponctualite, "part_avance": ind.avance, "service_assure": ind.service,
            "passages": ind.passages, "jours": ind.jours,
            "attente_excedentaire_s": None if reg is None else reg["attente_excedentaire"],
            "temps_reel_douteux": route_id in discarded,
            "part_retards_nuls": discarded.get(route_id),
        })
    return pd.DataFrame(records, columns=[
        "route_id", "score_v2", "marge_v2", "ponctualite_stricte", "part_avance", "service_assure", "passages",
        "jours", "attente_excedentaire_s", "temps_reel_douteux", "part_retards_nuls",
    ])


def route_regularity(conn, since_day, end_day, communes=None, routes=None) -> dict:
    params: list = [since_day, end_day]
    where = ""
    if communes:
        where += f" AND stop_id IN (SELECT stop_id FROM stop_municipalities WHERE commune_name IN ({', '.join('?' for _ in communes)}))"
        params += list(communes)
    if routes:
        where += f" AND route_id IN ({', '.join('?' for _ in routes)})"
        params += list(routes)
    excluded = excluded_days(conn, since_day, end_day)
    if excluded:
        where += f" AND date_service NOT IN ({', '.join('?' for _ in excluded)})"
        params += excluded
    try:
        rows = conn.execute(
            f"""SELECT route_id, SUM(sum_h2_act), SUM(sum_h_act), SUM(sum_h2_sch), SUM(sum_h_sch),
                       COUNT(DISTINCT date_service)
                FROM agg_hourly_regularity
                WHERE date_service >= ? AND date_service < ? {where}
                GROUP BY route_id""", params).fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for route_id, h2a, ha, h2s, hs, days in rows:
        reg = regularity(h2a, ha, h2s, hs)
        if reg is not None:
            out[route_id] = {**reg, "jours": days}
    return out
