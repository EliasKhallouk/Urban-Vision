#!/usr/bin/env python3
"""Génère un rapport mensuel Urban Vision au format LaTeX/PDF.

Le document commence volontairement par une synthèse exécutive d'une page :
elle est destinée aux décideurs. Les tableaux complets sont reportés en annexe.
Une version territoriale s'obtient en fournissant un profil de destinataire
dont les lignes ont été vérifiées (voir recipients.example.json).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

matplotlib.rcParams.update({
    "font.family": "Inter",
    "font.size": 9,
    "axes.unicode_minus": False,
})

_REPORTS_DIR = str(Path(__file__).resolve().parent)
if _REPORTS_DIR not in sys.path:
    sys.path.insert(0, _REPORTS_DIR)
from palette import (  # noqa: E402  (module partagé de charte et de seuils)
    BLACK_FOREST, COPPERWOOD, OLIVE_LEAF, SUNLIT_CLAY, CORNSILK, WHITE, TEAL,
    hex as palette_hex, kpi_latex as palette_kpi_latex,
)

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "src" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import indicateurs as ind  # noqa: E402
from db import QUALITY_INCOMPLETE_HOURS  # noqa: E402

plt.rcParams["axes.prop_cycle"] = plt.cycler(color=[BLACK_FOREST, COPPERWOOD, OLIVE_LEAF, SUNLIT_CLAY, TEAL])

SUNLIT_CLAY_TINT = "#F6E7D7"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "urban_vision.db"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "output"
FRESHNESS_BUFFER_SECONDS = 20 * 60
# Seuil de volume pour classer une ligne (aligné sur le dashboard app.py:57) :
# en dessous, les pourcentages (arrêts sautés notamment) ne sont pas exploitables.
MIN_PASSAGES_FOR_RANKING = 50
FLEX_ROUTES_SQL = " AND o.route_id NOT IN (SELECT route_id FROM routes WHERE route_long_name LIKE '%Flex%')"
SIGNIFICANT_GAP_SECONDS = 600
RECENT_ROWS_SQL = " AND o.rowid NOT IN (SELECT x.rowid FROM observations x WHERE x.last_seen_at >= ?)"
MONTH_SQL = (
    " AND ((o.departure_time >= ? AND o.departure_time < ?)"
    " OR (o.departure_time IS NULL AND o.last_seen_at >= ? AND o.last_seen_at < ?))"
)
FRENCH_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


@dataclass
class Scope:
    recipient: str
    routes: list[str]
    communes: list[str]
    description: str


def latex(value: object) -> str:
    """Escape arbitrary database/configuration text for a LaTeX text cell."""
    text = str(value) if value is not None else "—"
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def pct(value: float | None, decimals: int = 1) -> str:
    return "—" if value is None or pd.isna(value) else f"{value:.{decimals}f}\\%"


def number(value: float | int | None, decimals: int = 0) -> str:
    """French thousands separator for LaTeX, without an English comma."""
    if value is None or pd.isna(value):
        return "—"
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", r"\,").replace(".", ",")


def duration(seconds: float | None, signed: bool = True) -> str:
    if seconds is None or pd.isna(seconds):
        return "—"
    seconds = int(round(seconds))
    prefix = "+" if signed and seconds > 0 else "-" if seconds < 0 else ""
    minutes, rest = divmod(abs(seconds), 60)
    return f"{prefix}{minutes} min {rest:02d} s" if minutes else f"{prefix}{rest} s"


def kpi_color(metrics: dict, key: str) -> str:
    """Nom LaTeX du KPI évaluatif, déterminé par les seuils partagés (palette.py).

    Trois paliers : positif=olive, moyen=sunlitclay, négatif=alert.
    """
    return palette_kpi_latex(metrics, key)


def net_val(network_metrics: dict | None, key: str, formatter) -> str:
    """Return a LaTeX snippet showing the network-wide comparison value, or empty."""
    if network_metrics is None:
        return ""
    return f" {{\\tiny\\color{{olive}}Réseau: {formatter(network_metrics[key])}}}"


def safe_slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-") or "rapport"


def resolve_month(conn: sqlite3.Connection, requested_month: str | None) -> str:
    if requested_month:
        try:
            datetime.strptime(requested_month, "%Y-%m")
        except ValueError as error:
            raise ValueError("Le mois doit respecter le format AAAA-MM.") from error
        return requested_month
    row = conn.execute(
        "SELECT MAX(datetime(departure_time, 'unixepoch', 'localtime')) FROM observations WHERE departure_time IS NOT NULL"
    ).fetchone()
    if not row[0]:
        raise ValueError("Impossible de déterminer le mois : aucune heure de départ n'est disponible.")
    return row[0][:7]


def load_scope(args: argparse.Namespace) -> Scope:
    routes = [route.strip() for route in (args.routes or "").split(",") if route.strip()]
    communes = [commune.strip() for commune in (args.communes or "").split(",") if commune.strip()]
    recipient = args.recipient or "Bordeaux Métropole et TBM"
    description = "Réseau TBM - ensemble des lignes observées"
    if not args.profile:
        if communes:
            description = f"Arrêts géolocalisés dans la commune de {', '.join(communes)}"
        return Scope(recipient, routes, communes, description)

    config_path = Path(args.recipients_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Fichier de profils introuvable : {config_path}")
    profiles = json.loads(config_path.read_text(encoding="utf-8"))
    if args.profile not in profiles:
        choices = ", ".join(profiles) or "aucun"
        raise ValueError(f"Profil '{args.profile}' inconnu. Profils disponibles : {choices}")
    profile = profiles[args.profile]
    profile_routes = profile.get("routes", [])
    profile_communes = profile.get("communes", [])
    if not profile_routes and not profile_communes:
        raise ValueError(f"Le profil '{args.profile}' ne contient ni commune ni ligne. Renseignez-le après vérification.")
    resolved_communes = communes or [str(commune) for commune in profile_communes]
    return Scope(
        profile.get("recipient", args.profile),
        routes or [str(route) for route in profile_routes],
        resolved_communes,
        profile.get(
            "description",
            f"Arrêts géolocalisés dans la commune de {', '.join(resolved_communes)}" if resolved_communes
            else "Périmètre défini par les lignes sélectionnées.",
        ),
    )


def month_bounds(month: str) -> tuple[int, int]:
    start = datetime.strptime(month, "%Y-%m")
    end = (start + timedelta(days=32)).replace(day=1)
    return int(start.timestamp()), int(end.timestamp())


def query_observations(conn: sqlite3.Connection, month: str, scope: Scope) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Return scheduled and skipped observations for the calendar month and scope."""
    latest = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    if latest is None:
        raise ValueError("La base ne contient aucune observation.")
    cutoff = int(latest) - FRESHNESS_BUFFER_SECONDS
    lo, hi = month_bounds(month)
    month_latest = conn.execute(
        "SELECT MAX(COALESCE(o.departure_time, o.last_seen_at)) FROM observations o WHERE 1 = 1" + MONTH_SQL,
        (lo, hi, lo, hi),
    ).fetchone()[0]
    route_filter = ""
    params: list[object] = [lo, hi, lo, hi, cutoff]
    if scope.routes:
        placeholders = ", ".join("?" for _ in scope.routes)
        route_filter = f" AND o.route_id IN ({placeholders})"
        params.extend(scope.routes)
    commune_filter = ""
    if scope.communes:
        has_mapping = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stop_municipalities')"
        ).fetchone()[0]
        if not has_mapping:
            raise ValueError(
                "Le rattachement des arrêts aux communes n'a pas été calculé. "
                "Exécutez d'abord src/scripts/assign_stop_municipalities.py."
            )
        placeholders = ", ".join("?" for _ in scope.communes)
        available = {
            row[0].casefold(): row[0]
            for row in conn.execute("SELECT DISTINCT commune_name FROM stop_municipalities")
        }
        unknown = [name for name in scope.communes if name.casefold() not in available]
        if unknown:
            raise ValueError(f"Commune(s) inconnue(s) : {', '.join(unknown)}")
        canonical_communes = [available[name.casefold()] for name in scope.communes]
        automatic_description = f"Arrêts géolocalisés dans la commune de {', '.join(scope.communes)}"
        if scope.description == automatic_description:
            scope.description = f"Arrêts géolocalisés dans la commune de {', '.join(canonical_communes)}"
        scope.communes = canonical_communes
        commune_filter = (
            " AND o.stop_id IN (SELECT stop_id FROM stop_municipalities "
            f"WHERE commune_name IN ({placeholders}))"
        )
        params.extend(canonical_communes)

    base = f"""
        FROM observations o
        LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE 1 = 1 {MONTH_SQL} {RECENT_ROWS_SQL}
          {route_filter}
          {commune_filter}
          {FLEX_ROUTES_SQL}
    """
    scheduled = pd.read_sql_query(
        "SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne, o.departure_delay, o.departure_time "
        + base + " AND +o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL",
        conn, params=params,
    )
    skipped = pd.read_sql_query(
        "SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne, "
        "SUM(CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped, COUNT(*) AS eligible "
        + base + " AND +o.schedule_relationship IN ('SCHEDULED', 'SKIPPED') GROUP BY o.route_id, ligne",
        conn, params=params,
    )
    collected_at = (
        datetime.fromtimestamp(int(month_latest)) if month_latest else datetime.fromtimestamp(cutoff)
    ).strftime("%d/%m/%Y à %H:%M")
    return scheduled, skipped, collected_at


def query_stop_stats(conn: sqlite3.Connection, month: str, scope: Scope) -> pd.DataFrame:
    """Return per-stop delay statistics for the given scope, with the dominant ligne."""
    latest = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    if latest is None:
        raise ValueError("La base ne contient aucune observation.")
    cutoff = int(latest) - FRESHNESS_BUFFER_SECONDS
    lo, hi = month_bounds(month)
    route_filter = ""
    params: list[object] = [lo, hi, cutoff]
    if scope.routes:
        placeholders = ", ".join("?" for _ in scope.routes)
        route_filter = f" AND o.route_id IN ({placeholders})"
        params.extend(scope.routes)
    commune_filter = ""
    if scope.communes:
        placeholders = ", ".join("?" for _ in scope.communes)
        commune_filter = (
            " AND o.stop_id IN (SELECT stop_id FROM stop_municipalities "
            f"WHERE commune_name IN ({placeholders}))"
        )
        params.extend(scope.communes)
    query = f"""
        SELECT o.stop_id, COALESCE(s.stop_name, o.stop_id) AS stop_name,
               o.departure_delay, o.route_id
        FROM observations o
        LEFT JOIN stops s ON o.stop_id = s.stop_id
        WHERE o.departure_time >= ? AND o.departure_time < ? {RECENT_ROWS_SQL}
          {route_filter}
          {commune_filter}
          {FLEX_ROUTES_SQL}
          AND +o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
    """
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return pd.DataFrame()
    stats = df.groupby(["stop_id", "stop_name"], as_index=False).agg(
        retard_moyen=("departure_delay", "mean"),
        retard_median=("departure_delay", "median"),
        passages=("departure_delay", "count"),
        main_route=("route_id", lambda xs: xs.value_counts().index[0]),
    ).sort_values("retard_median", ascending=False)
    kept = stats[stats["passages"] >= MIN_PASSAGES_FOR_RANKING]
    if not kept.empty:
        stats = kept
    route_names = pd.read_sql_query("SELECT route_id, route_short_name FROM routes", conn)
    stats = stats.merge(route_names, left_on="main_route", right_on="route_id", how="left")
    try:
        rows = conn.execute(
            "SELECT route_id, stop_id, terminus FROM stop_direction WHERE terminus IS NOT NULL"
        ).fetchall()
        dir_map = {(r, s): f"vers {t}" for r, s, t in rows}
        stats["direction"] = stats.apply(
            lambda row: dir_map.get((row["main_route"], row["stop_id"]), ""), axis=1
        )
    except sqlite3.OperationalError:
        stats["direction"] = ""
    return stats


def query_monthly_evolution(conn: sqlite3.Connection, month: str, scope: Scope) -> pd.DataFrame:
    """Return monthly punctuality trend for the scope (all months up to the given one)."""
    params: list[object] = [f"{month}-32"]
    table = "agg_daily"
    filters = ""
    if scope.routes:
        filters += f" AND a.route_id IN ({', '.join('?' for _ in scope.routes)})"
        params.extend(scope.routes)
    if scope.communes:
        table = "agg_daily_stop"
        filters += (
            " AND a.stop_id IN (SELECT stop_id FROM stop_municipalities "
            f"WHERE commune_name IN ({', '.join('?' for _ in scope.communes)}))"
        )
        params.extend(scope.communes)
    query = f"""
        SELECT substr(a.date_service, 1, 7) AS mois,
               SUM(a.cnt_le300) * 100.0 / SUM(a.obs) AS ponctualite,
               SUM(a.sum_delay) * 1.0 / SUM(a.obs) AS retard_moyen,
               SUM(a.obs) AS passages
        FROM {table} a
        WHERE a.date_service < ? {filters}
        GROUP BY mois
        HAVING SUM(a.obs) > 0
        ORDER BY mois
    """
    try:
        return pd.read_sql_query(query, conn, params=params)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return pd.DataFrame()


def count_month_observations(conn: sqlite3.Connection, month: str) -> int:
    lo, hi = month_bounds(month)
    return conn.execute(
        "SELECT COUNT(*) FROM observations o "
        "LEFT JOIN routes r ON r.route_id = o.route_id "
        "WHERE o.departure_time >= ? AND o.departure_time < ?",
        (lo, hi),
    ).fetchone()[0] or 0


def query_collection_gaps(conn: sqlite3.Connection, month: str) -> dict:
    """Return gap stats and total observations for the methodology section."""
    result = {"gap_seconds": 0, "gap_count": 0, "short_seconds": 0, "short_count": 0,
              "total_raw": int(count_month_observations(conn, month))}
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='collection_gaps'"
    ).fetchone()
    if not table_exists:
        return result
    lo, hi = month_bounds(month)
    for start, end in conn.execute(
        "SELECT gap_start, gap_end FROM collection_gaps WHERE gap_end > ? AND gap_start < ?", (lo, hi)
    ):
        clipped = min(end, hi) - max(start, lo)
        kind = "gap" if end - start >= SIGNIFICANT_GAP_SECONDS else "short"
        result[f"{kind}_seconds"] += int(clipped)
        result[f"{kind}_count"] += 1
    return result


def gap_methodology_line(gaps: dict | None, passages: int) -> str:
    gaps = gaps or {}
    if gaps.get("gap_count"):
        line = (f"{gaps['gap_count']}~interruption(s) de plus de 10~minutes ce mois-ci, "
                f"{number(gaps['gap_seconds'] / 60)}~min au total : les passages de ces intervalles peuvent manquer.")
    else:
        line = "Aucune interruption de collecte de plus de 10~minutes ce mois-ci."
    if gaps.get("short_count"):
        line += (f" {gaps['short_count']}~interruption(s) plus courte(s) ({number(gaps['short_seconds'] / 60)}~min "
                 "au total) ont été rattrapées par le relevé suivant, le flux conservant les passages "
                 "quelques minutes après leur départ.")
    if gaps.get("total_raw"):
        line += (f" Passages analysés : {number(passages)} sur {number(gaps['total_raw'])} observations "
                 "brutes ; les autres sont des arrêts non desservis, des passages sans retard publié ou "
                 "des lignes à la demande.")
    return line


def query_service_alerts(conn: sqlite3.Connection, month: str, route_ids: set[str]) -> list[dict]:
    """Return service alerts active during the calendar month for the given route_ids."""
    if not route_ids:
        return []
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='service_alerts'"
    ).fetchone()
    if not table_exists:
        return []
    year, mon = map(int, month.split("-"))
    start_of_month = int(datetime(year, mon, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(year, mon)[1]
    end_of_month = int(datetime(year, mon, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    placeholders = ", ".join("?" for _ in route_ids)
    rows = conn.execute(f"""
        SELECT alert_id, route_id, active_period_start, active_period_end,
               header_text, description_text
        FROM service_alerts
        WHERE active_period_start <= ?
          AND (active_period_end IS NULL OR active_period_end >= ?)
          AND (route_id = '' OR route_id IN ({placeholders}))
        ORDER BY active_period_start DESC
    """, [end_of_month, start_of_month] + list(route_ids)).fetchall()
    return [
        {
            "alert_id": r[0],
            "route_id": r[1],
            "active_period_start": r[2],
            "active_period_end": r[3],
            "header_text": r[4],
            "description_text": r[5],
        }
        for r in rows
    ]


V2_DAY_TYPES = (("Jours de semaine", "semaine"), ("Samedis", "samedi"), ("Dimanches", "dimanche"))
V2_TABLE_ROWS = 8


def next_month(month: str) -> str:
    return (datetime.strptime(month, "%Y-%m") + timedelta(days=32)).strftime("%Y-%m")


def month_label(month: str) -> str:
    date = datetime.strptime(month, "%Y-%m")
    return f"{FRENCH_MONTHS[date.month - 1]} {date.year}"


def query_method_v2(conn: sqlite3.Connection, month: str, scope: Scope) -> dict | None:
    since, end = f"{month}-01", f"{next_month(month)}-01"
    previous_since = f"{previous_month(month)}-01"
    routes = scope.routes or None
    communes = scope.communes or None
    try:
        current = ind.indicators(conn, since, end, routes=routes, communes=communes)
        if not current.disponible:
            return None
        by_day_type = []
        for label, kind in V2_DAY_TYPES:
            now = ind.indicators(conn, since, end, routes=routes, communes=communes, day_types=[kind])
            before = ind.indicators(conn, previous_since, since, routes=routes, communes=communes, day_types=[kind])
            by_day_type.append({"label": label, "current": now, "previous": before,
                                "change": ind.compare(now, before)})
        lines = ind.route_indicators(conn, since, end, communes=communes, min_passages=MIN_PASSAGES_FOR_RANKING)
        if routes:
            lines = lines[lines["route_id"].isin(routes)]
        regularity = ind.route_regularity(conn, since, end, communes=communes, routes=routes)
        flags = ind.quality_flags(conn, since, end)
    except sqlite3.Error:
        return None
    names = dict(conn.execute("SELECT route_id, COALESCE(route_short_name, route_id) FROM routes"))
    return {
        "month": month,
        "current": current,
        "by_day_type": by_day_type,
        "lines": lines,
        "regularity": regularity,
        "degraded_days": sorted(d for d, flag in flags.items() if flag == "degrade"),
        "names": names,
    }


def _short_day(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%d/%m")


def _with_margin(value: float | None, margin: float | None, decimals: int = 1) -> str:
    if value is None:
        return "—"
    if margin is None:
        return number(value, decimals)
    return f"{number(value, decimals)} $\\pm$ {number(margin, decimals)}"


def _pct_fr(value: float | None) -> str:
    return "—" if value is None or pd.isna(value) else f"{number(value, 1)}\\,\\%"


def _change_reading(change: dict | None) -> str:
    if change is None:
        return "comparaison impossible"
    if change["significatif"] is None:
        return "marge non calculable"
    if not change["significatif"]:
        return "dans la marge d'incertitude"
    return "hausse significative" if change["ecart"] > 0 else "baisse significative"


def method_v2_summary(v2: dict | None) -> str:
    if not v2:
        return ""
    current = v2["current"]
    return (
        "\n\\vspace{.2cm}\n"
        rf"\textbf{{Score {ind.METHOD_VERSION} (en test).}} {_with_margin(current.score, current.marge)} sur 100. "
        "Cette mesure plus exigeante tient compte des départs en avance et des courses supprimées. "
        rf"Elle est présentée page \pageref{{v2page}} et ne remplace pas encore le score ci-dessus."
    )


def method_v2_section(v2: dict | None) -> str:
    if not v2:
        return ""
    current = v2["current"]
    names = v2["names"]
    previous_label = month_label(previous_month(v2["month"]))
    score_color = kpi_color({"fiability": current.score}, "fiability")
    punctuality_color = kpi_color({"ponctualite": current.ponctualite}, "ponctualite")
    rows = []
    for item in v2["by_day_type"]:
        now, before, change = item["current"], item["previous"], item["change"]
        rows.append(
            f"{item['label']} & {_with_margin(now.score, now.marge)} ({now.jours} j) & "
            f"{_with_margin(before.score, before.marge)} ({before.jours} j) & "
            f"{'—' if change is None else _with_margin(change['ecart'], change['marge'])} & "
            f"{_change_reading(change)} \\\\"
        )
    day_type_rows = "\n".join(rows)

    regular = {r: reg for r, reg in v2["regularity"].items() if reg["jours"] * 2 >= current.jours}
    regularity = sorted(regular.items(), key=lambda kv: kv[1]["attente_excedentaire"], reverse=True)
    regularity_rows = "\n".join(
        f"{latex(names.get(route_id, route_id))} & {duration(reg['attente_prevue'], signed=False)} & "
        f"{duration(reg['attente_reelle'], signed=False)} & {duration(reg['attente_excedentaire'])} \\\\"
        for route_id, reg in regularity[:V2_TABLE_ROWS]
    )
    regularity_block = (
        r"\begin{tabularx}{\textwidth}{@{}Xrrr@{}}" "\n"
        r"\toprule" "\n"
        r"\textbf{Ligne} & \textbf{Attente prévue} & \textbf{Attente réelle} & \textbf{Attente excédentaire} \\" "\n"
        r"\midrule" "\n"
        f"{regularity_rows}\n"
        r"\bottomrule" "\n"
        r"\end{tabularx}"
    ) if regularity else r"\textit{Aucune ligne ne compte au moins 5 passages prévus par heure sur ce périmètre.}"


    lines = v2["lines"]
    worst = lines[~lines["temps_reel_douteux"]].sort_values("score_v2").head(V2_TABLE_ROWS)
    line_rows = "\n".join(
        f"{latex(names.get(row.route_id, row.route_id))} & {_with_margin(row.score_v2, row.marge_v2)} & "
        f"{_pct_fr(row.ponctualite_stricte)} & {_pct_fr(row.part_avance)} & {_pct_fr(row.service_assure)} & "
        f"{number(int(row.passages))} \\\\"
        for row in worst.itertuples()
    )

    excluded = ", ".join(_short_day(d) for d in current.jours_exclus) or "aucun"
    degraded = ", ".join(_short_day(d) for d in v2["degraded_days"]) or "aucun"
    discarded = ", ".join(
        f"{latex(names.get(route_id, route_id))} ({number(share, 0)}\\,\\% de retards nuls)"
        for route_id, share in sorted(current.lignes_ecartees.items())
    ) or "aucune"

    return rf"""\newpage
\section*{{Méthode {ind.METHOD_VERSION} — indicateurs en test}}
\label{{v2page}}
Ces indicateurs sont calculés en parallèle du score de fiabilité, sans le remplacer. Ils corrigent trois limites de la méthode actuelle : un départ en avance n'est plus compté « à l'heure », les courses supprimées entrent dans le calcul, et chaque résultat est accompagné de sa marge d'incertitude. Le choix de la méthode de référence sera fait après plusieurs mois de double affichage.\\[.4cm]
\makebox[\textwidth]{{\kpi[{score_color}]{{Score 2.0}}{{{_with_margin(current.score, current.marge)}}}\hfill
\kpi[{punctuality_color}]{{Ponctualité stricte ($-1$ à $+5$ min)}}{{{_with_margin(current.ponctualite, current.marge_ponctualite)}\,\%}}\hfill
\kpi{{Service assuré}}{{{_pct_fr(current.service)}}}}}

\vspace{{.3cm}}
\textbf{{Lecture.}} Sur 100 passages attendus, {number(current.service, 1)} ont été assurés (non supprimés, arrêt desservi) ; {_pct_fr(current.ponctualite)} des passages observés sont partis dans la fenêtre d'une minute d'avance à cinq minutes de retard et {_pct_fr(current.avance)} avec plus d'une minute d'avance. Le score 2.0 combine les deux : il compte les passages assurés et à l'heure parmi tous les passages attendus.

\subsection*{{Comparaison avec {latex(previous_label)}, à type de jour égal}}
\begin{{tabularx}}{{\textwidth}}{{@{{}}lrrrX@{{}}}}
\toprule
\textbf{{Type de jour}} & \textbf{{{latex(month_label(v2['month']))}}} & \textbf{{{latex(previous_label)}}} & \textbf{{Écart}} & \textbf{{Lecture}} \\
\midrule
{day_type_rows}
\bottomrule
\end{{tabularx}}

\vspace{{.15cm}}
{{\small Un écart est significatif lorsqu'il dépasse la marge combinée des deux mois. Comparer un mois à l'autre par type de jour évite de confondre une dégradation avec un calendrier différent (nombre de week-ends, jours fériés).}}

\subsection*{{Régularité des lignes fréquentes}}
Sur une ligne fréquente, l'usager n'attend pas un horaire précis mais le prochain passage : ce qui compte est la régularité des intervalles. L'attente excédentaire est le temps d'attente moyen ajouté par des passages irréguliers par rapport à la grille prévue. Seules figurent les lignes fréquentes au moins un jour sur deux.\\[.2cm]
{regularity_block}

\subsection*{{Lignes les moins bien placées selon le score 2.0}}
\begin{{tabularx}}{{\textwidth}}{{@{{}}Xrrrrr@{{}}}}
\toprule
\textbf{{Ligne}} & \textbf{{Score 2.0}} & \textbf{{Ponctualité stricte}} & \textbf{{En avance}} & \textbf{{Service assuré}} & \textbf{{Passages}} \\
\midrule
{line_rows}
\bottomrule
\end{{tabularx}}

\subsection*{{Qualité des données}}
\begin{{itemize}}[leftmargin=1.4em,itemsep=.2em]
\item \textbf{{Jours exclus}} (collecte incomplète) : {excluded}.
\item \textbf{{Jours à collecte dégradée}} (conservés) : {degraded}.
\item \textbf{{Lignes écartées}} (temps réel douteux) : {discarded}.
\end{{itemize}}

\subsection*{{Définitions}}
{{\small
\begin{{itemize}}[leftmargin=1.4em,itemsep=.15em]
\item \textbf{{Fenêtre « à l'heure »}} : départ entre {ind.EARLY_TOLERANCE_SECONDS}~s d'avance et {ind.LATE_TOLERANCE_SECONDS // 60}~min de retard.
\item \textbf{{Passages attendus}} : passages desservis ou sautés, plus les passages des courses supprimées. Ces derniers sont estimés au prorata du nombre moyen de passages par course de la ligne le même jour. Une course supprimée puis remplacée par une course ajoutée le même jour sur la même ligne n'est pas comptée comme perdue.
\item \textbf{{Score 2.0}} = passages assurés et à l'heure / passages attendus. \textbf{{Service assuré}} = passages assurés / passages attendus. \textbf{{Ponctualité stricte}} = passages dans la fenêtre / passages observés.
\item \textbf{{Marge ($\pm$)}} : intervalle de confiance à 95\,\% (loi de Student), les jours étant traités comme unités d'échantillonnage. La marge reflète la variabilité d'un jour à l'autre et non la précision de la mesure d'un passage ; elle s'élargit quand peu de jours sont disponibles.
\item \textbf{{Jour incomplet}} : au moins {QUALITY_INCOMPLETE_HOURS} heures entre 5~h et 23~h avec moins de la moitié du volume habituel (médiane des trois mêmes jours de la semaine précédents). Un jour incomplet est exclu ; un jour avec une ou deux heures lacunaires est conservé et signalé.
\item \textbf{{Temps réel douteux}} : ligne dont au moins {int(ind.DOUBTFUL_ZERO_SHARE * 100)}\,\% des retards valent exactement zéro (horaire théorique republié à la place du temps réel).
\item \textbf{{Attente moyenne}} = $\sum h^2 / (2 \sum h)$, où $h$ est l'intervalle entre deux passages successifs à un arrêt. Calculée sur les arrêts et heures comptant au moins 5 passages prévus.
\item Les passages attendus et assurés sont rattachés au jour de la course, la ponctualité au jour du départ effectif ; les deux ne diffèrent que pour les départs après minuit.
\end{{itemize}}
}}
"""


def make_line_stats(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    rows = scheduled.groupby(["route_id", "ligne"], as_index=False).agg(
        passages=("departure_delay", "size"),
        retard_moyen=("departure_delay", "mean"),
        retard_median=("departure_delay", "median"),
        ponctualite=("departure_delay", lambda series: (series <= 300).mean() * 100),
        retard_5=("departure_delay", lambda series: (series > 300).mean() * 100),
    )
    rows = rows.merge(skipped, on=["route_id", "ligne"], how="left").fillna({"skipped": 0, "eligible": 0})
    rows["arrets_sautes"] = rows["skipped"] / rows["eligible"].replace(0, 1) * 100
    rows["score"] = (rows["ponctualite"] - 2 * rows["arrets_sautes"]).clip(0, 100)
    return rows.sort_values(["score", "passages"], ascending=[True, False])


def ranking_lines(lines: pd.DataFrame) -> pd.DataFrame:
    """Restreint le classement aux lignes à volume suffisant sur la période.

    Une ligne apparue quelques jours seulement produit des pourcentages
    d'arrêts sautés hors d'échelle (ex. 1 saut sur 6 = 16,7 %). On conserve
    les lignes avec au moins MIN_PASSAGES_FOR_RANKING passages ; si aucune
    ligne n'atteint le seuil, on restitue l'ensemble (périmètres de très
    faible volume).
    """
    if lines.empty:
        return lines
    filtered = lines[lines["passages"] >= MIN_PASSAGES_FOR_RANKING].copy()
    return filtered if not filtered.empty else lines


def kpis(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> dict[str, float | int]:
    eligible = int(skipped["eligible"].sum()) if not skipped.empty else 0
    skipped_count = int(skipped["skipped"].sum()) if not skipped.empty else 0
    return {
        "passages": len(scheduled),
        "ponctualite": (scheduled.departure_delay <= 300).mean() * 100,
        "retard": scheduled.departure_delay.mean(),
        "retard_median": scheduled.departure_delay.median(),
        "retard_5": (scheduled.departure_delay > 300).mean() * 100,
        "skipped": skipped_count,
        "skip_rate": skipped_count / eligible * 100 if eligible else 0,
        "fiability": max(0.0, (scheduled.departure_delay <= 300).mean() * 100 - 2 * (skipped_count / eligible * 100 if eligible else 0)),
    }


def comparison(current: dict[str, float | int], previous: dict[str, float | int] | None) -> dict[str, str]:
    if not previous:
        return {"fiability": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain.",
                "ponctualite": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain.",
                "retard": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain.",
                "skip_rate": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain."}
    return {
        "fiability": f"{float(current['fiability']) - float(previous['fiability']):+.1f} / 100",
        "ponctualite": f"{float(current['ponctualite']) - float(previous['ponctualite']):+.1f} %",
        "retard": (
            f"moy. {float(current['retard']) - float(previous['retard']):+.0f} s ; "
            f"méd. {float(current['retard_median']) - float(previous['retard_median']):+.0f} s"
        ),
        "skip_rate": f"{float(current['skip_rate']) - float(previous['skip_rate']):+.2f} %",
    }


def previous_month(month: str) -> str:
    year, month_number = map(int, month.split("-"))
    return f"{year - 1:04d}-12" if month_number == 1 else f"{year:04d}-{month_number - 1:02d}"


def executive_message(metrics: dict[str, float | int], lines: pd.DataFrame, scope: Scope | None = None) -> str:
    p = metrics["ponctualite"]
    skip = metrics["skip_rate"]
    worst = lines.iloc[0]
    prefix = f"Le périmètre {latex(scope.description)} " if scope and scope.communes else "Le réseau TBM "
    if p >= 95:
        assessment = f"{prefix}affiche une ponctualité excellente ({p:.1f}\\%)."
    elif p >= 90:
        assessment = f"{prefix}enregistre un bon niveau de ponctualité ({p:.1f}\\%)."
    elif p >= 85:
        assessment = f"{prefix}présente une fiabilité correcte ({p:.1f}\\%), encore perfectible."
    elif p >= 80:
        assessment = f"{prefix}montre une fiabilité intermédiaire ({p:.1f}\\%)."
    elif p >= 75:
        assessment = f"{prefix}connaît des difficultés de ponctualité notables ({p:.1f}\\%)."
    elif p >= 65:
        assessment = f"{prefix}enregistre une ponctualité insuffisante ({p:.1f}\\%)."
    else:
        assessment = f"{prefix}subit des retards critiques ({p:.1f}\\% de passages à l'heure)."
    if skip > 5:
        assessment += f" Le taux d'arrêts sautés ({skip:.2f}\\% des passages) aggrave la situation."
    return (
        f"{assessment} La principale alerte concerne la ligne {latex(worst.ligne)}, avec un score de fiabilité "
        f"de {worst.score:.1f}/100 et {worst.retard_5:.1f}\\% de passages au-delà de cinq minutes de retard."
    )


def line_table(lines: pd.DataFrame, network_lines: pd.DataFrame | None = None,
               alert_routes: set[str] | None = None) -> str:
    net_lookup: dict[str, tuple] = {}
    if network_lines is not None and not network_lines.empty:
        net_lookup = {row.route_id: row for row in network_lines.itertuples()}
    alert_routes = alert_routes or set()
    table_rows = []
    for row in lines.itertuples():
        net_row = net_lookup.get(row.route_id)
        prefix = r"\alertmark{} " if row.route_id in alert_routes else ""
        ponctualite_cell = pct(row.ponctualite)
        retard_5_cell = pct(row.retard_5)
        arrets_cell = pct(row.arrets_sautes, 2)
        passages_cell = number(int(row.passages))
        if net_row is not None:
            ponctualite_cell += f" {{\\tiny\\color{{olive}}({pct(net_row.ponctualite)})}}"
            retard_5_cell += f" {{\\tiny\\color{{olive}}({pct(net_row.retard_5)})}}"
            arrets_cell += f" {{\\tiny\\color{{olive}}({pct(net_row.arrets_sautes, 2)})}}"
            passages_cell += f" {{\\tiny\\color{{olive}}({number(int(net_row.passages))})}}"
        table_rows.append(
            f"{prefix}{latex(row.ligne)} & {passages_cell} & {ponctualite_cell} & "
            f"{duration(row.retard_moyen)} / {duration(row.retard_median)} & {retard_5_cell} & {arrets_cell} \\\\"
        )
    return "\n".join(table_rows)


def operational_views(scheduled: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the hourly-risk and delay-distribution views shown in the dashboard."""
    dated = scheduled.dropna(subset=["departure_time"]).copy()
    if dated.empty:
        return pd.DataFrame(), pd.DataFrame()
    dated["heure"] = pd.to_datetime(dated["departure_time"], unit="s", utc=True).dt.tz_convert("Europe/Paris").dt.hour
    hourly = dated.groupby("heure", as_index=False).agg(
        passages=("departure_delay", "size"),
        retard_moyen=("departure_delay", "mean"),
        retard_median=("departure_delay", "median"),
        retard_5=("departure_delay", lambda values: (values > 300).mean() * 100),
    )
    all_hours = pd.DataFrame({"heure": range(24)})
    hourly = all_hours.merge(hourly, on="heure", how="left").fillna(
        {"passages": 0, "retard_5": 0.0, "retard_moyen": 0.0, "retard_median": 0.0}
    )
    bins = [-3600, -600, -300, -120, -60, 0, 60, 120, 300, 600, 1200, 3601]
    labels = ["< -10", "-10 a -5", "-5 a -2", "-2 a -1", "-1 a 0", "0 a +1", "+1 a +2", "+2 a +5", "+5 a +10", "+10 a +20", "> +20"]
    classes = pd.cut(dated["departure_delay"].clip(-3600, 3600), bins=bins, labels=labels, right=False)
    distribution = classes.value_counts(sort=False).rename_axis("plage").reset_index(name="passages")
    return hourly, distribution


def _setup_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(SUNLIT_CLAY_TINT)
    ax.tick_params(color=BLACK_FOREST, labelcolor=BLACK_FOREST)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color(BLACK_FOREST + "30")
    ax.spines["left"].set_visible(True)
    ax.spines["left"].set_color(BLACK_FOREST + "30")
    ax.grid(axis="y", color=BLACK_FOREST, alpha=0.15, linewidth=0.5)
    ax.grid(axis="x", color=BLACK_FOREST, alpha=0.15, linewidth=0.5)


def _save_chart(fig: plt.Figure, output_dir: Path, name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.png"
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)
    return path


def reliability_chart(lines: pd.DataFrame, output_dir: Path, name: str,
                      network_lines: pd.DataFrame | None = None) -> Path | None:
    selected = lines.head(15).reset_index(drop=True)
    if selected.empty:
        return None
    fig, ax = plt.subplots(figsize=(7.5, max(2.5, len(selected) * 0.35)))
    _setup_ax(ax)
    colors = [palette_hex(float(r.score), "score") for _, r in selected.iterrows()]
    y = range(len(selected))
    ax.barh(y, selected["score"], color=colors, height=0.55, zorder=3, edgecolor="white", linewidth=0.3, label="Score")
    if network_lines is not None and not network_lines.empty:
        net_scores = []
        for i, row in selected.iterrows():
            nr = network_lines[network_lines["route_id"] == row.route_id]
            net_scores.append(float(nr.iloc[0]["score"]) if not nr.empty else 0)
        ax.barh(y, net_scores, color=BLACK_FOREST, height=0.18, alpha=0.35, zorder=4, label="Réseau")
        ax.legend(fontsize=7, loc="lower right")
    for i, row in selected.iterrows():
        ax.text(float(row.score) + 0.8, i, f"{row.score:.0f}", va="center", fontsize=7, color=BLACK_FOREST)
    ax.set_yticks(list(y))
    ax.set_yticklabels(selected["ligne"].tolist(), fontsize=7)
    ax.set_xlim(0, 105)
    ax.set_xlabel("Score de fiabilité / 100", color=BLACK_FOREST, fontsize=8)
    ax.set_ylabel("Ligne", color=BLACK_FOREST, fontsize=8)
    ax.xaxis.set_major_locator(mticker.MultipleLocator(20))
    ax.set_title("Priorités de fiabilité par ligne", color=BLACK_FOREST, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def risk_scatter_chart(lines: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if lines.empty:
        return None
    fig, ax = plt.subplots(figsize=(6.5, 5))
    _setup_ax(ax)
    for _, row in lines.iterrows():
        c = palette_hex(float(row.score), "score")
        ax.scatter(float(row.retard_median), float(row.retard_5), c=c, s=30, zorder=3, edgecolors="white", linewidth=0.3)
    for _, row in lines.iterrows():
        ax.text(float(row.retard_median) + max(float(lines.retard_median.max()) * 0.025, 3),
                float(row.retard_5), row.ligne, fontsize=6, color=BLACK_FOREST, va="center")
    xmax = float(lines.retard_median.max()) * 1.3 or 120
    ymax = float(lines.retard_5.max()) * 1.3 or 30
    ax.set_xlim(0, xmax)
    ax.set_ylim(0, ymax)
    ax.set_xlabel("Retard médian (secondes)", color=BLACK_FOREST, fontsize=8)
    ax.set_ylabel("Passages > 5 min (%)", color=BLACK_FOREST, fontsize=8)
    ax.set_title("Carte de risque des lignes", color=BLACK_FOREST, fontsize=10, fontweight="bold")
    ax.text(0.05, 0.92, "Retards rares mais longs", transform=ax.transAxes, fontsize=7, color=BLACK_FOREST + "80", va="top")
    ax.text(0.70, 0.92, "Zone critique", transform=ax.transAxes, fontsize=7, color=BLACK_FOREST + "80", va="top")
    ax.text(0.05, 0.05, "Risque faible", transform=ax.transAxes, fontsize=7, color=BLACK_FOREST + "80", va="bottom")
    ax.text(0.70, 0.05, "Retards fréquents mais courts", transform=ax.transAxes, fontsize=7, color=BLACK_FOREST + "80", va="bottom")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def stop_chart(stop_stats: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if stop_stats.empty:
        return None
    selected = stop_stats.head(12).iloc[::-1].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(7, max(2.5, len(selected) * 0.4)))
    _setup_ax(ax)
    colors = [palette_hex(float(r.retard_median), "retard") for _, r in selected.iterrows()]
    y = range(len(selected))
    ax.barh([i - 0.15 for i in y], selected["retard_moyen"], height=0.25, color=colors, zorder=3, label="Moyen", edgecolor="white", linewidth=0.3, alpha=0.5)
    ax.barh([i + 0.15 for i in y], selected["retard_median"], height=0.25, color=colors, zorder=3, label="Médian", edgecolor="white", linewidth=0.3)
    for i, row in selected.iterrows():
        ax.text(float(row.retard_moyen) + 1.5, i - 0.15, duration(float(row.retard_moyen)), va="center", fontsize=6, color=BLACK_FOREST)
        ax.text(float(row.retard_median) + 1.5, i + 0.15, duration(float(row.retard_median)), va="center", fontsize=6, color=BLACK_FOREST)
    labels = []
    for _, row in selected.iterrows():
        ligne = row.get("route_short_name") or row.get("main_route", "")
        suffix = f" ({ligne})" if ligne else ""
        direction = row.get("direction")
        if isinstance(direction, str) and direction:
            labels.append(f"{row.stop_name} — {direction}{suffix}")
        else:
            labels.append(f"{row.stop_name}{suffix}")
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Retard (secondes)", color=BLACK_FOREST, fontsize=8)
    ax.set_title("Arrêts les plus problématiques du périmètre", color=BLACK_FOREST, fontsize=10, fontweight="bold")
    ax.legend(loc="best", fontsize=7, frameon=True, framealpha=0.9, ncol=2, title="Retard")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def evolution_chart(monthly: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if monthly.empty or len(monthly) < 2:
        return None
    fig, ax = plt.subplots(figsize=(7, 3.5))
    _setup_ax(ax)
    colors = [palette_hex(float(r.ponctualite), "score") for _, r in monthly.iterrows()]
    for i in range(len(monthly) - 1):
        ax.plot([i, i + 1], [monthly.iloc[i]["ponctualite"], monthly.iloc[i + 1]["ponctualite"]],
                color=BLACK_FOREST, linewidth=1.5, zorder=2)
    ax.scatter(range(len(monthly)), monthly["ponctualite"], c=colors, s=40, zorder=3, edgecolors="white", linewidth=0.5)
    ax.set_ylim(50, 100)
    ax.set_xticks(range(len(monthly)))
    ax.set_xticklabels(monthly["mois"].tolist(), fontsize=7, rotation=30, ha="right")
    ax.set_xlabel("Mois", color=BLACK_FOREST, fontsize=8)
    ax.set_ylabel("Ponctualité (%)", color=BLACK_FOREST, fontsize=8)
    ax.set_title("Évolution mensuelle de la ponctualité", color=BLACK_FOREST, fontsize=10, fontweight="bold")
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def hourly_chart(hourly: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if hourly.empty:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    _setup_ax(ax)
    colors = [palette_hex(float(r.retard_5), "pourcent") for _, r in hourly.iterrows()]
    ax.bar(hourly["heure"], hourly["retard_5"], color=colors, width=0.7, zorder=3, edgecolor="white", linewidth=0.3)
    for _, row in hourly.iterrows():
        ax.text(int(row.heure), float(row.retard_5) + 0.5, f"{row.retard_5:.1f}",
                ha="center", fontsize=6, color=BLACK_FOREST)
    ax.axvspan(7.5, 9.5, color=SUNLIT_CLAY_TINT, alpha=0.4, zorder=1)
    ax.axvspan(17.5, 19.5, color=SUNLIT_CLAY_TINT, alpha=0.4, zorder=1)
    net_avg = float(hourly["retard_5"].mean())
    ax.axhline(net_avg, color=BLACK_FOREST, linewidth=0.8, linestyle="--", zorder=2)
    ax.text(23, net_avg, f"Moyenne réseau : {net_avg:.1f}%", fontsize=6, color=BLACK_FOREST, va="bottom", ha="right")
    ax.set_xlim(-0.5, 23.5)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("Heure", color=BLACK_FOREST, fontsize=8)
    ax.set_ylabel("Retards > 5 min (%)", color=BLACK_FOREST, fontsize=8)
    ax.set_title("Risque selon l'heure de départ", color=BLACK_FOREST, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def distribution_chart(distribution: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if distribution.empty:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    _setup_ax(ax)
    plages = distribution["plage"].tolist()
    dist_color_map = {
        "< -10": COPPERWOOD, "-10 a -5": COPPERWOOD, "-5 a -2": COPPERWOOD,
        "-2 a -1": SUNLIT_CLAY,
        "-1 a 0": OLIVE_LEAF, "0 a +1": OLIVE_LEAF, "+1 a +2": OLIVE_LEAF,
        "+2 a +5": SUNLIT_CLAY,
        "+5 a +10": COPPERWOOD, "+10 a +20": COPPERWOOD, "> +20": COPPERWOOD,
    }
    colors = [dist_color_map.get(p, COPPERWOOD) for p in plages]
    n = len(distribution)
    ax.bar(range(n), distribution["passages"], color=colors, width=0.7, zorder=3, edgecolor="white", linewidth=0.3)
    for i, (_, row) in enumerate(distribution.iterrows()):
        ax.text(i, int(row.passages) + max(1, int(distribution.passages.max()) * 0.02),
                str(int(row.passages)), ha="center", fontsize=7, color=BLACK_FOREST)
    ax.set_xticks(range(n))
    ax.set_xticklabels(plages, fontsize=7, rotation=45, ha="right")
    ax.set_xlabel("Tranche de retard (minutes)", color=BLACK_FOREST, fontsize=8)
    ax.set_ylabel("Nombre de passages", color=BLACK_FOREST, fontsize=8)
    ax.set_title("Distribution des retards", color=BLACK_FOREST, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def graphical_annex(lines: pd.DataFrame, scheduled: pd.DataFrame,
                     network_lines: pd.DataFrame | None = None,
                     stop_stats: pd.DataFrame | None = None,
                     monthly_evolution: pd.DataFrame | None = None,
                     output_dir: Path | None = None) -> str:
    if output_dir is None:
        output_dir = Path("/tmp/urban_vision_charts")
    hourly, distribution = operational_views(scheduled)
    imgs: list[str] = []
    imgs.append(r"\newpage\section*{Annexe — Analyse graphique}")
    imgs.append(r"\small\textbf{Seuils de couleur utilisés dans les graphiques :} "
                r"positif = bonne performance ($\geq$ 80/100 pour le score de fiabilité, "
                r"$\leq$ 60 s pour le retard moyen/médian, $\leq$ 5\% pour les retards $>$ 5 min), "
                r"moyen = performance intermédiaire, "
                r"négatif = performance dégradée. Consulter la section Méthode pour le détail des calculs.\\[.3cm]")
    p = reliability_chart(lines, output_dir, "reliability", network_lines)
    if p:
        imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    p = risk_scatter_chart(lines, output_dir, "risk_scatter")
    if p:
        imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    if stop_stats is not None and not stop_stats.empty:
        p = stop_chart(stop_stats, output_dir, "stops")
        if p:
            imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    if monthly_evolution is not None and len(monthly_evolution) >= 2:
        p = evolution_chart(monthly_evolution, output_dir, "evolution")
        if p:
            imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    imgs.append(r"\newpage\section*{Annexe — Profil opérationnel}")
    p = hourly_chart(hourly, output_dir, "hourly") if not hourly.empty else None
    imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}" if p
                else r"\textit{Aucune heure de départ exploitable pour cette période.}")
    p = distribution_chart(distribution, output_dir, "distribution")
    if p:
        imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    return "\n".join(imgs)


def build_no_data_latex(month: str, scope: Scope, collected_at: str) -> str:
    """Produce a transparent report even when a small municipality has no passage."""
    report_date = datetime.strptime(month, "%Y-%m")
    report_month = f"{FRENCH_MONTHS[report_date.month - 1]} {report_date.year}"
    cover_logo = str(Path(__file__).resolve().parents[1] / "assets" / "logo" / "urban-vision-logo-white.png")
    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[french]{{babel}}
\usepackage[margin=2cm]{{geometry}}
\usepackage{{xcolor,fancyhdr,graphicx,tcolorbox}}
\definecolor{{olive}}{{HTML}}{{606C38}}
\definecolor{{blackforest}}{{HTML}}{{283618}}
\definecolor{{teal}}{{HTML}}{{2A6F6F}}
\definecolor{{cornsilk}}{{HTML}}{{FEFAE0}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{blackforest}}{{URBAN VISION}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
\begin{{document}}
\begin{{center}}
\begin{{tcolorbox}}[width=\textwidth,colback=blackforest,colframe=blackforest,arc=4pt,boxrule=0pt,left=14pt,right=14pt,top=12pt,bottom=12pt,halign=center]
\includegraphics[height=1.05cm]{{{cover_logo}}}\\[7pt]
{{\color{{cornsilk}}\LARGE\bfseries Rapport mensuel de fiabilité des transports}}\\[4pt]
{{\color{{cornsilk!75}}\large Urban Vision}}\\[4pt]
{{\color{{white}}\small {latex(report_month).capitalize()}}}\\[2pt]
{{\color{{white!85}}\footnotesize Périmètre : {latex(scope.description)}}}\\[2pt]
{{\color{{white!70}}\scriptsize Rapport produit par Elias Khallouk --- eliaskhallouk@gmail.com}}
\end{{tcolorbox}}
\end{{center}}
\vspace{{1cm}}\hrule\vspace{{1cm}}
\section*{{Absence de données exploitables}}
Aucun passage programmé avec une heure de départ et un retard stabilisé n'a été observé dans ce périmètre durant le mois analysé. Il n'est donc pas possible de calculer des indicateurs ni de produire des graphiques fiables pour cette édition.

\vspace{{.4cm}}
Cette absence ne signifie pas nécessairement l'absence de desserte : elle peut résulter d'une couverture de collecte insuffisante, d'une période sans circulation, ou d'arrêts présents dans le GTFS mais non observés dans le flux temps réel.

\vfill
\small\color{{olive}} Source : flux GTFS-RT TripUpdates TBM, données arrêtées au {latex(collected_at)}. Le périmètre repose sur les arrêts géolocalisés dans la commune.
\end{{document}}
"""


def build_latex(month: str, scope: Scope, metrics: dict[str, float | int], change: dict[str, str],
                lines: pd.DataFrame, scheduled: pd.DataFrame, collected_at: str,
                output_dir: Path,
                network_metrics: dict | None = None,
                network_lines: pd.DataFrame | None = None,
                stop_stats: pd.DataFrame | None = None,
                monthly_evolution: pd.DataFrame | None = None,
                gaps: dict | None = None,
                alerts: list[dict] | None = None,
                v2: dict | None = None) -> str:
    report_date = datetime.strptime(month, "%Y-%m")
    report_month = f"{FRENCH_MONTHS[report_date.month - 1]} {report_date.year}"
    worst = lines.head(3)

    # Compute network-wide ranking for alert lines
    net_rank: dict[str, int] = {}
    net_total = 0
    if network_lines is not None and not network_lines.empty:
        ranked = network_lines.sort_values("score", ascending=True).reset_index(drop=True)
        net_total = len(ranked)
        net_rank = {row.route_id: idx + 1 for idx, row in ranked.iterrows()}

    # Collection gaps info for methodology
    gap_line = gap_methodology_line(gaps, int(metrics["passages"]))
    evolution_note = rf"\textbf{{Évolution mensuelle.}} {change.get('fiability', '')}"


    alert_routes = {a["route_id"] for a in (alerts or []) if a["route_id"]}

    priority_items = []
    for row in worst.itertuples():
        marker = r"\alertmark{} " if row.route_id in alert_routes else ""
        net = f" (rang réseau : {net_rank.get(row.route_id, '—')}/{net_total})" if net_rank else ""
        priority_items.append(
            rf"\item {marker}\textbf{{Ligne {latex(row.ligne)}}}{net}"
            rf" : score {row.score:.1f}/100, {pct(row.retard_5)} de retards supérieurs à 5 minutes, {pct(row.arrets_sautes, 2)} d'arrêts sautés."
        )
    priority_alerts = "\n".join(priority_items)

    # Format service alerts for the dedicated "Infos trafic" section,
    # grouped by ligne and placed after the methodology.
    # Déduplication par CONTENU (route, titre, période) : le flux publie parfois
    # la même annonce sous plusieurs alert_id, ce qui créait des doublons.
    alerts_by_line: dict[str, list[tuple[str, str]]] = {}
    seen = set()
    for a in alerts or []:
        key = (a["route_id"], a["header_text"], a["active_period_start"], a["active_period_end"])
        if key in seen:
            continue
        seen.add(key)
        start = datetime.fromtimestamp(a["active_period_start"]).strftime("%d/%m")
        period = f"début {start} (en cours)"
        if a["active_period_end"]:
            end = datetime.fromtimestamp(a["active_period_end"]).strftime("%d/%m")
            period = f"du {start} au {end}"
        ligne = a["route_id"] if a["route_id"] else "Réseau"
        header = a["header_text"] or "(information non disponible)"
        alerts_by_line.setdefault(ligne, []).append((header, period))

    if alerts_by_line:
        # Rapport réseau complet : détail uniquement pour les lignes prioritaires,
        # les autres ne sont que mentionnées (volume du « Infos trafic »).
        priority_routes = {row.route_id for row in worst.itertuples()} if not scope.communes else None
        line_items = []
        for ligne in sorted(alerts_by_line):
            details = " ; ".join(
                f"{latex(header)} ({period})"
                for header, period in alerts_by_line[ligne]
            )
            if priority_routes is not None and ligne != "Réseau" and ligne not in priority_routes:
                continue
            line_items.append(rf"\item \textbf{{Ligne {latex(ligne)}}} : {details}")
        other_lines = [
            ligne for ligne in sorted(alerts_by_line)
            if priority_routes is not None and ligne != "Réseau" and ligne not in priority_routes
        ]
        if other_lines:
            counts = ", ".join(f"{latex(ligne)} ({len(alerts_by_line[ligne])})" for ligne in other_lines)
            line_items.append(rf"\item \textbf{{Autres lignes concernées}} (détail non affiché) : {counts}")
        alerts_section = (
            r"\newpage"
            r"\section*{Infos trafic}"
            r"\label{alertspage}"
            r"{\footnotesize"
            r"\begin{itemize}[leftmargin=1.2em,itemsep=.3em]"
            "\n"
            + "\n".join(line_items)
            + "\n"
            + r"\end{itemize}"
            r"}"
        )
        footer_note = (
            r"\fancyfoot[L]{{\ifnum\value{page}<3\scriptsize\color{alert}\alertmark{} "
            r"Lignes concernées par des infos trafic — voir page \pageref{alertspage}\fi}}"
        )
    else:
        alerts_section = ""
        footer_note = ""

    cover_logo = str(Path(__file__).resolve().parents[1] / "assets" / "logo" / "urban-vision-logo-white.png")

    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[french]{{babel}}
\usepackage{{fontspec}}
\setmainfont{{Lato}}
\usepackage[margin=1.7cm]{{geometry}}
\usepackage{{amsmath,booktabs,longtable,array,xcolor,tabularx,enumitem,graphicx,tcolorbox}}
\usepackage{{fancyhdr}}
\definecolor{{uvwhite}}{{HTML}}{{FFFFFF}}
\definecolor{{alert}}{{HTML}}{{BC6C25}}
\definecolor{{olive}}{{HTML}}{{606C38}}
\definecolor{{sunlitclay}}{{HTML}}{{DDA15E}}
\definecolor{{blackforest}}{{HTML}}{{283618}}
\definecolor{{teal}}{{HTML}}{{2A6F6F}}
\definecolor{{cornsilk}}{{HTML}}{{FEFAE0}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{blackforest}}{{URBAN VISION}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
{footer_note}
\setlength{{\parindent}}{{0pt}}
\newcommand{{\kpi}}[3][blackforest]{{\begin{{tcolorbox}}[width=.28\textwidth,sharp corners,boxrule=0pt,leftrule=3pt,colback=uvwhite,colframe=#1,arc=0pt,outer arc=0pt,left=6pt,right=4pt,top=4pt,bottom=4pt,halign=flush left,valign=top]{{\scriptsize #2\\[3pt]}}{{\Large\bfseries\color{{#1}} #3}}\end{{tcolorbox}}}}
\IfFontExistsTF{{Symbola}}{{%
  \newfontfamily{{\uvsym}}[Scale=MatchUppercase]{{Symbola}}%
  \newcommand{{\alertmark}}{{\textcolor{{alert}}{{\uvsym ⚠}}}}%
}}{{%
  \newcommand{{\alertmark}}{{\textcolor{{alert}}{{\textbf{{!}}}}}}%
}}

\begin{{document}}
\begin{{center}}
\begin{{tcolorbox}}[width=\textwidth,colback=blackforest,colframe=blackforest,arc=4pt,boxrule=0pt,left=14pt,right=14pt,top=12pt,bottom=12pt,halign=center]
\includegraphics[height=1.05cm]{{{cover_logo}}}\\[7pt]
{{\color{{cornsilk}}\LARGE\bfseries Rapport mensuel de fiabilité des transports}}\\[4pt]
{{\color{{cornsilk!75}}\large Urban Vision}}\\[4pt]
{{\color{{white}}\small {latex(report_month).capitalize()}}}\\[2pt]
{{\color{{white!85}}\footnotesize Périmètre : {latex(scope.description)}}}\\[2pt]
{{\color{{white!70}}\scriptsize Rapport produit par Elias Khallouk --- eliaskhallouk@gmail.com}}
\end{{tcolorbox}}
\end{{center}}
\vspace{{.45cm}}
\hrule\vspace{{.45cm}}
\section*{{Synthèse exécutive}}
\textit{{Cette page présente les indicateurs à retenir. Les résultats détaillés et la méthode figurent en annexe.}}\\[.5cm]
\makebox[\textwidth]{{\kpi[{kpi_color(metrics, 'fiability')}]{{Fiabilité}}{{{number(int(metrics['fiability']))}{net_val(network_metrics, 'fiability', number)}}}\hfill
\kpi{{Passages analysés}}{{{number(int(metrics['passages']))}{net_val(network_metrics, 'passages', number)}}}\hfill
\kpi[{kpi_color(metrics, 'ponctualite')}]{{Ponctualité (retard $\leq$ 5 min)}}{{{pct(float(metrics['ponctualite']))}{net_val(network_metrics, 'ponctualite', pct)}}}}}

\vspace{{1cm}}
\makebox[\textwidth]{{\kpi[{kpi_color(metrics, 'retard')}]{{Retard moyen}}{{{duration(float(metrics['retard']))}{net_val(network_metrics, 'retard', duration)}}}\hfill
\kpi[{kpi_color(metrics, 'retard_median')}]{{Retard médian}}{{{duration(float(metrics['retard_median']))}{net_val(network_metrics, 'retard_median', duration)}}}\hfill
\kpi[{kpi_color(metrics, 'skip_rate')}]{{Arrêts sautés}}{{{pct(float(metrics['skip_rate']), 2)}{net_val(network_metrics, 'skip_rate', lambda v: pct(v, 2))}}}}}

\vspace{{.7cm}}
\begin{{tabularx}}{{\textwidth}}{{@{{}}lXXXX@{{}}}}
\toprule
 & \textbf{{Fiabilité}} & \textbf{{Ponctualité}} & \textbf{{Retards moyen \& médian}} & \textbf{{Arrêts sautés}} \\
\midrule
\textbf{{Évolution du mois précédent}} & {latex(change['fiability'])} & {latex(change['ponctualite'])} & {latex(change['retard'])} & {latex(change['skip_rate'])} \\
\bottomrule
\end{{tabularx}}

\vspace{{.5cm}}
\textbf{{Lecture du mois.}} {executive_message(metrics, lines, scope)}

\vspace{{.3cm}}
\textbf{{Score de fiabilité.}} Ce score (sur 100) mesure la fiabilité du réseau sur le mois. Il part de la ponctualité : le pourcentage de passages avec au plus 5 minutes de retard. Puis il applique une pénalité pour les arrêts sautés : chaque pourcent d'arrêts sautés retire 2 points. Formule : \textit{{score = max(0 ; ponctualité - 2 $\times$ taux d'arrêts sautés)}}. Un score faible signale une ligne prioritaire.
{method_v2_summary(v2)}

\vspace{{.35cm}}
\textbf{{Alertes prioritaires}}
\begin{{itemize}}[leftmargin=1.4em,itemsep=.25em]
{priority_alerts}
\end{{itemize}}

\vfill
\small\color{{olive}} Source : flux GTFS-RT TripUpdates TBM, données arrêtées au {latex(collected_at)}. Les vingt dernières minutes du flux sont exclues afin de ne considérer que des observations stabilisées.
\newpage

\section*{{Annexe — Résultats détaillés}}
\textbf{{Périmètre analysé :}} {latex(scope.description)}. Les lignes sont classées de la plus à la moins prioritaire selon un score combinant la ponctualité et les arrêts sautés.\\[.4cm]
\renewcommand{{\arraystretch}}{{1.18}}
\begin{{longtable}}{{lrrrrr}}
\toprule
\textbf{{Ligne}} & \textbf{{Passages}} & \textbf{{À l'heure}} & \textbf{{Retards moy. / méd.}} & \textbf{{> 5 min}} & \textbf{{Arrêts sautés}} \\
\midrule
\endfirsthead
\toprule
\textbf{{Ligne}} & \textbf{{Passages}} & \textbf{{À l'heure}} & \textbf{{Retards moy. / méd.}} & \textbf{{> 5 min}} & \textbf{{Arrêts sautés}} \\
\midrule
\endhead
{line_table(lines, network_lines, alert_routes)}
\bottomrule
\end{{longtable}}

\small\color{{olive}} Sont exclues du classement les lignes comptant moins de {MIN_PASSAGES_FOR_RANKING} passages sur le mois (volume insuffisant pour un pourcentage d\'arrêts sautés exploitable) ainsi que les lignes à la demande (Flex\', Flex\'Night), sans desserte à horaires fixes. Le graphique « Arrêts les plus problématiques » ne retient que les arrêts d\'au moins {MIN_PASSAGES_FOR_RANKING} passages.

{graphical_annex(lines, scheduled, network_lines, stop_stats, monthly_evolution, output_dir)}

\newpage
\section*{{Méthode et calcul de la fiabilité}}
L'indice de fiabilité est un score synthétique (de 0 à 100) qui mesure à quel point les transports ont été fiables sur la période. Plus le score est élevé, meilleure est la fiabilité observée.

Contrairement à une simple mesure de temps, cet indicateur combine deux facteurs clés :

\begin{{itemize}}[leftmargin=1.4em]
\item \textbf{{La ponctualité (la base)}} : le pourcentage de passages effectués avec au plus 5 minutes de retard. Chaque passage à l'heure fait monter ce score de base ; au-delà de 5 minutes, le retard est jugé trop pénalisant pour l'usager et le passage ne compte plus comme « à l'heure ».
\item \textbf{{Les arrêts sautés (la pénalité)}} : lorsqu'un véhicule ne dessert pas un arrêt prévu (événement \texttt{{SKIPPED}}), la gêne est maximale. Chaque pourcent d'arrêts sautés retire donc 2 points au score.
\end{{itemize}}

\[
\text{{Score de fiabilité}} = \max(0 \;,\; \text{{Ponctualité}} - 2 \times \text{{Taux d'arrêts sautés}})
\]

\vspace{{.2cm}}
\textbf{{Exemple.}} Avec 92~\% de passages à l'heure et 3~\% d'arrêts sautés, le score est de $92 - 2 \times 3 = 86$ sur 100.

\vspace{{.2cm}}
À noter~:
\begin{{itemize}}[leftmargin=1.4em]
\item Un score faible indique une ligne prioritaire à corriger.
\item Ce rapport repose sur l'analyse des données GTFS-RT consolidées (observations sorties du flux depuis plus de 20 minutes) et permet de mesurer la qualité de service observée, sans en analyser les causes opérationnelles.
\end{{itemize}}

\subsection*{{Précision et limites}}
\textbf{{Marge d'incertitude.}} Les retards sont calculés à partir de l'heure de départ effective transmise par le véhicule dans le flux GTFS-RT. Ce flux est interrogé toutes les 60~secondes~; l'heure réelle de départ peut donc précéder ou suivre l'observation d'au plus 60~secondes. Cette marge d'incertitude ($\pm 60$~s) est inhérente au dispositif de collecte et ne remet pas en cause la pertinence des tendances présentées.

\textbf{{Trous de collecte.}} Le flux est relevé chaque minute ; une interruption de la collecte (redémarrage, indisponibilité du réseau ou du flux) peut faire manquer des passages, qui ne sont pas extrapolés.
{gap_line}
{evolution_note}

\textbf{{Alertes travaux.}} Les alertes de la section \textit{{Infos trafic}} (\alertmark) sont issues du flux ServiceAlerts TBM et sont reproduites à titre indicatif. Elles ne sont pas utilisées pour filtrer ou corriger les indicateurs de ponctualité. La présence d'une alerte sur une ligne ne signifie pas que les retards ou arrêts sautés observés sont causés par les travaux annoncés.

{method_v2_section(v2)}

{alerts_section}

\vspace{{.4cm}}
\hrule\vspace{{.3cm}}
\small Elias Khallouk --- eliaskhallouk@gmail.com \hfill Urban Vision --- {latex(collected_at)}
\end{{document}}
"""


def compile_pdf(tex_path: Path) -> Path:
    executable = shutil.which("xelatex") or shutil.which("lualatex")
    if not executable:
        raise RuntimeError("xelatex/lualatex introuvable. Le fichier .tex a été généré, mais ne peut pas être compilé en PDF.")
    command = [executable, "-interaction=nonstopmode", "-halt-on-error", tex_path.name]
    for _ in range(2):
        result = subprocess.run(command, cwd=tex_path.parent, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"La compilation LaTeX a échoué :\n{result.stdout[-2000:]}\n{result.stderr[-1000:]}")
    pdf_path = tex_path.with_suffix(".pdf")
    # Clean up auxiliary files
    for suffix in (".aux", ".log"):
        aux = tex_path.with_suffix(suffix)
        if aux.exists():
            aux.unlink()
    return pdf_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère le rapport mensuel Urban Vision (LaTeX/PDF).")
    parser.add_argument("--month", help="Mois analysé au format AAAA-MM (par défaut : dernier mois disponible).")
    parser.add_argument("--db-path", default=DEFAULT_DB, type=Path, help="Base SQLite à analyser.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT, type=Path, help="Répertoire des rapports générés.")
    parser.add_argument("--recipient", help="Destinataire affiché dans le rapport réseau.")
    parser.add_argument("--routes", help="Liste de route_id séparés par des virgules (filtre complémentaire facultatif).")
    parser.add_argument("--communes", help="Communes séparées par des virgules : le rapport est alors filtré sur leurs arrêts.")
    parser.add_argument("--profile", help="Identifiant d'un destinataire dans le fichier de profils.")
    parser.add_argument("--recipients-file", default=PROJECT_ROOT / "reports" / "recipients.json", help="Fichier JSON de profils territoriaux.")
    parser.add_argument("--compile", action="store_true", help="Compile aussi le .tex en PDF avec xelatex/lualatex.")
    args = parser.parse_args()
    if not args.db_path.exists():
        parser.error(f"Base introuvable : {args.db_path}")
    try:
        scope = load_scope(args)
        with sqlite3.connect(args.db_path, timeout=120) as conn:
            month = resolve_month(conn, args.month)
            scheduled, skipped, collected_at = query_observations(conn, month, scope)
            if scheduled.empty:
                content = build_no_data_latex(month, scope, collected_at)
                network_metrics = None
                network_lines = None
                stop_stats = None
            else:
                lines = ranking_lines(make_line_stats(scheduled, skipped))
                current = kpis(scheduled, skipped)
                previous_scheduled, previous_skipped, _ = query_observations(conn, previous_month(month), scope)
                previous = kpis(previous_scheduled, previous_skipped) if not previous_scheduled.empty else None
                network_metrics = None
                network_lines = None
                stop_stats = None
                monthly_evolution = None
                if scope.communes:
                    net_scope = Scope("Réseau TBM", scope.routes, [], "Réseau TBM global")
                    net_scheduled, net_skipped, _ = query_observations(conn, month, net_scope)
                    if not net_scheduled.empty:
                        network_metrics = kpis(net_scheduled, net_skipped)
                        network_lines = ranking_lines(make_line_stats(net_scheduled, net_skipped))
                    stop_stats = query_stop_stats(conn, month, scope)
                monthly_evolution = query_monthly_evolution(conn, month, scope)
                gaps = query_collection_gaps(conn, month)
                route_ids = set(scheduled["route_id"].unique()) if not scheduled.empty else set()
                alerts_data = query_service_alerts(conn, month, route_ids) if route_ids else []
                v2 = query_method_v2(conn, month, scope)
                content = build_latex(month, scope, current, comparison(current, previous),
                                      lines, scheduled, collected_at,
                                      args.output_dir,
                                      network_metrics, network_lines, stop_stats,
                                      monthly_evolution, gaps, alerts_data, v2)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        tex_path = args.output_dir / f"urban-vision-{month}-{safe_slug(scope.recipient)}.tex"
        tex_path.write_text(content, encoding="utf-8")
        if args.compile:
            pdf_path = compile_pdf(tex_path)
            tex_path.unlink(missing_ok=True)
            print(f"PDF généré : {pdf_path}")
        else:
            print(f"Rapport LaTeX généré : {tex_path}")
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
