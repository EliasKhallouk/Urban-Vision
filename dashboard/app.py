"""Tableau de bord de fiabilite des passages TBM.

Charte écran « Pierre et vin » (fond blanc, jetons de reports/palette.py) : encre,
texte secondaire, surface, trait, couleur d'action et dégradé continu Garonne,
pierre, vin, lie, à repères fixes ; les rapports gardent leurs trois paliers. Les modes de transport se
distinguent par la forme du marqueur (tram ●, bus ■, ferry ▲), jamais par la couleur. Les observations les plus recentes
restent dans le flux GTFS-RT : elles sont ecartees afin de ne mesurer que des
passages pour lesquels le retard est stabilise.

Architecture de chargement : les aggregations lourdes sont faites en SQL (seuls
quelques resultats agregees transitent en pandas, pas les 1,3 M de lignes brutes),
les pages sont rendues paresseusement (seule la page active calcule ses
graphiques) et les loaders sont mis en cache 60 secondes.
"""

import base64
import html
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from carte import carte_arrets, hover_routes, map_payload
from carte_troncons import carte_troncons, tram_lines_payload, troncons_payload
from graphique import graphique_cliquable

import diagnostic as dg

from diagnostic import (
    PERIOD_ORDER,
    format_number,
    format_seconds,
    number_formatter,
    median_from_hists as _median_from_hists,
    period_labels as _period_labels,
)
from highcharts import (
    GROUP_ALL,
    TREND_METRICS,
    groups_hourly_chart,
    groups_trend_chart,
    period_score_chart,
    render as hc_render,
    score_sparkline,
    ranking_chart,
    scatter_chart,
    commune_ranking_chart,
    mode_comparison_chart,
    period_mode_chart,
    engagement_progression_chart,
    delay_distribution_chart,
    collection_minutely_chart,
    hourly_distribution_chart,
    line_profile_chart,
    slot_profile_chart,
    skip_profile_chart,
    stop_lines_chart,
    risk_by_label_chart,
    daily_status_chart,
    cancellations_chart,
)

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "urban_vision.db"
FRESHNESS_BUFFER_SECONDS = 20 * 60
CACHE_TTL_SECONDS = 60
MIN_OBSERVATIONS = 50
COLLECTION_HOURLY_DAYS = 7

# Constantes couleur : importées du module partagé reports/palette.py pour garantir
# une seule source de vérité (palette et seuils de couleur identiques aux rapports).
_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
_SRC_SCRIPTS = str(Path(__file__).resolve().parents[1] / "src" / "scripts")
if _SRC_SCRIPTS not in sys.path:
    sys.path.insert(0, _SRC_SCRIPTS)
import indicateurs as ind  # noqa: E402
import troncons  # noqa: E402
from generate_all_reports import slug as report_slug  # noqa: E402
from palette import (  # noqa: E402
    ACTION,
    GARONNE,
    INK,
    INK_MUTED,
    LIE,
    LINE,
    PIERRE,
    SCORE_ANCHORS,
    SCREEN_HEX,
    SCREEN_NO_DATA,
    SURFACE,
    VIN,
    ink_on,
    score_color,
    screen_hex,
    kpi_tier as palette_kpi_tier,
    TIER_LABELS,
    tier_label,
    mode_glyph,
    mode_marker,
    CRITICAL,
    FREQUENT_SHORT,
    LOW,
    RARE_LONG,
    RISK_MEDIAN_S,
    RISK_PCT_GT300,
)


CAUTION_TEXT = (
    "Cette ligne fait l'objet d'une perturbation signalée par TBM sur tout ou "
    "partie de la période — sans lien de causalité établi arrêt par arrêt avec "
    "les statistiques présentées."
)

def fmt_int(value) -> str:
    """Entier avec séparateur de milliers à la française (espace)."""
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.0f}".replace(",", " ")


def _score_tier_style(value: float) -> str:
    """Style de cellule selon le dégradé continu des scores (`palette.score_color`)."""
    color = score_color(value)
    return f"background-color: {color}; color: {ink_on(color)};"


def _delta_style(value: float, threshold: float = 2.0) -> str:
    """Style d'une cellule d'évolution : neutre, une évolution n'est pas un état."""
    return f"color: {INK}; font-weight: 600;" if abs(value) >= threshold else f"color: {INK_MUTED};"


def score_gradient_css() -> str:
    """Dégradé CSS des scores de 100 (gauche) à 20 (droite), repères aux positions de `SCORE_ANCHORS`."""
    stops = ", ".join(f"{c} {(100 - a) / 80 * 100:.1f}%" for a, c in zip(SCORE_ANCHORS, (GARONNE, PIERRE, VIN, LIE)))
    return f"linear-gradient(90deg, {GARONNE} 0%, {stops}, {LIE} 100%)"


def render_tier_legend(title: str = "", invert: bool = False) -> None:
    """Légende des couleurs, en mots courants.

    Pour le score : barre du dégradé continu avec les repères fiable (80) et problématique (50).
    `invert=True` pour les métriques où « plus = pire » (retards, arrêts non desservis, écart à
    l'horaire), tracées par paliers : trois pastilles, le palier fiable à gauche.
    """
    head = f"<b>{title}</b>&nbsp; " if title else ""
    if not invert:
        bar = (f'<span class="tl-bar" style="background:{score_gradient_css()}"></span>'
               '<span class="tl-ticks"><span style="left:25%">80 : fiable</span>'
               '<span style="left:62.5%">50 : problématique</span></span>')
        st.markdown(f'<div class="tier-legend">{head}<span class="tl-scale">{bar}</span></div>',
                    unsafe_allow_html=True)
        return
    chips = "".join(
        '<span style="display:inline-flex;align-items:center;gap:.35rem;margin-right:1rem">'
        f'<span style="width:.72rem;height:.72rem;border-radius:3px;background:{SCREEN_HEX[t]}"></span>'
        f'<b>{TIER_LABELS[t].capitalize()}</b></span>'
        for t in ("positif", "moyen", "negatif"))
    st.markdown(f'<div class="tier-legend">{head}{chips}</div>', unsafe_allow_html=True)


MODE_LABELS = {0: "Tramway", 3: "Bus", 4: "Ferry", 2: "Rail", 5: "Câble", 7: "Funiculaire", 11: "Trolleybus"}
CAUSE_LABELS = {
    1: "Inconnu", 2: "Autre", 3: "Problème technique", 4: "Grève", 5: "Demande",
    6: "Météo", 7: "Maintenance", 8: "Travaux", 9: "Activité de police",
    10: "Urgence médicale", 11: "Accident",
}

DIST_BUCKETS = [
    "neg600", "neg300", "neg120", "neg60", "neg0",
    "pos0", "pos60", "pos120", "pos300", "pos600", "pos1200",
]
DIST_LABELS = [
    "< −10 min", "−10 à −5", "−5 à −2", "−2 à −1", "−1 à 0",
    "0 à +1", "+1 à +2", "+2 à +5", "+5 à +10", "+10 à +20", "> +20 min",
]


MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
             "octobre", "novembre", "décembre"]
ROLLING_DAYS = (7, 30)
MID_MONTH_DAY = 15

# Index opportunistes : sans eux, chaque requête du dashboard scanne toute la
# table observations (1,4 M de lignes). CREATE INDEX IF NOT EXISTS est idempotent,
# donc l'ajout se fait automatiquement au premier démarrage d'une base existante.
INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_observations_last_seen_at ON observations(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_observations_route ON observations(route_id)",
    # Couvre les vues bornées par `departure_time >= ?` (période Grafana) : le
    # scan ne lit que la tranche d'index sans accéder aux lignes de la table.
    "CREATE INDEX IF NOT EXISTS idx_observations_departure_time"
    " ON observations(departure_time, schedule_relationship, departure_delay, route_id)",
    "CREATE INDEX IF NOT EXISTS idx_service_alerts_period"
    " ON service_alerts(active_period_start, active_period_end)",
]


def _day_bounds(since_ts: int | None, end_ts: int | None, cutoff_ts: int) -> tuple[str, str]:
    """Bornes exclusives de dates-service (AAAA-MM-JJ) de la période choisie.

    Les agrégats sont stockés par journée de service complète : une période est
    un ensemble de jours, pas un intervalle à la seconde près. `end_ts` est
    fourni comme minuit local du jour *suivant* le dernier jour inclus.
    """
    since_day = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d") if since_ts is not None else "0000-00-00"
    if end_ts is None:
        end_day = (datetime.fromtimestamp(cutoff_ts) + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        end_day = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d")
    return since_day, end_day


def _ensure_aggregates(conn: sqlite3.Connection) -> None:
    """Crée les tables d'agrégation et les reconstruit si besoin.

    En production, c'est le collecteur (toutes les ~60 s) qui tient ces tables
    à jour ; ce garde-fou couvre le (re)démarrage d'une base qui ne les a jamais,
    une migration qui ajoute une table (ex. agg_daily_stop) ou un backfill
    partiel : dès que agg_daily_stop couvre moins de jours de service que
    agg_daily, un recalcul complet est déclenché pour aligner l'historique.
    """
    src_dir = Path(__file__).resolve().parents[1] / "src" / "scripts"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import db as _db

    conn.executescript(_db.AGG_DDL)
    n_daily = conn.execute("SELECT COUNT(*) FROM agg_daily").fetchone()[0]
    n_days_daily = conn.execute("SELECT COUNT(DISTINCT date_service) FROM agg_daily").fetchone()[0]
    n_days_stop = conn.execute("SELECT COUNT(DISTINCT date_service) FROM agg_daily_stop").fetchone()[0]
    if n_daily == 0 or n_days_stop < n_days_daily:
        _db.refresh_aggregates(conn, days=None)

    # Directions des arrêts : backfill one-shot (~min sur une grosse base).
    conn.executescript(_db.STOP_DIRECTION_DDL)
    n_dir = conn.execute("SELECT COUNT(*) FROM stop_direction").fetchone()[0]
    if n_dir == 0:
        _db.refresh_stop_directions(conn)


class _MedianAgg:
    """Agrégat SQLite `median_s` : médiane exacte, identique à pandas.median().

    Conservé pour compatibilité ; les loaders utilisent désormais les
    histogrammes de agg_daily.
    """

    def __init__(self) -> None:
        self.values: list = []

    def step(self, value) -> None:
        if value is not None:
            self.values.append(value)

    def finalize(self):
        if not self.values:
            return None
        self.values.sort()
        n = len(self.values)
        if n % 2 == 1:
            return self.values[n // 2]
        return (self.values[n // 2 - 1] + self.values[n // 2]) / 2.0


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for ddl in INDEX_DDL:
        try:
            conn.execute(ddl)
        except sqlite3.Error:
            pass
    conn.commit()
    for pragma in (
        "PRAGMA busy_timeout = 120000",     # le collecteur écrit en continu : attendre, ne pas échouer
        "PRAGMA cache_size = -65536",        # page cache mémoire de 64 Mo (lectures)
        "PRAGMA mmap_size = 268435456",      # 256 Mo de mapping mémoire si dispo
        "PRAGMA temp_store = MEMORY",        # tris/group-by en mémoire
    ):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass
    try:
        conn.create_aggregate("median_s", 1, _MedianAgg)
    except sqlite3.Error:
        pass
    _ensure_aggregates(conn)
    return conn


def get_cutoff_ts(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(last_seen_at) AS max_ts FROM observations").fetchone()
    return None if row["max_ts"] is None else int(row["max_ts"]) - FRESHNESS_BUFFER_SECONDS


def _date_and_time(ts: int | None) -> tuple[str, str | None]:
    if not ts:
        return "inconnue", None
    moment = datetime.fromtimestamp(ts)
    return moment.strftime("%d/%m/%Y"), moment.strftime("à %H:%M")


def format_date(ts: int | None) -> str:
    if not ts:
        return "inconnue"
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y à %H:%M")


def _day_midnight(d: datetime.date) -> datetime:
    return datetime.combine(d, dtime(0, 0))


@dataclass(frozen=True)
class Period:
    """Période analysée (jours de service, fin exclue) et période de comparaison."""
    start: date
    end: date
    label: str
    prev_start: date | None
    prev_end: date | None
    prev_label: str | None


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def _previous_month(d: date) -> date:
    return _month_start(_month_start(d) - timedelta(days=1))


def period_options(first_day: date, last_day: date) -> list[str]:
    """Choix proposés : les mois couverts (du plus récent au plus ancien), puis les autres périodes."""
    months, m = [], _month_start(last_day)
    while m >= _month_start(first_day):
        months.append(f"mois:{m:%Y-%m}")
        m = _previous_month(m)
    return months + [f"jours:{n}" for n in ROLLING_DAYS] + ["tout", "dates"]


def default_period_choice(first_day: date, last_day: date) -> str:
    """Mois en cours s'il a au moins MID_MONTH_DAY jours de données, sinon le mois précédent complet."""
    current = _month_start(last_day)
    if last_day.day >= MID_MONTH_DAY or _previous_month(current) < _month_start(first_day):
        return f"mois:{current:%Y-%m}"
    return f"mois:{_previous_month(current):%Y-%m}"


def period_choice_label(choice: str, last_day: date) -> str:
    if choice.startswith("mois:"):
        m = date.fromisoformat(choice[5:] + "-01")
        label = f"{MONTHS_FR[m.month - 1].capitalize()} {m.year}"
        if _month_start(last_day) == m and _next_month(m) - timedelta(days=1) != last_day:
            label += f" (jusqu'au {last_day:%d/%m})"
        return label
    if choice.startswith("jours:"):
        return f"{choice[6:]} derniers jours"
    return "Toute la période collectée" if choice == "tout" else "Dates précises…"


def resolve_period(choice: str, first_day: date, last_day: date,
                   custom: tuple[date, date] | None = None) -> Period:
    """Bornes de la période choisie et de sa période de comparaison.

    Mois : le mois civil, comparé au mois précédent. N derniers jours : jusqu'au
    dernier jour de données, comparés aux N jours d'avant. Dates précises :
    comparées à la même durée juste avant. Toute la période : sans comparaison.
    La comparaison est omise si elle tombe avant le premier jour de données.
    """
    if choice.startswith("mois:"):
        start = date.fromisoformat(choice[5:] + "-01")
        end = _next_month(start)
        prev_start, prev_end = _previous_month(start), start
        label = f"{MONTHS_FR[start.month - 1]} {start.year}"
        prev_label = f"{MONTHS_FR[prev_start.month - 1]} {prev_start.year}"
    elif choice.startswith("jours:"):
        n = int(choice[6:])
        end = last_day + timedelta(days=1)
        start = end - timedelta(days=n)
        prev_start, prev_end = start - timedelta(days=n), start
        label, prev_label = f"les {n} derniers jours", f"les {n} jours précédents"
    elif choice == "dates" and custom:
        start, end = custom[0], custom[-1] + timedelta(days=1)
        n = (end - start).days
        prev_start, prev_end = start - timedelta(days=n), start
        label = f"du {start:%d/%m/%Y} au {custom[-1]:%d/%m/%Y}"
        prev_label = f"les {n} jours précédents"
    else:
        return Period(first_day, last_day + timedelta(days=1), "toute la période collectée", None, None, None)
    if prev_end <= first_day:
        prev_start = prev_end = prev_label = None
    return Period(start, end, label, prev_start, prev_end, prev_label)


def _ts(d: date | None) -> int | None:
    return None if d is None else int(_day_midnight(d).timestamp())


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_service_days(_conn, cutoff_ts: int) -> tuple[date, date]:
    """Premier jour de service agrégé et dernier jour stabilisé (jour du seuil de fraîcheur)."""
    row = _conn.execute("SELECT MIN(date_service) FROM agg_daily").fetchone()
    last_day = datetime.fromtimestamp(cutoff_ts).date()
    first_day = date.fromisoformat(row[0]) if row and row[0] else last_day
    return min(first_day, last_day), last_day


def period_picker(conn, cutoff_ts: int, locked: bool = False) -> Period:
    """Choix de la période : un mois complet par défaut, ou une autre période.

    `locked` : la page affichée a sa propre période (carte des tronçons) ; le choix est montré grisé,
    avec la raison, et la période choisie ailleurs est conservée pour les autres pages.
    """
    first_day, last_day = load_service_days(conn, cutoff_ts)
    options = period_options(first_day, last_day)
    if st.session_state.get("period_choice") not in options:
        st.session_state["period_choice"] = default_period_choice(first_day, last_day)
    if locked:
        st.selectbox("Période", ["Période scolaire"], key="period_locked", disabled=True,
                     help="Sans effet sur cette page : la carte des tronçons porte sur la période scolaire, "
                          "depuis la rentrée, recalculée chaque nuit.")
        return resolve_period(st.session_state["period_choice"], first_day, last_day,
                              st.session_state.get("_period_custom"))
    choice = st.selectbox("Période", options, key="period_choice",
                          format_func=lambda c: period_choice_label(c, last_day),
                          help="Un mois complet est comparé au mois précédent ; les autres périodes, à la "
                               "même durée juste avant.")
    custom = None
    if choice == "dates":
        picked = st.date_input("Du … au …", value=(max(first_day, last_day - timedelta(days=13)), last_day),
                               min_value=first_day, max_value=last_day, format="DD/MM/YYYY", key="period_dates")
        picked = picked if isinstance(picked, (list, tuple)) else (picked,)
        custom = (picked[0], picked[-1]) if picked else None
    st.session_state["_period_custom"] = custom
    return resolve_period(choice, first_day, last_day, custom)


def inject_style() -> None:
    st.markdown(
        f"""
        <style>
        :root {{
            --fs-hero: 4.2rem; --fs-page: 1.75rem; --fs-figure: 1.6rem; --fs-section: 1.3rem; --fs-block: 1.0625rem;
            --fs-body: 1rem; --fs-small: .875rem; --fs-label: .8125rem; --fs-micro: .75rem;
            --lh-title: 1.2; --lh-body: 1.55; --lh-small: 1.45;
            --ink: {INK}; --ink-2: {INK_MUTED}; --surface: {SURFACE}; --line: {LINE}; --action: {ACTION};
            --garonne: {GARONNE}; --pierre: {PIERRE}; --vin: {VIN}; --lie: {LIE};
        }}
        .stApp {{ background: #FFFFFF; color: var(--ink); }}
        [data-testid="stMain"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stMain"] [data-testid="stMarkdownContainer"] li {{ font-size: var(--fs-body); line-height: var(--lh-body); }}
        [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ font-size: var(--fs-small) !important; line-height: var(--lh-small); color: var(--ink-2) !important; opacity: 1 !important; }}
        [data-testid="stMain"] [data-testid="stWidgetLabel"] p {{ font-size: var(--fs-small); font-weight: 600; color: var(--ink); }}
        [data-testid="stExpander"] summary p {{ font-size: var(--fs-body); font-weight: 600; }}
        [data-testid="stHeader"] {{ background: transparent; }}
        .block-container {{ max-width: 1500px; padding-top: 2.1rem; padding-bottom: 3rem; }}

        [data-testid="stSidebar"] {{ background: var(--surface); border-right: 1px solid var(--line); }}
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {{ display: none; }}
        [data-testid="stSidebar"] > div:first-child {{ padding-top: 1.2rem; }}
        .sidebar-brand {{ display: flex; flex-direction: column; align-items: flex-start; gap: .55rem; margin: .1rem 0 1rem; }}
        .sidebar-brand img {{ height: 120px; width: auto; max-width: 100%; mix-blend-mode: multiply; }}
        .sidebar-brand span {{ color: var(--ink); font-size: var(--fs-section); font-weight: 700; letter-spacing: -.01em; line-height: var(--lh-title); }}
        .sidebar-nav-label {{ color: var(--ink-2); font-size: var(--fs-small); font-weight: 600; margin: .8rem 0 .4rem; }}
        [data-testid="stSidebar"] [data-testid="stButton"] button {{ background: #FFFFFF; border-color: var(--line); }}
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover,
        [data-testid="stSidebar"] [data-testid="stButton"] button:focus-visible {{ border-color: var(--action); color: var(--action); }}
        [data-testid="stSidebar"] [data-testid="stButton"] button:hover p {{ color: var(--action); }}
        [data-testid="stSidebar"] hr {{ border-color: var(--line); }}
        [data-testid="stSidebar"] [data-testid="stCheckbox"] {{ margin-top: 1.2rem; }}
        [data-testid="stSidebar"] [data-testid="stCheckbox"] label p {{ color: var(--ink); font-weight: 600; font-size: var(--fs-body); }}
        [data-testid="stSidebar"] [data-testid="stCheckbox"] label:has(input:checked) > span + div {{ background-color: var(--action) !important; }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label {{
            color: var(--ink); opacity: 1; padding: .5rem .6rem; border-radius: 8px; font-size: var(--fs-body); font-weight: 600;
            margin-bottom: .15rem; cursor: pointer; transition: background .12s ease, color .12s ease;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:hover {{ background: rgba(43, 37, 35, .06); }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {{
            background: #FFFFFF; box-shadow: inset 0 0 0 1px var(--line); font-weight: 700;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label [data-testid="stMarkdownContainer"] p {{
            color: var(--ink) !important; margin: 0; font-size: var(--fs-body); font-weight: 600;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) [data-testid="stMarkdownContainer"] p {{
            color: var(--action) !important; font-weight: 700;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) > div:first-child {{ background: var(--action) !important; border-color: var(--action) !important; }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:nth-child(n+4) {{ padding: .3rem .6rem; }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:nth-child(n+4) [data-testid="stMarkdownContainer"] p {{ font-size: var(--fs-small) !important; font-weight: 600; color: var(--ink-2) !important; }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:nth-child(4) {{ margin-top: 1rem; border-top: 1px solid var(--line); border-radius: 0 0 8px 8px; padding-top: .8rem; }}

        /* ---- Titres ---- */
        h1, h2, h3, h4 {{ color: var(--ink) !important; letter-spacing: -.01em; line-height: var(--lh-title) !important; }}
        h1 {{ font-size: var(--fs-page) !important; font-weight: 700 !important; padding: 0 !important; margin: 0 0 .35rem !important; }}
        h2, h3 {{ font-size: var(--fs-section) !important; font-weight: 700 !important; padding: 0 !important; margin: 1.6rem 0 .6rem !important; }}
        h4 {{ font-size: var(--fs-block) !important; font-weight: 700 !important; padding: 0 !important; margin: 1.3rem 0 .45rem !important; }}

        /* ---- Cartes KPI : fond blanc uniforme + bordure gauche sémantique ---- */
        .kpi-card {{ background: #FFFFFF; border: 1px solid var(--line); border-radius: 10px; padding: .95rem 1.05rem; height: 100%; }}
        .kpi-dot {{ flex: none; width: .6em; height: .6em; border-radius: 50%; box-shadow: 0 0 0 1px rgba(43, 37, 35, .18); }}
        .kpi-label {{ display: flex; align-items: center; color: var(--ink-2); font-size: var(--fs-label); font-weight: 600; line-height: var(--lh-small); }}
        .kpi-value {{ display: flex; align-items: center; gap: .45rem; color: var(--ink); font-size: var(--fs-figure); font-weight: 700; margin-top: .25rem; line-height: var(--lh-title); min-width: 0; }}
        .kpi-sublabel {{ color: var(--ink-2); font-size: var(--fs-label); margin-top: .3rem; font-weight: 400; line-height: var(--lh-small); }}
        [data-testid="stMain"] [data-testid="stMarkdownContainer"] a {{ color: var(--action); text-decoration-color: var(--action); }}
        .lexique-item {{ padding: .5rem .75rem; margin: 0 -.75rem; border-left: 3px solid transparent; border-radius: 4px; }}
        .lexique-focus {{ background: var(--surface); border-left-color: var(--action); }}
        .kpi-help {{ position: relative; display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; margin-left: .35rem; border-radius: 50%; background: var(--ink-2); color: #FFFFFF; font-size: var(--fs-micro); font-weight: 700; letter-spacing: 0; text-transform: none; flex: none; cursor: help; }}
        .kpi-help:focus-visible {{ outline: 2px solid var(--action); outline-offset: 2px; }}
        .kpi-tip {{ display: none; position: absolute; z-index: 30; top: calc(100% + 8px); left: -12px; width: 260px; padding: .65rem .75rem; border-radius: 8px; background: #FFFFFF; border: 1px solid var(--line); box-shadow: 0 6px 18px rgba(43, 37, 35, .18); color: var(--ink) !important; font-size: var(--fs-small); font-weight: 400; line-height: var(--lh-small); letter-spacing: 0; text-transform: none; text-align: left; white-space: normal; cursor: default; }}
        .kpi-tip::before {{ content: ""; position: absolute; top: -10px; left: 0; right: 0; height: 10px; }}
        .kpi-tip a {{ display: block; margin-top: .45rem; color: var(--action) !important; font-weight: 700; text-decoration-color: var(--action); }}
        .kpi-help:hover .kpi-tip, .kpi-help:focus-within .kpi-tip {{ display: block; }}
        [data-testid="stColumn"]:last-child .kpi-tip {{ left: auto; right: -12px; }}
        [data-testid="stTooltipIcon"] svg {{ width: 19px; height: 19px; stroke: var(--ink); stroke-width: 2.4; }}
        [data-testid="stTooltipIcon"] button {{ opacity: 1; }}

        .eyebrow {{ color: var(--ink-2); font-size: var(--fs-small); font-weight: 600; margin-top: .4rem; }}
        .hero-subtitle {{ color: var(--ink-2); font-size: var(--fs-body); line-height: var(--lh-body); margin-bottom: 1.2rem; }}
        .section-note {{ color: var(--ink-2); font-size: var(--fs-small); line-height: var(--lh-small); margin: 0 0 .75rem; }}
        .insight {{ background: #ffffff; border-left: 3px solid var(--ink); border-radius: 8px; padding: .8rem 1rem; margin: .4rem 0 .9rem; color: var(--ink); font-size: var(--fs-body); line-height: var(--lh-body); box-shadow: 0 1px 3px rgba(43, 37, 35, .08); }}
        .insight.brief {{ margin: 1rem 0 .6rem; }}
        .insight.brief ul, .hints ul {{ margin: .35rem 0 0 1.1rem; padding: 0; }}
        .insight.brief li {{ margin-bottom: .3rem; line-height: var(--lh-body); }}
        .hints {{ border: 1px dashed var(--line); border-radius: 8px; padding: .7rem 1rem; color: var(--ink); margin-bottom: 1rem; }}
        .hints li {{ margin-bottom: .25rem; }}
        @keyframes fiche-in {{ from {{ opacity: 0; transform: translateY(18px); }} to {{ opacity: 1; transform: none; }} }}
        @media (prefers-reduced-motion: no-preference) {{
            [class*="st-key-fiche_"] {{ animation: fiche-in 260ms cubic-bezier(.2, .7, .2, 1) both; }}
        }}
        [data-testid="stHeaderActionElements"] {{ display: none; }}
        [data-testid="stMain"] button:focus-visible, [data-testid="stSidebar"] button:focus-visible,
        [data-testid="stSidebar"] [role="radiogroup"] label:has(input:focus-visible) {{ outline: 2px solid var(--action) !important; outline-offset: 2px; }}
        .crumb-label {{ color: var(--ink-2); font-size: var(--fs-small); white-space: nowrap; }}
        .crumb-sep {{ color: var(--ink-2); font-size: var(--fs-body); }}
        .crumb-here {{ color: var(--ink); font-size: var(--fs-small); font-weight: 700; }}
        .st-key-crumbs {{ gap: .35rem !important; margin-top: .35rem; flex-wrap: wrap; }}
        .st-key-crumbs [data-testid="stMarkdownContainer"] p {{ margin: 0; }}
        .st-key-crumbs {{ align-items: center !important; }}
        .st-key-crumbs [data-testid="stElementContainer"] {{ margin: 0 !important; width: auto !important; }}
        .st-key-crumbs [data-testid="stButton"] button {{ min-height: 0; height: auto; padding: 0; line-height: 1.45; }}
        .st-key-crumbs [data-testid="stButton"] button p {{ color: var(--action); font-size: var(--fs-small); font-weight: 600; text-decoration: underline; text-underline-offset: 3px; }}
        .hero {{ padding: .4rem 0; }}
        .hero-kicker {{ color: var(--ink-2); font-size: var(--fs-small); font-weight: 600; }}
        .hero-score {{ display: flex; align-items: center; gap: .6rem; color: var(--ink); font-size: var(--fs-hero); font-weight: 700; line-height: 1; letter-spacing: -.03em; font-variant-numeric: tabular-nums; margin: .35rem 0 .2rem; }}
        .hero-dot {{ flex: none; width: .42em; height: .42em; border-radius: 50%; box-shadow: 0 0 0 1px rgba(43, 37, 35, .18); }}
        .hero-max {{ color: var(--ink-2); font-size: var(--fs-section); font-weight: 600; letter-spacing: 0; align-self: flex-end; margin-bottom: .45rem; }}
        .hero-word {{ color: var(--ink); font-size: var(--fs-section); font-weight: 700; }}
        .hero-sub {{ color: var(--ink-2); font-size: var(--fs-small); margin-top: .15rem; }}
        .hero-evo {{ display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .6rem; margin-top: .7rem; }}
        .hero-chip {{ display: inline-block; padding: .2rem .6rem; border-radius: 999px; background: var(--surface); border: 1px solid var(--line); color: var(--ink); font-size: var(--fs-small); font-weight: 600; }}
        .hero-chip.strong {{ border-color: var(--ink); }}
        .hero-why {{ color: var(--ink-2); font-size: var(--fs-label); }}
        .hero-curve-title {{ color: var(--ink-2); font-size: var(--fs-small); font-weight: 600; margin-bottom: -.2rem; }}
        [class*="st-key-tile_"]:not([class*="st-key-tile_btn"]) {{ background: var(--surface); border-radius: 10px; padding: 1rem 1.1rem .8rem; min-height: 9.5rem; gap: .5rem; }}
        [class*="st-key-tile_btn"] {{ margin-top: auto; }}
        .tile-figure {{ color: var(--ink); font-size: var(--fs-figure); font-weight: 700; line-height: 1.1; font-variant-numeric: tabular-nums; }}
        .tile-label {{ color: var(--ink); font-size: var(--fs-body); font-weight: 600; margin-top: .3rem; line-height: var(--lh-small); }}
        .tile-detail {{ color: var(--ink-2); font-size: var(--fs-small); line-height: var(--lh-small); margin-top: .15rem; }}
        [class*="st-key-tile_btn"] button, [class*="_btn_"] button[kind="tertiary"] {{ padding: 0; min-height: 0; }}
        [class*="st-key-tile_btn"] button p, [class*="st-key-atraiter_btn"] button p, [class*="st-key-plusrates_btn"] button p {{ color: var(--action); font-weight: 600; font-size: var(--fs-small); }}
        [class*="st-key-atraiter_"]:not([class*="_btn_"]), [class*="st-key-plusrates_"]:not([class*="_btn_"]) {{ border-bottom: 1px solid var(--line); padding: .45rem 0; }}
        [class*="st-key-atraiter_btn"] button p, [class*="st-key-plusrates_btn"] button p {{ color: var(--ink) !important; font-size: var(--fs-body) !important; font-weight: 700 !important; text-align: left; }}
        [class*="st-key-atraiter_btn"] button:hover p, [class*="st-key-plusrates_btn"] button:hover p {{ color: var(--action) !important; text-decoration: underline; }}
        .sr-right {{ display: flex; align-items: center; gap: .8rem; }}
        [class*="st-key-atraiter_"] [data-testid="stColumn"] [data-testid="stVerticalBlock"],
        [class*="st-key-plusrates_"] [data-testid="stColumn"] [data-testid="stVerticalBlock"] {{ gap: 0; }}
        .sr-score {{ display: inline-flex; justify-content: center; min-width: 2.4rem; border-radius: 6px; padding: .12rem 0; font-size: var(--fs-small); font-weight: 700; font-variant-numeric: tabular-nums; }}
        .sr-where {{ display: block; color: var(--ink-2); font-size: var(--fs-label); font-weight: 400; }}
        .sr-bar {{ display: block; flex: none; height: .5rem; border-radius: 3px; background: var(--ink-2); opacity: .55; }}
        .sr-fig {{ color: var(--ink-2); font-size: var(--fs-small); white-space: nowrap; font-variant-numeric: tabular-nums; }}
        @media (max-width: 640px) {{ .sr-right {{ flex-wrap: wrap; }} .sr-fig {{ white-space: normal; }} }}
        .empty-ok {{ color: var(--ink); font-size: var(--fs-body); padding: .6rem .9rem; background: var(--surface); border-radius: 8px; }}
        .map-legend {{ display: flex; flex-wrap: wrap; gap: .35rem 1.6rem; margin: .2rem 0 .6rem; color: var(--ink); font-size: var(--fs-small); line-height: var(--lh-small); }}
        .map-legend > div {{ display: flex; align-items: center; flex-wrap: wrap; gap: .3rem .7rem; }}
        .lg-item {{ display: inline-flex; align-items: center; gap: .35rem; }}
        .lg-swatch {{ width: .8rem; height: .8rem; border-radius: 3px; }}
        .lg-glyph {{ font-size: var(--fs-block); line-height: 1; }}
        .tl-scale {{ display: inline-flex; flex-direction: column; vertical-align: middle; width: 21rem; max-width: 100%; margin-left: .4rem; }}
        .tl-bar {{ display: block; height: .7rem; border-radius: 3px; }}
        .tl-ticks {{ position: relative; display: block; height: 1.1rem; font-size: var(--fs-micro); color: var(--ink-2); }}
        .tl-ticks span {{ position: absolute; top: .1rem; transform: translateX(-50%); white-space: nowrap; }}
        .tl-ticks span::before {{ content: ""; position: absolute; left: 50%; top: -.25rem; width: 1px; height: .3rem; background: var(--ink-2); }}
        .zone-banner {{ display: grid; grid-template-columns: auto 1fr; gap: .1rem 1rem; align-items: baseline; margin: .9rem 0 .2rem; padding: .75rem 1rem; border-radius: 10px; background: var(--surface); border: 1px solid var(--line); }}
        .zone-kicker {{ grid-row: span 2; align-self: center; color: var(--ink-2); font-size: var(--fs-small); font-weight: 600; }}
        .zone-name {{ color: var(--ink); font-size: var(--fs-section); font-weight: 700; line-height: var(--lh-title); }}
        .zone-text {{ color: var(--ink); font-size: var(--fs-small); line-height: var(--lh-small); }}
        .zone-critique {{ border-color: var(--vin); }}
        .tier-legend {{ margin: .15rem 0 .6rem; color: var(--ink-2); font-size: var(--fs-label); line-height: var(--lh-small); }}
        .mode-card {{ flex: 1 1 0; min-width: 220px; background: #ffffff; border: 1px solid var(--line); border-left-width: 4px; border-radius: 10px; padding: .9rem 1rem; box-shadow: 0 1px 3px rgba(43, 37, 35, .08); }}
        .hint-note {{ color: var(--ink-2); font-size: var(--fs-label); font-weight: 400; }}
        .fiche-title, .carte-anchor {{ scroll-margin-top: 13rem; }}
        @media (max-width: 640px) {{ .fiche-title, .carte-anchor {{ scroll-margin-top: 1rem; }} }}
        .sibling-label {{ color: var(--ink-2); font-size: var(--fs-small); font-weight: 600; margin-bottom: -.3rem; }}
        .fiche-title {{ color: var(--ink); font-size: var(--fs-section); font-weight: 700; line-height: var(--lh-title); letter-spacing: -.01em; margin-top: 1.6rem; padding-top: 1rem; border-top: 2px solid var(--ink); }}
        .fiche-sub {{ color: var(--ink-2); font-size: var(--fs-small); line-height: var(--lh-small); margin: .3rem 0 .9rem; }}
        .wh-wrap {{ overflow-x: auto; margin: .2rem 0 .3rem; }}
        .wh-grid {{ border-collapse: separate; border-spacing: 2px; font-size: var(--fs-micro); }}
        .wh-grid th {{ color: var(--ink-2); font-weight: 600; padding: 0 3px; text-align: center; white-space: nowrap; }}
        .wh-grid td {{ min-width: 26px; height: 22px; text-align: center; border-radius: 3px; font-weight: 600; cursor: default; }}
        .wh-grid td.wh-empty {{ background: #FFFFFF; border: 1px dashed var(--line); }}
        .wh-grid td.wh-total {{ box-shadow: inset 0 0 0 1px rgba(43, 37, 35, .45); }}
        .watch-title {{ color: var(--ink); font-size: var(--fs-block); font-weight: 700; margin-top: .25rem; line-height: var(--lh-title); }}
        .stDataFrame {{ border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
        [data-testid="stSelectbox"] [role="group"] {{ border: 1px solid rgba(43, 37, 35, .45); background: #FFFFFF; box-shadow: 0 1px 2px rgba(43, 37, 35, .08); cursor: pointer; }}
        [data-testid="stSelectbox"] [role="group"]:hover {{ border-color: var(--ink); }}
        [data-testid="stSelectbox"] [role="group"]:focus-within {{ border-color: var(--action); box-shadow: 0 0 0 2px rgba(53, 30, 143, .25); }}
        [data-testid="stSelectbox"] [role="group"] input {{ cursor: pointer; }}
        [data-testid="stSelectbox"] [role="group"] button[aria-haspopup="listbox"] {{ color: var(--ink); }}
        [data-testid="stSelectbox"] [role="group"] button[aria-haspopup="listbox"] svg {{ width: 1.45rem; height: 1.45rem; }}
        [data-testid="stLayoutWrapper"]:has(> .st-key-ctx_bar) {{ position: sticky; top: 3.75rem; z-index: 60; background: #FFFFFF; box-shadow: 0 -3.75rem 0 #FFFFFF; }}
        @media (max-width: 640px) {{ [data-testid="stLayoutWrapper"]:has(> .st-key-ctx_bar) {{ position: static; box-shadow: none; }} }}
        .contention-note {{ margin: -.35rem 0 .5rem; }}
        .st-key-ctx_bar {{ background: #FFFFFF; padding: .55rem 0 .7rem; margin-bottom: .4rem; border-bottom: 2px solid var(--ink); }}
        .st-key-ctx_bar [data-testid="stColumn"] {{ position: relative; }}
        .st-key-ctx_bar [data-testid="stColumn"]:nth-child(3) {{ margin-left: .9rem; padding-left: .9rem; border-left: 1px solid var(--line); }}
        .st-key-ctx_back button {{ background: var(--action); color: #FFFFFF; border-color: var(--action); }}
        .st-key-ctx_back button p {{ color: #FFFFFF; font-size: var(--fs-small); font-weight: 700; }}
        .st-key-ctx_back button:hover {{ background: var(--ink); border-color: var(--ink); }}
        .st-key-ctx_back button:hover p {{ color: #FFFFFF; }}
        .st-key-ctx_back button:disabled {{ background: #FFFFFF; border-color: var(--line); }}
        .st-key-ctx_back button:disabled p {{ color: var(--ink-2); font-weight: 600; }}
        [data-testid="stPopoverButton"] {{ background: #ffffff; border: 1px solid var(--line); border-radius: 8px; font-size: var(--fs-small); font-weight: 500; color: var(--ink); padding: .4rem .8rem; white-space: nowrap; }}
        [data-testid="stPopoverButton"]:hover, [data-testid="stPopoverButton"][aria-expanded="true"] {{ background: var(--action); border-color: var(--action); color: #FFFFFF; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_exclusions(_conn, since_day: str, end_day: str) -> ind.Exclusions:
    """Jours incomplets et lignes au temps réel douteux de la période, écartés de toute analyse."""
    return ind.exclusions(_conn, since_day, end_day)


UNMEASURED_LABEL = "sans temps réel, non mesurable"


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_unmeasured_lines(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                          commune: str | None = None, stop_id: str | None = None) -> pd.DataFrame:
    """Lignes desservies sur la période (arrêts attendus) sans aucun passage mesuré : tous leurs trajets sont
    sans temps réel (section 7.4 de la documentation). Ni classées ni notées ; Flex' exclues."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    table, where, params = "agg_daily", "", []
    if commune or stop_id:
        table = "agg_daily_stop"
        if commune:
            where += " AND d.stop_id IN (SELECT stop_id FROM stop_municipalities WHERE commune_name = ?)"
            params.append(commune)
        if stop_id:
            where += " AND d.stop_id = ?"
            params.append(stop_id)
    try:
        return pd.read_sql_query(
            f"""SELECT d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type
                FROM {table} d JOIN routes r ON r.route_id = d.route_id
                WHERE d.date_service >= ? AND d.date_service < ? AND d.{ind.FLEX_FILTER} {where}
                GROUP BY d.route_id HAVING SUM(d.eligible) > 0 AND SUM(d.obs) = 0
                ORDER BY r.route_type, LENGTH(ligne), ligne""",
            _conn, params=[since_day, end_day, *params])
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame(columns=["route_id", "ligne", "route_type"])


def unmeasured_line_message(ligne: str) -> str:
    return (f"Ligne {ligne} : desservie sur la période, mais {UNMEASURED_LABEL}. Ses trajets n'ont pas de temps "
            "réel (tous leurs départs relevés valent exactement zéro) : elle n'est ni classée ni notée. "
            "Voir « Méthode et lexique ».")


def _excluded(conn, alias: str, since_day: str, end_day: str) -> tuple[str, list]:
    """Filtre SQL des agrégats : lignes Flex', jours et lignes écartés de la période (`load_exclusions`)."""
    return ind.exclusion_sql(alias, load_exclusions(conn, since_day, end_day))


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def _load_daily_core(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                     route_id: str | None = None, commune: str | None = None) -> pd.DataFrame:
    """Lignes des agrégats journaliers (réseau ou commune restreinte), avec les composantes du score.

    En mode réseau, lit agg_daily (une ligne par ligne et par jour). En mode
    commune, lit agg_daily_stop (une ligne par arrêt, ligne et jour) puis groupe
    au niveau ligne et jour, pour restreindre aux arrêts de la commune. Les jours
    et lignes écartés (`load_exclusions`) et les lignes Flex' sont exclus ; les
    composantes du score (`indicateurs.add_components`) sont calculées par ligne
    et par jour, avec les courses supprimées de agg_daily_trips.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "d", since_day, end_day)
    route_sql, route_params = (" AND d.route_id = ?", [route_id]) if route_id else ("", [])
    if commune is None:
        df = pd.read_sql_query(
            f"""
            SELECT d.date_service, d.route_id,
                   COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
                   d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60,
                   d.skipped, d.eligible, d.histogram, d.cnt_sans_tr, COALESCE(t.scheduled, 0) AS scheduled, COALESCE(t.cancelled, 0) AS cancelled, COALESCE(t.added, 0) AS added
            FROM agg_daily d LEFT JOIN routes r ON r.route_id = d.route_id
            LEFT JOIN agg_daily_trips t ON t.date_service = d.date_service AND t.route_id = d.route_id
            WHERE d.date_service >= ? AND d.date_service < ? {route_sql} {excl_sql}
            ORDER BY d.date_service
            """, _conn, params=(since_day, end_day, *route_params, *excl_params),
        )
    else:
        df = pd.read_sql_query(
            f"""
            SELECT d.date_service, d.route_id, d.stop_id,
                   COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
                   d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60,
                   d.skipped, d.eligible, d.histogram, d.cnt_sans_tr, COALESCE(t.scheduled, 0) AS scheduled, COALESCE(t.cancelled, 0) AS cancelled, COALESCE(t.added, 0) AS added
            FROM agg_daily_stop d
            JOIN routes r ON r.route_id = d.route_id
            JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
            LEFT JOIN agg_daily_trips t ON t.date_service = d.date_service AND t.route_id = d.route_id
            WHERE sm.commune_name = ? AND d.date_service >= ? AND d.date_service < ? {route_sql} {excl_sql}
            ORDER BY d.date_service
            """, _conn, params=(commune, since_day, end_day, *route_params, *excl_params),
        )
        if not df.empty:
            df = _group_daily_stop_to_route(df)
    df = ind.add_components(df)
    df["hist"] = df["histogram"].map(json.loads)
    df["date_service"] = pd.to_datetime(df["date_service"])
    return df


def _group_daily_stop_to_route(df: pd.DataFrame) -> pd.DataFrame:
    """Agrège les lignes de agg_daily_stop au niveau (date_service, route_id).

    Somme les compteurs et fusionne les histogrammes JSON arrêt par arrêt ; les
    courses (prévues, supprimées, ajoutées) sont celles de la ligne ce jour-là.
    """
    sums = {col: (col, "sum") for col in ("obs", "sum_delay", "cnt_le300", "cnt_gt300", "cnt_lt60", "skipped",
                                          "eligible", "cnt_sans_tr") if col in df}
    agg = (
        df.groupby(["date_service", "route_id", "ligne", "route_type"], sort=False)
        .agg(**sums, scheduled=("scheduled", "first"), cancelled=("cancelled", "first"), added=("added", "first"))
        .reset_index()
    )
    hists = {}
    for (ds, rid), sub in df.groupby(["date_service", "route_id"], sort=False):
        merged = Counter()
        for h in sub["histogram"]:
            if not h:
                continue
            for k, v in json.loads(h).items():
                merged[int(k)] += v
        hists[(ds, rid)] = json.dumps(dict(merged), sort_keys=True)
    agg["histogram"] = [hists[(ds, rid)] for ds, rid in zip(agg["date_service"], agg["route_id"])]
    return agg


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def _load_hourly_core(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                      route_id: str | None = None, commune: str | None = None) -> pd.DataFrame:
    """Lignes des agrégats horaires (réseau ou commune restreinte).

    En mode commune, lit agg_hourly_stop puis groupe au niveau ligne.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "h", since_day, end_day)
    route_sql, route_params = (" AND h.route_id = ?", [route_id]) if route_id else ("", [])
    if commune is None:
        df = pd.read_sql_query(
            f"""
            SELECT h.date_service, h.route_id, h.heure, r.route_type,
                   h.obs, h.sum_delay, h.cnt_le300, h.cnt_gt300, h.cnt_lt60
            FROM agg_hourly h LEFT JOIN routes r ON r.route_id = h.route_id
            WHERE h.date_service >= ? AND h.date_service < ? {route_sql} {excl_sql}
            ORDER BY h.heure
            """, _conn, params=(since_day, end_day, *route_params, *excl_params),
        )
    else:
        df = pd.read_sql_query(
            f"""
            SELECT h.date_service, h.route_id, h.heure, r.route_type,
                   h.obs, h.sum_delay, h.cnt_le300, h.cnt_gt300, h.cnt_lt60
            FROM agg_hourly_stop h
            JOIN routes r ON r.route_id = h.route_id
            JOIN stop_municipalities sm ON sm.stop_id = h.stop_id
            WHERE sm.commune_name = ? AND h.date_service >= ? AND h.date_service < ? {route_sql} {excl_sql}
            ORDER BY h.heure
            """, _conn, params=(commune, since_day, end_day, *route_params, *excl_params),
        )
        if not df.empty:
            df = (df.groupby(["date_service", "route_id", "heure", "route_type"], sort=False)
                  .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"), cnt_le300=("cnt_le300", "sum"),
                       cnt_gt300=("cnt_gt300", "sum"), cnt_lt60=("cnt_lt60", "sum"))
                  .reset_index())
    df["on_time"] = df["cnt_le300"] - df["cnt_lt60"]
    return df


def _daily_to_network(core: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Déduit du core journalier les deux tables du classement (scheduled/skipped)."""
    if core.empty:
        empty_s = core[["route_id", "ligne", "route_type"]].copy()
        for col in ("observations", "retard_moyen_s", "retard_median_s",
                    "pct_a_l_heure", "pct_retard_5min", "pct_avance_1min") + ind.COMPONENT_COLUMNS:
            empty_s[col] = np.nan
        empty_k = core[["route_id", "ligne", "route_type"]].copy()
        empty_k[["skipped", "eligible"]] = 0
        return empty_s, empty_k
    rows, hists = {}, {}
    for rid, sub in core.groupby("route_id", sort=False):
        obs = int(sub["obs"].sum())
        hists[rid] = list(sub["hist"])
        rows[rid] = {
            "route_id": rid,
            "ligne": sub["ligne"].iloc[0],
            "route_type": sub["route_type"].iloc[0],
            "observations": obs,
            "retard_moyen_s": sub["sum_delay"].sum() / max(obs, 1),
            "pct_a_l_heure": sub["on_time"].sum() / max(obs, 1) * 100,
            "pct_retard_5min": sub["cnt_gt300"].sum() / max(obs, 1) * 100,
            "pct_avance_1min": sub["cnt_lt60"].sum() / max(obs, 1) * 100,
            **{col: float(sub[col].sum()) for col in ind.COMPONENT_COLUMNS},
        }
    scheduled = pd.DataFrame(rows.values() if rows else rows)
    scheduled["retard_median_s"] = [_median_from_hists(hists[rid]) for rid in scheduled["route_id"]]
    skipped = (
        core.groupby(["route_id", "ligne", "route_type"], sort=False)
        .agg(skipped=("skipped", "sum"), eligible=("eligible", "sum"))
        .reset_index()
    )
    return scheduled, skipped


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_network_data(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                      commune: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Agrégats réseau par ligne, déduits des tables journalières précalculées.

    Le collecteur agrège la base brute en agg_daily (une ligne par ligne et par
    jour de service) : le dashboard ne lit donc plus que ~74 × N jours de lignes,
    quelles que soient les centaines de milliers d'observations brutes.
    """
    return _daily_to_network(_load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune))


def make_ranking(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    """Classement des lignes : score de fiabilité (`indicateurs.score_of`), service assuré, arrêts non desservis.

    Une ligne sans passage attendu sur la période (aucun score possible) n'est pas classée.
    """
    ranking = scheduled.merge(skipped, on=["route_id", "ligne", "route_type"], how="left").fillna({"skipped": 0, "eligible": 0})
    ranking["pct_arrets_sautes"] = np.where(ranking["eligible"] > 0, ranking["skipped"] / ranking["eligible"] * 100, 0)
    ranking["score_fiabilite"] = ind.score_of(ranking["assured_on_time"], ranking["expected"])
    ranking["pct_service"] = ind.score_of(ranking["assured"], ranking["expected"])
    ranking = ranking[ranking["score_fiabilite"].notna()].copy()
    ranking["mode"] = ranking["route_type"].map(MODE_LABELS).fillna("Autre")
    return ranking.sort_values(["score_fiabilite", "observations"], ascending=[True, False])


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_mode_stats(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                    commune: str | None = None) -> pd.DataFrame:
    """Statistiques par mode, déduites de agg_daily (médiane exacte par histogramme)."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return core
    rows, hists = {}, {}
    for rt, sub in core.groupby("route_type", sort=False):
        obs = int(sub["obs"].sum())
        hists[rt] = list(sub["hist"])
        rows[rt] = {
            "route_type": rt,
            "observations": obs,
            "retard_moyen_s": sub["sum_delay"].sum() / max(obs, 1),
            "pct_a_l_heure": sub["on_time"].sum() / max(obs, 1) * 100,
            "pct_retard_5min": sub["cnt_gt300"].sum() / max(obs, 1) * 100,
            "pct_avance_1min": sub["cnt_lt60"].sum() / max(obs, 1) * 100,
            "skipped": int(sub["skipped"].sum()),
            "eligible": int(sub["eligible"].sum()),
            "score_fiabilite": ind.score_of(sub["assured_on_time"].sum(), sub["expected"].sum()),
        }
    g = pd.DataFrame(rows.values() if rows else rows)
    g["retard_median_s"] = [_median_from_hists(hists[rt]) for rt in g["route_type"]]
    g["pct_arrets_sautes"] = np.where(g["eligible"] > 0, g["skipped"] / g["eligible"] * 100, 0)
    g["mode"] = g["route_type"].map(MODE_LABELS).fillna("Autre")
    return g.sort_values("observations", ascending=False)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_engagement_progression(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                                prev_since_ts: int | None, prev_end_ts: int | None,
                                commune: str | None = None) -> pd.DataFrame:
    """Tendance ligne par ligne : la période comparée à sa période de comparaison
    (le mois précédent pour un mois). Seules les lignes atteignant
    MIN_OBSERVATIONS sur les deux périodes sont retenues. Triée par évolution du
    score croissante (déclin d'abord).
    """
    empty = pd.DataFrame(columns=[
        "ligne", "route_id", "route_type", "mode", "observations", "observations_prev",
        "pct_a_l_heure", "pct_a_l_heure_prev", "pct_arrets_sautes", "score_fiabilite",
        "score_fiabilite_prev", "delta_score", "delta_pct_a_l_heure",
    ])
    if prev_since_ts is None or prev_end_ts is None:
        return empty
    current = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    previous = _load_daily_core(_conn, cutoff_ts, prev_since_ts, prev_end_ts, commune=commune)
    if current.empty or previous.empty:
        return empty
    prev_half = _daily_to_network(previous)
    recent = _daily_to_network(current)
    prev_rank = make_ranking(*prev_half)[["route_id", "observations", "pct_a_l_heure",
                                          "pct_arrets_sautes", "score_fiabilite"]]
    prev_rank = prev_rank.rename(columns={c: f"{c}_prev" for c in prev_rank.columns if c != "route_id"})
    recent_rank = make_ranking(*recent)[["route_id", "ligne", "route_type", "mode",
                                         "observations", "pct_a_l_heure", "pct_arrets_sautes",
                                         "score_fiabilite"]]
    out = recent_rank.merge(prev_rank, on="route_id", how="inner")
    out = out[(out["observations"] >= MIN_OBSERVATIONS)
              & (out["observations_prev"] >= MIN_OBSERVATIONS)].copy()
    if out.empty:
        return empty
    out["delta_score"] = out["score_fiabilite"] - out["score_fiabilite_prev"]
    out["delta_pct_a_l_heure"] = out["pct_a_l_heure"] - out["pct_a_l_heure_prev"]
    return out.sort_values("delta_score", ascending=True).reset_index(drop=True)


def _sort_periods(df: pd.DataFrame) -> pd.DataFrame:
    """Reclasse par l'ordre canonique PERIOD_ORDER, sans ajouter de colonne."""
    order = {p: i for i, p in enumerate(PERIOD_ORDER)}
    return df.assign(_ord=df["période"].map(order).fillna(len(PERIOD_ORDER))) \
             .sort_values("_ord", kind="stable").drop(columns="_ord")


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_period_stats(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                      commune: str | None = None) -> pd.DataFrame:
    """Fiabilité par créneau (Matin/Journée/Pointe du soir/Soirée & nuit/Week-end).

    Les arrêts non desservis et les courses supprimées n'ont pas d'heure : le score d'un
    créneau est sa part de passages à l'heure × le service assuré des jours qu'il couvre
    (semaine ou week-end, `period_day_rates`).
    """
    core = _load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return pd.DataFrame(columns=["période", "observations", "retard_moyen_s", "pct_a_l_heure",
                                     "pct_retard_5min", "pct_arrets_sautes", "pct_service", "score_fiabilite"])
    core = core.copy()
    core["période"] = _period_labels(core["date_service"], core["heure"])
    g = (core.groupby("période", sort=False)
         .agg(observations=("obs", "sum"), sum_delay=("sum_delay", "sum"),
              on_time=("on_time", "sum"), cnt_gt300=("cnt_gt300", "sum"))
         .reset_index())
    g["retard_moyen_s"] = g["sum_delay"] / g["observations"].replace(0, np.nan)
    g["pct_a_l_heure"] = g["on_time"] / g["observations"].replace(0, np.nan) * 100
    g["pct_retard_5min"] = g["cnt_gt300"] / g["observations"].replace(0, np.nan) * 100
    rates = period_day_rates(_load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune))
    day_type = ["week-end" if p == "Week-end" else "semaine" for p in g["période"]]
    g["pct_arrets_sautes"] = [rates[d]["skip"] for d in day_type]
    g["pct_service"] = [rates[d]["service"] for d in day_type]
    g["score_fiabilite"] = g["pct_a_l_heure"] * g["pct_service"] / 100
    return _sort_periods(g).reset_index(drop=True)[
        ["période", "observations", "retard_moyen_s", "pct_a_l_heure", "pct_retard_5min", "pct_arrets_sautes",
         "pct_service", "score_fiabilite"]]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_period_mode(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                     commune: str | None = None) -> pd.DataFrame:
    """Retards > 5 min par créneau et par mode, depuis les agrégats horaires."""
    core = _load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return pd.DataFrame(columns=["période", "route_type", "pct_retard_5min", "mode"])
    core = core.copy()
    core["période"] = _period_labels(core["date_service"], core["heure"])
    m = (core.groupby(["période", "route_type"], sort=False)
         .agg(observations=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index())
    m = m[m["observations"] > 0].copy()
    m["pct_retard_5min"] = m["cnt_gt300"] / m["observations"] * 100
    m = _sort_periods(m)[["période", "route_type", "pct_retard_5min"]]
    return _attach_mode(m)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_period_lines(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                      periode: str | None = None, commune: str | None = None) -> pd.DataFrame:
    """Classement des lignes pour un créneau, depuis les agrégats horaires."""
    empty = pd.DataFrame(columns=["ligne", "route_type", "mode", "observations", "pct_a_l_heure",
                                  "pct_retard_5min", "retard_moyen_s"])
    core = _load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return empty
    core = core.copy()
    core["période"] = _period_labels(core["date_service"], core["heure"])
    if periode is not None:
        core = core[core["période"] == periode]
    lines = (core.groupby(["route_id", "route_type"], sort=False)
             .agg(observations=("obs", "sum"), sum_delay=("sum_delay", "sum"),
                  on_time=("on_time", "sum"), cnt_gt300=("cnt_gt300", "sum"))
             .reset_index())
    lines = lines[lines["observations"] >= MIN_OBSERVATIONS]
    if lines.empty:
        return empty
    lines["retard_moyen_s"] = lines["sum_delay"] / lines["observations"].replace(0, np.nan)
    lines["pct_a_l_heure"] = lines["on_time"] / lines["observations"].replace(0, np.nan) * 100
    lines["pct_retard_5min"] = lines["cnt_gt300"] / lines["observations"].replace(0, np.nan) * 100
    rows = _conn.execute(
        "SELECT route_id, COALESCE(route_short_name, route_id) FROM routes"
    ).fetchall()
    ligne_map = {rid: nom for rid, nom in rows}
    lines["ligne"] = lines["route_id"].map(ligne_map).fillna(lines["route_id"])
    out = _attach_mode(lines)
    return out.sort_values(["pct_a_l_heure", "observations"], ascending=[True, False])[
        ["ligne", "route_type", "mode", "observations", "pct_a_l_heure", "pct_retard_5min",
         "retard_moyen_s"]]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_distribution(_conn, cutoff_ts: int, since_ts: int | None, route_id: str | None = None,
                      end_ts: int | None = None, commune: str | None = None) -> pd.DataFrame:
    """Distribution du retard par classes, depuis les histogrammes journaliers."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, route_id=route_id, commune=commune)
    if core.empty:
        return pd.DataFrame(columns=["observations", "plage"])
    return distribution_from_hists(core["hist"])


def distribution_from_hists(hists) -> pd.DataFrame:
    """Répartit des histogrammes {secondes: effectif} dans les 11 classes d'écart à l'horaire."""
    bucket_counts = Counter()
    for h in hists:
        for k, v in h.items():
            delay = int(k)
            if delay < -600:
                bucket_counts["neg600"] += v
            elif delay < -300:
                bucket_counts["neg300"] += v
            elif delay < -120:
                bucket_counts["neg120"] += v
            elif delay < -60:
                bucket_counts["neg60"] += v
            elif delay < 0:
                bucket_counts["neg0"] += v
            elif delay < 60:
                bucket_counts["pos0"] += v
            elif delay < 120:
                bucket_counts["pos60"] += v
            elif delay < 300:
                bucket_counts["pos120"] += v
            elif delay < 600:
                bucket_counts["pos300"] += v
            elif delay < 1200:
                bucket_counts["pos600"] += v
            else:
                bucket_counts["pos1200"] += v
    counts = pd.Series(bucket_counts, dtype="int64").reindex(DIST_BUCKETS, fill_value=0).reset_index()
    counts.columns = ["bucket", "observations"]
    return counts.assign(plage=DIST_LABELS).drop(columns="bucket")[["observations", "plage"]]


def _attach_mode(df: pd.DataFrame) -> pd.DataFrame:
    if not df.empty:
        df["mode"] = df["route_type"].map(MODE_LABELS).fillna("Autre")
    return df


MODE_ORDER = ["Tramway", "Bus", "Ferry"]
METRIC_SUMS = ("obs", "sum_delay", "on_time", "cnt_gt300", "skipped", "eligible") + ind.COMPONENT_COLUMNS[1:]


def group_metrics(core: pd.DataFrame, key: str | None) -> pd.DataFrame:
    """Indicateurs pour l'ensemble du périmètre (« Ensemble ») et pour chaque mode, par `key` (jour, heure…).

    Ponctualité, retards > 5 min et retard moyen ; score de fiabilité et arrêts non desservis quand
    les composantes du score sont présentes (agrégats journaliers seulement).
    """
    keys = ([key] if key else []) + ["groupe", "route_type"]
    columns = keys + ["observations", "pct_a_l_heure", "pct_retard_5min", "retard_moyen_s"]
    if core.empty:
        return pd.DataFrame(columns=columns)
    sums = [col for col in METRIC_SUMS if col in core.columns]
    frames = [core.assign(groupe=GROUP_ALL, route_type=-1)]
    for rt, sub in core.dropna(subset=["route_type"]).groupby("route_type"):
        frames.append(sub.assign(groupe=MODE_LABELS.get(int(rt), "Autre"), route_type=int(rt)))
    g = pd.concat(frames, ignore_index=True).groupby(keys, sort=True)[sums].sum().reset_index()
    obs = g["obs"].replace(0, np.nan)
    g["observations"] = g["obs"]
    g["pct_a_l_heure"] = g["on_time"] / obs * 100
    g["pct_retard_5min"] = g["cnt_gt300"] / obs * 100
    g["retard_moyen_s"] = g["sum_delay"] / obs
    if "expected" in g.columns:
        g["pct_arrets_sautes"] = np.where(g["eligible"] > 0, g["skipped"] / g["eligible"].replace(0, np.nan) * 100, 0.0)
        g["score_fiabilite"] = ind.score_of(g["assured_on_time"], g["expected"])
    return g


def period_totals(metrics: pd.DataFrame) -> float:
    """Score de fiabilité de l'ensemble sur toute la période (sortie de `group_metrics(core, None)`)."""
    row = metrics[metrics["groupe"] == GROUP_ALL]
    return float(row["score_fiabilite"].iloc[0]) if not row.empty else 0.0


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_daily_metrics(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                       commune: str | None = None, by_day: bool = True) -> pd.DataFrame:
    """Indicateurs par jour (ou sur toute la période) pour l'ensemble et chaque mode, depuis agg_daily."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    return group_metrics(core, "date_service" if by_day else None)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_hourly_metrics(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                        commune: str | None = None) -> pd.DataFrame:
    """Retards par heure pour l'ensemble et chaque mode, depuis agg_hourly."""
    return group_metrics(_load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune), "heure")


def period_day_rates(daily_core: pd.DataFrame) -> dict:
    """Service assuré et arrêts non desservis (en %) des jours de semaine et du week-end.

    `service` : passages assurés / passages attendus (composantes du score) ; `skip` :
    arrêts non desservis / arrêts attendus. Sans passage attendu : 100 % et 0 %.
    """
    out = {name: {"service": 100.0, "skip": 0.0} for name in ("semaine", "week-end")}
    if daily_core.empty:
        return out
    weekend = pd.to_datetime(daily_core["date_service"]).dt.weekday >= 5
    for name, mask in (("semaine", ~weekend), ("week-end", weekend)):
        sub = daily_core.loc[mask]
        expected, eligible = float(sub["expected"].sum()), float(sub["eligible"].sum())
        if expected > 0:
            out[name]["service"] = float(sub["assured"].sum()) / expected * 100
        if eligible > 0:
            out[name]["skip"] = float(sub["skipped"].sum()) / eligible * 100
    return out


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_active_alerts(_conn, now_ts: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT a.route_id, COALESCE(r.route_short_name, a.route_id) AS ligne,
               a.header_text, a.description_text, a.cause,
               datetime(a.active_period_start, 'unixepoch', 'localtime') AS debut,
               datetime(a.active_period_end, 'unixepoch', 'localtime') AS fin
        FROM service_alerts a LEFT JOIN routes r ON a.route_id = r.route_id
        WHERE a.active_period_start <= ? AND a.active_period_end >= ?
        ORDER BY a.active_period_end DESC
        """, _conn, params=(now_ts, now_ts),
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_collection_stats(_conn) -> dict:
    first = _conn.execute("SELECT MIN(last_seen_at) FROM observations").fetchone()[0]
    last = _conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    total = _conn.execute("SELECT MAX(rowid) FROM observations").fetchone()[0] or 0
    n_trajets = _conn.execute("SELECT COUNT(*) FROM trip_status").fetchone()[0]
    n_lignes, analysed = _conn.execute("SELECT COUNT(DISTINCT route_id), COALESCE(SUM(obs), 0) FROM agg_daily").fetchone()
    hourly = pd.read_sql_query(
        """
        SELECT CAST(strftime('%H', datetime(last_seen_at, 'unixepoch', 'localtime')) AS INTEGER) AS heure,
               COUNT(*) AS observations
        FROM observations
        WHERE last_seen_at >= ?
        GROUP BY heure ORDER BY heure
        """, _conn, params=(int(last or 0) - COLLECTION_HOURLY_DAYS * 86400,),
    )
    return {
        "hourly": hourly,
        "first_ts": first, "last_ts": last,
        "total": total, "trajets": n_trajets, "lignes": n_lignes, "analysed": analysed,
    }


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_collection_minutely(_conn, start_ts, end_ts):
    df = pd.read_sql_query(
        """
        SELECT datetime(
            CAST(last_seen_at / 60 AS INTEGER) * 60,
            'unixepoch', 'localtime'
        ) AS minute,
        COUNT(*) AS observations
        FROM observations
        WHERE last_seen_at >= ? AND last_seen_at < ?
        GROUP BY minute ORDER BY minute
        """,
        _conn, params=(start_ts, end_ts),
    )
    if df.empty:
        return df
    df["minute"] = pd.to_datetime(df["minute"])
    debut = datetime.fromtimestamp(start_ts).replace(second=0, microsecond=0)
    fin = datetime.fromtimestamp(end_ts).replace(second=0, microsecond=0)
    idx = pd.date_range(start=debut, end=fin, freq="min")
    df = df.set_index("minute").reindex(idx).fillna(0).rename_axis("minute").reset_index()
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_communes(_conn) -> list[str]:
    rows = _conn.execute(
        "SELECT DISTINCT commune_name FROM stop_municipalities ORDER BY commune_name"
    ).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_open_dataset(_conn, name: str, cutoff_ts: int, since_ts: int | None,
                      end_ts: int | None = None) -> pd.DataFrame:
    """Dataset open data (agrégats) pour la période, via src/scripts/export_open_data."""
    src_dir = Path(__file__).resolve().parents[1] / "src" / "scripts"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import export_open_data as _open_data
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    return pd.DataFrame(_open_data.dataset_rows(_conn, name, since=since_day, end=end_day))


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_method_v2(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                   commune: str | None = None, route_id: str | None = None) -> dict:
    """Score de fiabilité de la période avec sa marge, service assuré, départs en avance (src/scripts/indicateurs)."""
    src_dir = Path(__file__).resolve().parents[1] / "src" / "scripts"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import indicateurs as _ind
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    communes = [commune] if commune else None
    routes = [route_id] if route_id else None
    result = _ind.indicators(_conn, since_day, end_day, routes=routes, communes=communes)
    flags = _ind.quality_flags(_conn, since_day, end_day)
    regularity = _ind.route_regularity(_conn, since_day, end_day, communes=communes, routes=routes)
    return {
        **asdict(result),
        "disponible": result.disponible,
        "jours_degrades": sorted(d for d, flag in flags.items() if flag == "degrade"),
        "attente_excedentaire": regularity[route_id]["attente_excedentaire"] if route_id in regularity else None,
    }


def _short_days(days: list[str]) -> str:
    return ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m") for d in days) or "aucun"


def _with_margin(value: float, margin: float | None) -> str:
    return f"{format_number(value, 1)}" if margin is None else f"{format_number(value, 1)} ± {format_number(margin, 1)}"


def method_v2_caption(v2: dict | None, route_id: str | None = None) -> str | None:
    """Phrase de la fiche ligne : marge du score, service assuré, départs en avance, attente excédentaire."""
    if not v2 or not v2["disponible"]:
        return None
    parts = [f"score {_with_margin(v2['score'], v2['marge'])} / 100 (marge à 95 %)",
             f"service assuré {format_number(v2['service'], 1)} %",
             f"départs de plus d'1 min en avance {format_number(v2['avance'], 1)} %"]
    if v2["attente_excedentaire"] is not None:
        parts.append(f"attente excédentaire {format_seconds(v2['attente_excedentaire'], signed=True)}")
    return "Détail du score : " + ", ".join(parts) + "."


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_commune_stats(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None) -> pd.DataFrame:
    """Fiabilité par commune, agrégée depuis agg_daily_stop (jamais la table brute)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "d", since_day, end_day)
    rows = pd.read_sql_query(
        f"""
        SELECT sm.commune_name AS commune, d.date_service, d.route_id,
               SUM(d.obs) AS obs, SUM(d.sum_delay) AS sum_delay,
               SUM(d.cnt_le300) AS cnt_le300, SUM(d.cnt_gt300) AS cnt_gt300, SUM(d.cnt_lt60) AS cnt_lt60,
               SUM(d.skipped) AS skipped, SUM(d.eligible) AS eligible, SUM(d.cnt_sans_tr) AS cnt_sans_tr,
               COALESCE(t.scheduled, 0) AS scheduled, COALESCE(t.cancelled, 0) AS cancelled, COALESCE(t.added, 0) AS added
        FROM agg_daily_stop d
        JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
        LEFT JOIN agg_daily_trips t ON t.date_service = d.date_service AND t.route_id = d.route_id
        WHERE d.date_service >= ? AND d.date_service < ? {excl_sql}
        GROUP BY sm.commune_name, d.date_service, d.route_id
        """, _conn, params=(since_day, end_day, *excl_params),
    )
    empty = pd.DataFrame(columns=[
        "commune", "observations", "retard_moyen_s", "pct_a_l_heure",
        "pct_retard_5min", "pct_arrets_sautes", "score_fiabilite", "n_lignes",
    ])
    if rows.empty:
        return empty
    rows = ind.add_components(rows)
    df = (rows.groupby("commune", sort=False)
          .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"), on_time=("on_time", "sum"),
               cnt_gt300=("cnt_gt300", "sum"), skipped=("skipped", "sum"), eligible=("eligible", "sum"),
               expected=("expected", "sum"), assured_on_time=("assured_on_time", "sum"),
               n_lignes=("route_id", "nunique"))
          .reset_index())
    df = df[df["expected"] > 0].copy()
    df["observations"] = df["obs"]
    df["retard_moyen_s"] = df["sum_delay"] / df["obs"].replace(0, np.nan)
    df["pct_a_l_heure"] = df["on_time"] / df["obs"].replace(0, np.nan) * 100
    df["pct_retard_5min"] = df["cnt_gt300"] / df["obs"].replace(0, np.nan) * 100
    df["pct_arrets_sautes"] = np.where(df["eligible"] > 0, df["skipped"] / df["eligible"] * 100, 0)
    df["score_fiabilite"] = ind.score_of(df["assured_on_time"], df["expected"])
    return df[
        ["commune", "observations", "retard_moyen_s", "pct_a_l_heure",
         "pct_retard_5min", "pct_arrets_sautes", "score_fiabilite", "n_lignes"]
    ].sort_values("score_fiabilite", ascending=True).reset_index(drop=True)


GROUP_RADIUS_M = 150.0


def group_stops(df: pd.DataFrame) -> pd.DataFrame:
    """Regroupe les quais d'un même arrêt : même nom, même mode, à moins de GROUP_RADIUS_M.

    Les deux sens d'un arrêt ont presque la même position : sur la carte, l'un
    masquerait l'autre. Une ligne par groupe, portée par le sens le moins fiable
    (couleur du marqueur, fiche ouverte au clic) : position moyenne, passages
    cumulés, part des passages > 5 min de l'ensemble, lignes réunies, `n_sens`,
    `members` (stop_id du groupe) et `detail` (score de chaque sens, infobulle).
    """
    if df.empty:
        return df.assign(n_sens=pd.Series(dtype=int), members=pd.Series(dtype=object),
                         detail=pd.Series(dtype=str))
    d = df.reset_index(drop=True)
    lat, lon = d["lat"].to_numpy(dtype=float), d["lon"].to_numpy(dtype=float)
    coslat = math.cos(math.radians(float(lat.mean())))
    parent = np.arange(len(d))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    modes = [mode_marker(rt) for rt in d["route_type"]]
    for idx in d.groupby([d["stop_name"], pd.Series(modes)]).indices.values():
        if len(idx) < 2:
            continue
        for a_pos, a in enumerate(idx):
            for b in idx[a_pos + 1:]:
                dist = math.hypot((lat[a] - lat[b]) * 111_320, (lon[a] - lon[b]) * 111_320 * coslat)
                if dist <= GROUP_RADIUS_M:
                    parent[find(a)] = find(b)
    d["_group"] = [find(i) for i in range(len(d))]
    d["_late"] = d["observations"] * d["pct_retard_5min"] / 100
    d["_detail"] = [f"{html.escape(str(direction) or 'sens unique')} : {score:.0f}/100 · {pct:.0f} % &gt; 5 min"
                    for direction, score, pct in zip(d["direction"], d["score_fiabilite"], d["pct_retard_5min"])]
    d = d.sort_values(["_group", "score_fiabilite"], kind="stable")
    grouped = d.groupby("_group", sort=False)
    out = grouped.head(1).set_index("_group")
    agg = grouped.agg(lat=("lat", "mean"), lon=("lon", "mean"), observations=("observations", "sum"),
                      _late=("_late", "sum"), n_sens=("stop_id", "size"), members=("stop_id", list),
                      detail=("_detail", "<br/>".join), lignes=("lignes", lambda v: ", ".join(
                          sorted({x for item in v for x in str(item).split(", ") if x}))))
    out = out.drop(columns=["lat", "lon", "observations", "lignes", "_late", "_detail"]).join(agg)
    out["pct_retard_5min"] = (out["_late"] / out["observations"].where(out["observations"] > 0) * 100).round(1).fillna(0.0)
    return out.drop(columns="_late").reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def grouped_territorial(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                        commune: str | None = None) -> pd.DataFrame:
    """Arrêts du périmètre regroupés par quai (`group_stops`), pour la carte et « À surveiller »."""
    return group_stops(load_territorial(_conn, cutoff_ts, since_ts, end_ts, commune=commune))


def week_hour_grid_html(table: pd.DataFrame, peak: dict | None = None) -> str:
    """Grille jour de la semaine × heure : part des passages > 5 min, couleur du palier « pourcent ».

    Dernière colonne : toute la journée ; dernière ligne : tous les jours. Case
    vide : moins de MIN_CELL_PASSAGES passages. La case du moment qui ressort
    (`peak`) est encadrée. Le détail de chaque case est dans son infobulle.
    """
    if table is None or table.empty:
        return ""
    cells = {(int(r.weekday), int(r.heure)): r for r in table.itertuples()}
    hours = list(range(int(table["heure"].min()), int(table["heure"].max()) + 1))
    peak_cell = (peak["weekday"], peak["hour"]) if peak and "weekday" in peak else None

    def td(obs: float, cnt: float, title: str, css: str = "", outline: bool = False) -> str:
        if obs < dg.MIN_CELL_PASSAGES:
            return f'<td class="wh-empty {css}" title="{html.escape(title)}"></td>'
        pct = cnt / obs * 100
        color = screen_hex(pct, "pourcent")
        fg = ink_on(color)
        ring = f";outline:2px solid {INK};outline-offset:1px" if outline else ""
        return (f'<td class="{css}" style="background:{color};color:{fg}{ring}" '
                f'title="{html.escape(title)}">{pct:.0f}</td>')

    header = "<tr><th></th>" + "".join(f"<th>{h} h</th>" for h in hours) + "<th>Jour</th></tr>"
    body = []
    for d in range(7):
        tds, day_obs, day_cnt = [], 0.0, 0.0
        for h in hours:
            r = cells.get((d, h))
            if r is None:
                tds.append('<td class="wh-empty"></td>')
                continue
            day_obs += r.obs
            day_cnt += r.cnt_gt300
            title = (f"{dg.slot_label(d, h)} : {r.pct_gt300:.0f} % de passages à plus de 5 min · "
                     f"{int(r.obs)} passages · dégradé {int(r.bad_days)} fois sur {int(r.days)}")
            tds.append(td(r.obs, r.cnt_gt300, title, outline=peak_cell == (d, h)))
        day_title = f"{dg.WEEKDAYS[d]}, toute la journée : {int(day_obs)} passages"
        tds.append(td(day_obs, day_cnt, day_title, "wh-total"))
        body.append(f"<tr><th>{dg.WEEKDAYS[d][:3]}.</th>{''.join(tds)}</tr>")
    by_hour = table.groupby("heure").agg(obs=("obs", "sum"), cnt=("cnt_gt300", "sum"))
    tds = [td(float(by_hour.loc[h, "obs"]), float(by_hour.loc[h, "cnt"]), f"Tous les jours, {h} h : "
              f"{int(by_hour.loc[h, 'obs'])} passages", "wh-total") if h in by_hour.index
           else '<td class="wh-empty wh-total"></td>' for h in hours]
    tds.append(td(float(table["obs"].sum()), float(table["cnt_gt300"].sum()), "Toute la semaine", "wh-total"))
    body.append(f"<tr><th>Tous</th>{''.join(tds)}</tr>")
    return f'<div class="wh-wrap"><table class="wh-grid">{header}{"".join(body)}</table></div>'


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_territorial(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                     commune: str | None = None) -> pd.DataFrame:
    """Arrêts du périmètre avec leur ligne principale et leur score de fiabilité.

    Agrège depuis agg_daily_stop (jamais la table brute). Le score de l'arrêt
    porte sur toutes ses lignes (`indicateurs.score_of` sur les composantes de
    chaque ligne et de chaque jour à cet arrêt, comme le classement des lignes) ;
    la ligne la plus fréquentée devient la ligne « principale », dont le mode
    donne la forme du marqueur. Un arrêt sans passage attendu n'est pas retenu.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "d", since_day, end_day)
    commune_join, commune_sql, commune_params = (
        ("JOIN stop_municipalities sm ON sm.stop_id = d.stop_id", " AND sm.commune_name = ?", [commune])
        if commune is not None else ("", "", []))
    df = pd.read_sql_query(
        f"""
        SELECT d.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
               d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
               d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60, d.obs, d.skipped, d.eligible, d.cnt_sans_tr,
               COALESCE(t.scheduled, 0) AS scheduled, COALESCE(t.cancelled, 0) AS cancelled, COALESCE(t.added, 0) AS added
        FROM agg_daily_stop d
        JOIN stops s ON s.stop_id = d.stop_id
        JOIN routes r ON r.route_id = d.route_id
        {commune_join}
        LEFT JOIN agg_daily_trips t ON t.date_service = d.date_service AND t.route_id = d.route_id
        WHERE d.date_service >= ? AND d.date_service < ? {commune_sql} {excl_sql}
        """, _conn, params=(since_day, end_day, *commune_params, *excl_params),
    )
    columns = ["stop_id", "stop_name", "direction", "lat", "lon", "route_id", "ligne", "route_type",
               "pct_a_l_heure", "pct_retard_5min", "pct_arrets_sautes", "observations",
               "score_fiabilite", "lignes", "attendus", "rates"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    df = ind.add_components(df)
    sums = ["obs", "on_time", "cnt_gt300", "skipped", "eligible", "expected", "assured_on_time"]
    per_line = (df.groupby(["stop_id", "route_id"], sort=False)
                .agg(**{c: (c, "sum") for c in sums}, stop_name=("stop_name", "first"),
                     stop_lat=("stop_lat", "first"), stop_lon=("stop_lon", "first"),
                     ligne=("ligne", "first"), route_type=("route_type", "first"))
                .reset_index())
    per_stop = per_line.groupby("stop_id", sort=False).agg(
        **{c: (c, "sum") for c in sums},
        lignes=("ligne", lambda v: ", ".join(sorted(set(v.astype(str))))))
    main = (per_line.sort_values("obs", ascending=False).drop_duplicates("stop_id", keep="first")
            .set_index("stop_id")[["stop_name", "stop_lat", "stop_lon", "route_id", "ligne", "route_type"]])
    out = per_stop.join(main).reset_index()
    out = out[out["expected"] > 0]
    directions = load_stop_directions(_conn)
    obs = out["obs"].where(out["obs"] > 0)
    return pd.DataFrame({
        "stop_id": out["stop_id"],
        "stop_name": out["stop_name"],
        "direction": [directions.get((rid, sid), "") for rid, sid in zip(out["route_id"], out["stop_id"])],
        "lat": out["stop_lat"].astype(float),
        "lon": out["stop_lon"].astype(float),
        "route_id": out["route_id"],
        "ligne": out["ligne"],
        "route_type": out["route_type"],
        "pct_a_l_heure": (out["on_time"] / obs * 100).fillna(0.0).round(1),
        "pct_retard_5min": (out["cnt_gt300"] / obs * 100).fillna(0.0).round(1),
        "pct_arrets_sautes": (out["skipped"] / out["eligible"].where(out["eligible"] > 0) * 100).fillna(0.0).round(2),
        "observations": out["obs"].astype(int),
        "score_fiabilite": ind.score_of(out["assured_on_time"], out["expected"]).round(1),
        "lignes": out["lignes"],
        "attendus": out["expected"].astype(float),
        "rates": (out["expected"] - out["assured_on_time"]).astype(float),
    }, columns=columns).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_stop_directions(_conn) -> dict[tuple[str, str], str]:
    """Étiquette de direction « vers <terminus> » par (route_id, stop_id).

    Issu de la table stop_direction, calculée en backfill par le collecteur
    (jamais depuis la table brute au moment du rendu).
    """
    rows = _conn.execute(
        "SELECT route_id, stop_id, terminus FROM stop_direction WHERE terminus IS NOT NULL"
    ).fetchall()
    return {(route, stop): f"vers {terminus}" for route, stop, terminus in rows}


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_perturbation_history(_conn, since_ts: int | None, end_ts: int | None,
                              cutoff_ts: int, commune: str | None = None) -> pd.DataFrame:
    """Perturbations actives à un moment ou un autre dans la période.

    Couvre les champs fiables du flux ServiceAlerts (header, description,
    route_id, période d'activité). `cause`, quasi toujours UNKNOWN_CAUSE,
    est ignoré. La période effective de chaque perturbation est tronquée à
    l'intersection avec la période sélectionnée.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    start_ts = 0 if since_ts is None else int(datetime.strptime(since_day, "%Y-%m-%d").timestamp())
    end_ts_int = int(datetime.strptime(end_day, "%Y-%m-%d").timestamp())
    route_filter = ""
    params: list = [end_ts_int, end_ts_int, start_ts]
    if commune is not None:
        route_filter = """
            AND a.route_id IN (
                SELECT DISTINCT d.route_id FROM agg_daily_stop d
                JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
                WHERE sm.commune_name = ?
            )
        """
        params.append(commune)
    params.append(end_ts_int)
    rows = _conn.execute(
        f"""
        SELECT a.route_id,
               COALESCE(r.route_short_name, a.route_id) AS ligne,
               a.header_text, a.description_text,
               a.active_period_start, a.active_period_end
        FROM service_alerts a
        LEFT JOIN routes r ON r.route_id = a.route_id
        WHERE a.active_period_start < ? AND COALESCE(a.active_period_end, ?) > ?
        {route_filter}
        ORDER BY COALESCE(a.active_period_end, ?) DESC
        """, params,
    ).fetchall()
    results = []
    seen = set()
    for route_id, ligne, header, desc, p_start, p_end in rows:
        p_end = p_end if p_end is not None else end_ts_int
        eff_start = max(p_start, start_ts)
        eff_end = min(p_end, end_ts_int)
        if eff_end <= eff_start:
            continue
        # Déduplication par contenu : la même annonce peut être publiée sous
        # plusieurs alert_id par le flux ServiceAlerts.
        dedup_key = (route_id,
                     header,
                     datetime.fromtimestamp(eff_start).strftime("%d/%m/%Y"),
                     datetime.fromtimestamp(eff_end).strftime("%d/%m/%Y"))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        jours_couverts = int(math.ceil((eff_end - eff_start) / 86400.0))
        results.append({
            "ligne": ligne,
            "route_id": route_id,
            "header_text": header,
            "description_text": desc,
            "debut_effectif": datetime.fromtimestamp(eff_start).strftime("%d/%m/%Y %H:%M"),
            "fin_effective": datetime.fromtimestamp(eff_end).strftime("%d/%m/%Y %H:%M"),
            "jours_couverts": jours_couverts,
        })
    results.sort(key=lambda x: x["jours_couverts"], reverse=True)
    return pd.DataFrame(results)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_commune_routes(_conn, commune: str) -> set[str]:
    """Identifiants de lignes desservant au moins un arrêt de la commune.

    Issu de la table agrégée par arrêt (jamais de scan de la table brute).
    """
    rows = _conn.execute(
        """
        SELECT DISTINCT d.route_id FROM agg_daily_stop d
        JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
        WHERE sm.commune_name = ?
        """, (commune,),
    ).fetchall()
    return {r[0] for r in rows}


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_disturbed_route_ids(_conn, since_ts: int | None, end_ts: int | None,
                             cutoff_ts: int, commune: str | None = None) -> set[str]:
    """Identifiants de lignes ayant eu une perturbation active dans la période.

    Sert à marquer (⚠) les lignes concernées dans les classements et l'analyse
    d'une ligne, sans jamais toucher à la table brute des observations.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    start_ts = 0 if since_ts is None else int(datetime.strptime(since_day, "%Y-%m-%d").timestamp())
    end_ts_int = int(datetime.strptime(end_day, "%Y-%m-%d").timestamp())
    route_filter = ""
    params: list = [end_ts_int, end_ts_int, start_ts]
    if commune is not None:
        route_filter = """
            AND a.route_id IN (
                SELECT DISTINCT d.route_id FROM agg_daily_stop d
                JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
                WHERE sm.commune_name = ?
            )
        """
        params.append(commune)
    rows = _conn.execute(
        f"""
        SELECT DISTINCT a.route_id
        FROM service_alerts a
        WHERE a.route_id <> '' AND a.active_period_start < ?
              AND COALESCE(a.active_period_end, ?) > ?
        {route_filter}
        """, params,
    ).fetchall()
    return {r[0] for r in rows}


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_stop_daily(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                    stop_id: str) -> pd.DataFrame:
    """Compteurs quotidiens d'un arrêt, par ligne, avec les composantes du score (agg_daily_stop, index sur stop_id)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "d", since_day, end_day)
    df = pd.read_sql_query(
        f"""
        SELECT d.date_service, d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne,
               r.route_type, d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60, d.skipped,
               d.eligible, d.histogram, d.cnt_sans_tr, COALESCE(t.scheduled, 0) AS scheduled, COALESCE(t.cancelled, 0) AS cancelled, COALESCE(t.added, 0) AS added
        FROM agg_daily_stop d LEFT JOIN routes r ON r.route_id = d.route_id
        LEFT JOIN agg_daily_trips t ON t.date_service = d.date_service AND t.route_id = d.route_id
        WHERE d.stop_id = ? AND d.date_service >= ? AND d.date_service < ? {excl_sql}
        ORDER BY d.date_service
        """, _conn, params=(stop_id, since_day, end_day, *excl_params),
    )
    df = ind.add_components(df)
    df["hist"] = df["histogram"].map(json.loads)
    df["date_service"] = pd.to_datetime(df["date_service"])
    return df.drop(columns="histogram")


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_stop_hourly(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                     stop_id: str) -> pd.DataFrame:
    """Compteurs horaires d'un arrêt, toutes lignes (agg_hourly_stop, index sur stop_id)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "h", since_day, end_day)
    return pd.read_sql_query(
        f"""
        SELECT h.date_service, h.route_id, h.heure, h.obs, h.cnt_gt300
        FROM agg_hourly_stop h
        WHERE h.stop_id = ? AND h.date_service >= ? AND h.date_service < ? {excl_sql}
        """, _conn, params=(stop_id, since_day, end_day, *excl_params),
    )


def stop_lines_table(daily: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par ligne de transport desservant l'arrêt, avec score et médiane exacte."""
    cols = ["route_id", "ligne", "route_type", "mode", "observations", "cnt_gt300", "skipped",
            "eligible", "pct_a_l_heure", "pct_retard_5min", "pct_arrets_sautes",
            "retard_moyen_s", "retard_median_s", "score_fiabilite"]
    if daily.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for rid, sub in daily.groupby("route_id", sort=False):
        obs = int(sub["obs"].sum())
        eligible = int(sub["eligible"].sum())
        pct_ok = sub["on_time"].sum() / obs * 100 if obs else 0.0
        pct_skip = sub["skipped"].sum() / eligible * 100 if eligible else 0.0
        score = ind.score_of(sub["assured_on_time"].sum(), sub["expected"].sum())
        if pd.isna(score):
            continue
        rows.append({
            "route_id": rid, "ligne": sub["ligne"].iloc[0], "route_type": sub["route_type"].iloc[0],
            "mode": MODE_LABELS.get(sub["route_type"].iloc[0], "Autre"),
            "observations": obs, "cnt_gt300": int(sub["cnt_gt300"].sum()),
            "skipped": int(sub["skipped"].sum()), "eligible": eligible,
            "pct_a_l_heure": pct_ok,
            "pct_retard_5min": sub["cnt_gt300"].sum() / obs * 100 if obs else 0.0,
            "pct_arrets_sautes": pct_skip,
            "retard_moyen_s": sub["sum_delay"].sum() / obs if obs else None,
            "retard_median_s": _median_from_hists(list(sub["hist"])),
            "score_fiabilite": score,
        })
    return pd.DataFrame(rows, columns=cols).sort_values("cnt_gt300", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def segments_available(_conn) -> bool:
    """Vrai si agg_daily_segment contient des données (rattrapage du collecteur effectué)."""
    try:
        return _conn.execute("SELECT 1 FROM agg_daily_segment LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


MIN_SERVED_SHARE = 0.05


def keep_served_stops(df: pd.DataFrame, share: float = MIN_SERVED_SHARE) -> pd.DataFrame:
    """Écarte d'un profil les arrêts rarement desservis dans leur direction.

    Une variante de course marginale (quelques passages sur la période) ajoute
    des arrêts qui s'intercalent dans l'ordre de la ligne et fausseraient le
    profil et le terminus : un arrêt est gardé s'il compte au moins `share` des
    arrêts attendus (colonne eligible) de l'arrêt le plus desservi de sa direction.
    """
    if df.empty:
        return df
    top = df.groupby("direction_id")["eligible"].transform("max")
    return df[df["eligible"] >= share * top]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_route_segments(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                        route_id: str) -> pd.DataFrame:
    """Profil d'une ligne arrêt par arrêt et par direction (agg_daily_segment).

    Les arrêts rarement desservis dans une direction sont écartés
    (`keep_served_stops`). Colonnes : direction_id, stop_id, stop_name, commune,
    order (rang moyen de l'arrêt), delay_s (retard moyen à l'arrêt), carried_s
    (retard déjà présent
    en arrivant), gain_s (retard pris sur le tronçon), prev_stop_id,
    prev_stop_name, pairs, obs, eligible, skipped, cnt_gain_gt120, terminus.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "g", since_day, end_day)
    df = pd.read_sql_query(
        f"""
        SELECT g.direction_id, g.stop_id, s.stop_name, sm.commune_name AS commune,
               SUM(g.eligible) eligible, SUM(g.skipped) skipped, SUM(g.sum_seq) sum_seq,
               SUM(g.obs) obs, SUM(g.sum_delay) sum_delay, SUM(g.pairs) pairs,
               SUM(g.sum_prev_delay) sum_prev_delay, SUM(g.sum_gain) sum_gain,
               SUM(g.cnt_gain_gt120) cnt_gain_gt120
        FROM agg_daily_segment g
        LEFT JOIN stops s ON s.stop_id = g.stop_id
        LEFT JOIN stop_municipalities sm ON sm.stop_id = g.stop_id
        WHERE g.route_id = ? AND g.date_service >= ? AND g.date_service < ? {excl_sql}
        GROUP BY g.direction_id, g.stop_id
        """, _conn, params=(route_id, since_day, end_day, *excl_params),
    )
    if df.empty:
        return df
    prev = pd.read_sql_query(
        f"""
        SELECT g.direction_id, g.stop_id, g.prev_stop_id, SUM(g.pairs) n
        FROM agg_daily_segment g
        WHERE g.route_id = ? AND g.date_service >= ? AND g.date_service < ? AND g.prev_stop_id IS NOT NULL {excl_sql}
        GROUP BY g.direction_id, g.stop_id, g.prev_stop_id
        """, _conn, params=(route_id, since_day, end_day, *excl_params),
    )
    prev = prev.sort_values(["n", "prev_stop_id"], ascending=[False, True]).drop_duplicates(["direction_id", "stop_id"])
    df = df.merge(prev[["direction_id", "stop_id", "prev_stop_id"]], on=["direction_id", "stop_id"], how="left")
    names = dict(zip(df["stop_id"], df["stop_name"]))
    df["prev_stop_name"] = df["prev_stop_id"].map(names)
    df = keep_served_stops(df[df["eligible"] > 0]).copy()
    df["order"] = df["sum_seq"] / df["eligible"]
    df["delay_s"] = df["sum_delay"] / df["obs"].where(df["obs"] > 0)
    df["carried_s"] = df["sum_prev_delay"] / df["pairs"].where(df["pairs"] > 0)
    df["gain_s"] = (df["sum_gain"] / df["pairs"].where(df["pairs"] > 0)).fillna(0.0)
    df = df.sort_values(["direction_id", "order"]).reset_index(drop=True)
    terminus = df.groupby("direction_id")["stop_name"].last()
    df["terminus"] = df["direction_id"].map(lambda d: f"vers {terminus[d]}")
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_line_cancellations(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                            route_id: str) -> pd.DataFrame:
    """Courses supprimées, ajoutées et connues par jour de service (agg_daily_trips, comme le score ; jours écartés exclus)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "t", since_day, end_day)
    df = pd.read_sql_query(
        f"""
        SELECT t.date_service, t.cancelled, t.added, t.scheduled + t.cancelled + t.added AS trips
        FROM agg_daily_trips t
        WHERE t.route_id = ? AND t.date_service >= ? AND t.date_service < ? {excl_sql}
        ORDER BY t.date_service
        """, _conn, params=(route_id, since_day, end_day, *excl_params),
    )
    df["date_service"] = pd.to_datetime(df["date_service"])
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_line_stops(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                    route_id: str) -> pd.DataFrame:
    """Arrêts d'une ligne avec passages > 5 min, arrêts non desservis et direction (agg_daily_stop)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "d", since_day, end_day)
    rows = pd.read_sql_query(
        f"""
        SELECT d.stop_id, s.stop_name, sm.commune_name AS commune,
               d.obs, d.cnt_le300, d.cnt_lt60, d.cnt_gt300, d.skipped, d.eligible, d.cnt_sans_tr, COALESCE(t.scheduled, 0) AS scheduled, COALESCE(t.cancelled, 0) AS cancelled, COALESCE(t.added, 0) AS added
        FROM agg_daily_stop d
        LEFT JOIN stops s ON s.stop_id = d.stop_id
        LEFT JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
        LEFT JOIN agg_daily_trips t ON t.date_service = d.date_service AND t.route_id = d.route_id
        WHERE d.route_id = ? AND d.date_service >= ? AND d.date_service < ? {excl_sql}
        """, _conn, params=(route_id, since_day, end_day, *excl_params),
    )
    if rows.empty:
        return rows
    rows = ind.add_components(rows)
    df = (rows.groupby("stop_id", sort=False)
          .agg(stop_name=("stop_name", "first"), commune=("commune", "first"), obs=("obs", "sum"),
               on_time=("on_time", "sum"), cnt_gt300=("cnt_gt300", "sum"), skipped=("skipped", "sum"),
               eligible=("eligible", "sum"), expected=("expected", "sum"),
               assured_on_time=("assured_on_time", "sum"))
          .reset_index())
    df = df[df["expected"] > 0].copy()
    directions = load_stop_directions(_conn)
    df["direction"] = [directions.get((route_id, sid), "") for sid in df["stop_id"]]
    df["pct_retard_5min"] = df["cnt_gt300"] / df["obs"].where(df["obs"] > 0) * 100
    df["pct_arrets_sautes"] = df["skipped"] / df["eligible"].where(df["eligible"] > 0) * 100
    df["score_fiabilite"] = ind.score_of(df["assured_on_time"], df["expected"])
    df["impact"] = df["cnt_gt300"] + df["skipped"]
    return df.sort_values("impact", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_route_hourly_stops(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                            route_id: str) -> pd.DataFrame:
    """Agrégats horaires d'une ligne, arrêt par arrêt (agg_hourly_stop, index route_id + date_service)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    excl_sql, excl_params = _excluded(_conn, "h", since_day, end_day)
    return pd.read_sql_query(
        f"""
        SELECT h.date_service, h.stop_id, h.heure, h.obs, h.sum_delay, h.cnt_gt300
        FROM agg_hourly_stop h
        WHERE h.route_id = ? AND h.date_service >= ? AND h.date_service < ? {excl_sql}
        """, _conn, params=(route_id, since_day, end_day, *excl_params),
    )


def _on_map_click() -> None:
    state = st.session_state.get("carte_arrets") or {}
    clicked = state.get("clicked") if hasattr(state, "get") else getattr(state, "clicked", None)
    if clicked:
        show_stop(clicked)


@st.cache_data(ttl=3600)
def load_route_shapes(_conn) -> dict:
    """Tracé principal de chaque ligne et sens (table route_shapes) : {route_id: {direction_id: [[lon, lat], …]}}."""
    try:
        rows = _conn.execute("SELECT route_id, direction_id, coords FROM route_shapes").fetchall()
    except sqlite3.Error:
        return {}
    out: dict = {}
    for route_id, direction, coords in rows:
        out.setdefault(route_id, {})[int(direction)] = json.loads(coords)
    return out


def _hex_rgb(color: str) -> list[int]:
    color = color.lstrip("#")
    return [int(color[i:i + 2], 16) for i in (0, 2, 4)]


def stop_route_paths(lines: pd.DataFrame, shapes: dict, directions: dict,
                     unmeasured: pd.DataFrame | None = None) -> list[dict]:
    """Tracés des lignes qui desservent un arrêt, colorés selon l'état de chaque ligne.

    `lines` : une ligne par ligne de transport (route_id, ligne, route_type, score_fiabilite) ;
    `directions` : sens de chaque ligne à cet arrêt (route_id → direction_id). Sans sens connu, les deux
    sens sont tracés. Les lignes sans mesure (`unmeasured` : route_id, ligne, route_type) sont tracées en
    gris, sans score, sous les autres ; puis les plus fiables d'abord : les moins fiables passent dessus.
    """
    out = []
    grey = _hex_rgb(SCREEN_NO_DATA)
    for r in (unmeasured if unmeasured is not None else pd.DataFrame(columns=["route_id"])).itertuples():
        if r.route_id in set(lines.get("route_id", [])):
            continue
        by_dir = shapes.get(r.route_id) or {}
        direction = directions.get(r.route_id)
        for coords in ([by_dir[direction]] if direction in by_dir else list(by_dir.values())):
            out.append({"r": r.route_id, "l": str(r.ligne), "s": None, "e": UNMEASURED_LABEL,
                        "g": mode_glyph(r.route_type), "c": grey, "t": _hex_rgb(INK), "p": coords})
    for r in lines.sort_values("score_fiabilite", ascending=False).itertuples():
        by_dir = shapes.get(r.route_id) or {}
        direction = directions.get(r.route_id)
        chosen = [by_dir[direction]] if direction in by_dir else list(by_dir.values())
        for coords in chosen:
            out.append({"r": r.route_id, "l": str(r.ligne), "s": round(float(r.score_fiabilite)),
                        "e": tier_label(float(r.score_fiabilite)), "g": mode_glyph(r.route_type),
                        "c": _hex_rgb(score_color(float(r.score_fiabilite))),
                        "t": _hex_rgb(ink_on(score_color(float(r.score_fiabilite)))),
                        "p": coords})
    return out


def selected_stop_paths(conn, cutoff: int, since_ts: int | None, end_ts: int | None, stop_id: str | None) -> list:
    if not stop_id:
        return []
    shapes = load_route_shapes(conn)
    if not shapes:
        return []
    daily = load_stop_daily(conn, cutoff, since_ts, end_ts, stop_id)
    unmeasured = load_unmeasured_lines(conn, cutoff, since_ts, end_ts, stop_id=stop_id)
    if daily.empty and unmeasured.empty:
        return []
    try:
        directions = dict(conn.execute("SELECT route_id, direction_id FROM stop_direction WHERE stop_id = ?",
                                       (stop_id,)).fetchall())
    except sqlite3.Error:
        directions = {}
    return stop_route_paths(stop_lines_table(daily), shapes, directions, unmeasured)


@st.cache_data(ttl=3600)
def load_stop_route_keys(_conn) -> dict:
    """Lignes et sens qui desservent chaque arrêt (table stop_direction) : stop_id -> [« ligne|sens »]."""
    try:
        rows = _conn.execute("SELECT stop_id, route_id, direction_id FROM stop_direction").fetchall()
    except sqlite3.Error:
        return {}
    out: dict = {}
    for stop_id, route_id, direction in rows:
        out.setdefault(stop_id, []).append(f"{route_id}|{int(direction)}")
    return out


def map_routes(conn, ranking: pd.DataFrame) -> dict:
    """Tracés de survol de toutes les lignes, à la couleur de leur score (gris sans mesure)."""
    colors = {str(r.route_id): _hex_rgb(score_color(float(r.score_fiabilite))) for r in ranking.itertuples()}
    names = {str(r.route_id): str(r.ligne) for r in ranking.itertuples()}
    return hover_routes(load_route_shapes(conn), colors, names, _hex_rgb(SCREEN_NO_DATA))


def _on_line_map_click() -> None:
    state = st.session_state.get("carte_ligne") or {}
    clicked = state.get("clicked") if hasattr(state, "get") else getattr(state, "clicked", None)
    if clicked:
        open_stop(clicked)


def line_map_stops(line_stops: pd.DataFrame, network: pd.DataFrame, route_type, ligne: str) -> pd.DataFrame:
    """Arrêts d'une ligne pour sa carte : position des arrêts, score de la ligne à chaque arrêt."""
    if line_stops.empty or network.empty:
        return pd.DataFrame(columns=["stop_id", "stop_name", "direction", "lat", "lon", "route_type",
                                     "score_fiabilite", "pct_retard_5min", "observations", "lignes"])
    pos = network.drop_duplicates("stop_id").set_index("stop_id")[["lat", "lon"]]
    df = line_stops.join(pos, on="stop_id", how="inner")
    return pd.DataFrame({
        "stop_id": df["stop_id"], "stop_name": df["stop_name"], "direction": df["direction"].fillna(""),
        "lat": df["lat"], "lon": df["lon"], "route_type": route_type,
        "score_fiabilite": df["score_fiabilite"].fillna(0.0), "pct_retard_5min": df["pct_retard_5min"].fillna(0.0),
        "observations": df["obs"].astype(int), "lignes": ligne}).reset_index(drop=True)


def render_line_map(conn, cutoff: int, since_ts: int | None, end_ts: int | None, line: pd.Series) -> None:
    """Carte de la ligne dans sa fiche : son tracé à la couleur de son état et ses arrêts à la couleur du score
    de la ligne à chacun ; un clic sur un arrêt ouvre sa fiche sur la carte."""
    stops = line_map_stops(load_line_stops(conn, cutoff, since_ts, end_ts, line["route_id"]),
                           load_territorial(conn, cutoff, since_ts, end_ts, commune=None),
                           line["route_type"], str(line["ligne"]))
    if stops.empty:
        return
    paths = stop_route_paths(pd.DataFrame([line[["route_id", "ligne", "route_type", "score_fiabilite"]]]),
                             load_route_shapes(conn), {})
    carte_arrets(map_payload(stops, group_stops(stops), None, f"Ligne {line['ligne']}", paths=paths,
                             dim_stops=False, fit=True),
                 key="carte_ligne", on_click=_on_line_map_click)
    st.caption("Couleur des arrêts : le score de la ligne à chaque arrêt. Cliquez sur un arrêt pour ouvrir sa fiche.")


def _territorial_map(stops: pd.DataFrame, groups: pd.DataFrame, commune: str | None,
                     selected_id: str | None, paths: list | None = None, routes: dict | None = None,
                     stop_routes: dict | None = None) -> None:
    """Carte des arrêts (composant `carte.carte_arrets`, deck.gl).

    Couleur = palier du score de fiabilité de l'arrêt (fiable ≥ 80/100,
    à surveiller 50–80, problématique < 50) ; forme = mode de la ligne principale
    (● tram, ■ bus, ▲ ferry) ; taille = nombre de passages analysés, qui suit
    le zoom. Les quais d'un même arrêt sont regroupés en vue éloignée et
    séparés en vue rapprochée ; l'arrêt sélectionné est entouré d'un halo.
    """
    if stops.empty:
        st.info("Aucun arrêt exploitable sur ce périmètre pour la période.")
        return
    carte_arrets(map_payload(stops, groups, selected_id, commune, focus_on_load=selected_id is not None,
                             paths=paths, routes=routes, stop_routes=stop_routes),
                 key="carte_arrets", on_click=_on_map_click)
    if paths:
        names = ", ".join(dict.fromkeys(f"{p['g']} {p['l']} ({p['e']})" for p in reversed(paths)))
        st.caption(f"Tracés des lignes qui desservent l'arrêt sélectionné, à la couleur de leur état : {names}.")


PAGE_HOME = "Accueil"
PAGE_TERRITORY = "Carte"
PAGE_LINE = "Lignes"
PAGE_REPORTS = "Rapports mensuels"
PAGE_DATA = "Méthode et lexique"
PAGE_ABOUT = "À propos"
NAV_ITEMS = [PAGE_HOME, PAGE_TERRITORY, PAGE_LINE, PAGE_REPORTS, PAGE_DATA, PAGE_ABOUT]
MAIN_PAGES = NAV_ITEMS[:3]
VIEW_STOPS = "État des arrêts"
VIEW_CONTENTION = "Temps perdu entre arrêts"
MAP_VIEWS = [VIEW_STOPS, VIEW_CONTENTION]
HOME_DETAILS = ["Quand ?", "Modes de transport", "Perturbations", "Arrêts qui ratent le plus de passages"]
HELP_TEXTS = {
    "score": "Score sur 100 : sur 100 passages prévus, combien ont été assurés (ni arrêt non desservi, ni "
             "course supprimée) et à l'heure. Fiable à partir de 80, problématique sous 50.",
    "ponctualite": "Part des passages partis entre 1 minute d'avance et 5 minutes de retard sur l'horaire prévu.",
    "retard": "Écart moyen entre l'heure de départ réelle et l'heure prévue, sur tous les passages.",
    "skip": "Part des arrêts prévus où le véhicule ne s'est pas arrêté alors que la course continuait.",
    "passage": "Un passage = un départ d'un bus, tram ou bateau à un arrêt, dont le retard est connu.",
}
LEXIQUE = [
    ("Passage", "Le départ d'un véhicule (bus, tram, bateau) à un arrêt. C'est l'unité de mesure."),
    ("À l'heure (ponctualité)", "Un passage est à l'heure s'il part entre 1 minute d'avance et 5 minutes de "
                                "retard sur l'horaire prévu. Partir plus tôt fait manquer le bus à ceux qui "
                                "arrivent à l'heure : ce n'est pas être à l'heure."),
    ("Score de fiabilité", "Note sur 100 : sur 100 passages prévus, le nombre de passages assurés (arrêt "
                           "desservi, course non supprimée) et à l'heure. C'est le service assuré multiplié "
                           "par la part des passages à l'heure."),
    ("Service assuré", "La part des passages prévus qui ont bien eu lieu : ni arrêt non desservi, ni course "
                       "supprimée (une course supprimée puis remplacée le même jour ne compte pas)."),
    ("Fiable, à surveiller, problématique", "État d'un arrêt, d'une ligne ou d'une commune selon son score : "
                                             "fiable à partir de 80, à surveiller de 50 à 80, problématique sous "
                                             "50."),
    ("Arrêt non desservi", "Le véhicule passe sans s'arrêter alors que l'arrêt était prévu (le flux de données "
                           "le signale « SKIPPED »)."),
    ("Course supprimée", "Une course prévue qui n'a pas circulé du tout. Si une course de remplacement est "
                         "ajoutée le même jour sur la ligne, elle compense la suppression dans le score."),
    ("Retard moyen", "L'écart moyen entre l'heure de départ réelle et l'heure prévue, sur tous les passages. "
                     "Un départ en avance compte comme un retard négatif."),
    ("Retards de plus de 5 min", "La part des passages partis avec plus de 5 minutes de retard sur l'horaire "
                                 "prévu."),
    ("Évolution", "L'écart, en points sur 100, entre le score de la période choisie et celui de la période "
                  "précédente (le mois précédent pour un mois)."),
    ("Retard déjà présent / retard pris", "En comparant le retard d'un même véhicule d'un arrêt au suivant, on "
                                          "sait si le retard existait déjà en arrivant ou s'il est apparu entre "
                                          "les deux arrêts (sur ce « tronçon »)."),
    ("Créneau", "Une tranche horaire : matin (6 h–10 h), journée (10 h–16 h), pointe du soir (16 h–20 h), "
                "soirée et nuit (20 h–6 h) en semaine, et le week-end."),
    ("Jour dégradé", "Un jour où au moins 15 % des passages ont plus de 5 minutes de retard."),
    ("Retard médian", "Le retard « du milieu » : la moitié des passages ont moins de retard, l'autre moitié "
                      "plus. Moins sensible qu'une moyenne à quelques très gros retards."),
    ("Jours et lignes écartés", "Les jours où la collecte des données est incomplète et les lignes dont le "
                                "temps réel est douteux (au moins un quart des retards exactement nuls) ne sont "
                                "comptés dans aucun chiffre de la période. « Méthode » en donne la liste."),
    ("Trajet sans temps réel", "Une course d'un jour donné dont tous les départs relevés (au moins deux) "
                               "valent exactement zéro : le flux republie l'horaire au lieu de la position "
                               "réelle. Ses passages ne comptent pas dans la ponctualité ni les retards, mais "
                               "comptent dans le service assuré. Un retard nul isolé reste une mesure."),
    ("Marge (±)", "L'incertitude d'un score, due aux écarts d'un jour à l'autre : le vrai niveau a 95 chances "
                  "sur 100 d'être dans la fourchette."),
    ("Observation", "Une ligne reçue du flux de données en temps réel de TBM. Sert à suivre le volume de la "
                    "collecte ; un même passage est observé plusieurs fois avant son départ."),
]
HELP_TERMS = {"score": "Score de fiabilité", "ponctualite": "À l'heure (ponctualité)", "retard": "Retard moyen",
              "skip": "Arrêt non desservi", "passage": "Passage"}


def lexique_slug(term: str) -> str:
    """Identifiant d'un terme du lexique, utilisé dans les liens (?lexique=…)."""
    ascii_text = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def lexique_link(term: str) -> str:
    return f"?lexique={lexique_slug(term)}"
DETAIL_HINT = ("Graphiques et tableaux détaillés (où, quand, quel type de retard, contexte) : activez "
               "« Vue détaillée » dans le menu de gauche.")
GUIDE_STEPS = [
    ("Cherchez votre commune, votre ligne ou votre arrêt",
     "La recherche en haut de chaque page. Le fil d'Ariane rappelle ce que vous regardez ; « Revenir » "
     "ramène à ce que vous regardiez juste avant."),
    ("Lisez l'état en un mot",
     "Chaque arrêt, ligne ou commune est **fiable**, **à surveiller** ou **problématique**. Sur les cartes, "
     "la couleur va du bleu-vert (tout va bien) au rouge vin (problème) ; le gris veut dire « pas assez de "
     "données ». La forme indique le mode : ● tram, ■ bus, ▲ ferry."),
    ("Cliquez pour comprendre",
     "Un clic sur un arrêt de la carte ou sur une ligne ouvre sa fiche : une phrase dit d'où vient le "
     "problème, quand il se produit et à qui s'adresser. La « Vue détaillée » du menu ajoute les graphiques."),
]

def _logo_data_uri() -> str:
    logo = Path(__file__).resolve().parents[1] / "assets" / "logo" / "urban-vision-logo-color.png"
    try:
        return "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode("ascii")
    except OSError:
        return ""

@st.dialog("Comment lire Urban Vision")
def show_guide() -> None:
    for i, (title, text) in enumerate(GUIDE_STEPS, start=1):
        st.markdown(f"**{i}. {title}**  \n{text}")
    st.caption("Fermez cette fenêtre (✕ ou touche Échap) pour commencer.")


def render_sidebar() -> tuple[str, bool]:
    with st.sidebar:
        st.markdown(
            f'<div class="sidebar-brand"><img src="{_logo_data_uri()}" alt="Urban Vision"/>'
            f'<span>Urban Vision</span></div>',
            unsafe_allow_html=True,
        )
        page = st.radio("Navigation", NAV_ITEMS, index=0, label_visibility="collapsed", key="sidebar_nav")
        detailed = st.toggle("Vue détaillée", key="detailed_view",
                             help="Ajoute les graphiques d'analyse, les tableaux complets et les définitions "
                                  "techniques. La vue rapide garde l'essentiel en phrases.")
        if st.button("Guide de lecture", key="guide_open"):
            show_guide()
    return page, detailed


GUIDE_COOKIE = "uv_guide_vu"


def should_show_guide(session: dict, query_params: dict, cookies: dict | None = None) -> bool:
    """Guide à la première visite : ni déjà vu dans la session, ni mémorisé par le navigateur, ni lien vers une fiche."""
    return (not session.get("guide_seen") and GUIDE_COOKIE not in (cookies or {})
            and not ({"arret", "ligne", "lexique"} & set(query_params)))


def remember_guide_seen() -> None:
    st.html(f"<script>document.cookie='{GUIDE_COOKIE}=1; max-age=31536000; path=/; SameSite=Lax'</script>",
            unsafe_allow_javascript=True)

def _kpi_color(polarity: str) -> str | None:
    """Couleur de la pastille d'une carte : palier, couleur explicite (#…), ou aucune si neutre."""
    if polarity.startswith("#"):
        return polarity
    tier_name = {"good": "positif", "bad": "negatif", "négatif": "negatif"}.get(polarity, polarity)
    return SCREEN_HEX.get(tier_name)


def kpi_card(label: str, value: str, sublabel: str | None = None, polarity: str = "neutral",
             help: str | None = None) -> str:
    color = _kpi_color(polarity)
    dot = f'<span class="kpi-dot" style="background:{color}" aria-hidden="true"></span>' if color else ""
    sub = f'<div class="kpi-sublabel">{sublabel}</div>' if sublabel else ""
    term = next((HELP_TERMS[k] for k, text in HELP_TEXTS.items() if text == help), None)
    link = (f'<a href="{lexique_link(term)}" target="_blank" rel="noopener">Voir dans le lexique ↗</a>'
            if term else "")
    tip = (f' <span class="kpi-help" tabindex="0" role="button" aria-label="Aide : {html.escape(label, quote=True)}">'
           f'?<span class="kpi-tip" role="tooltip">{html.escape(help)}{link}</span></span>') if help else ""
    return (
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{label}{tip}</div>'
        f'<div class="kpi-value">{dot}{value}</div>{sub}</div>'
    )

def render_kpis(items) -> None:
    cols = st.columns(len(items))
    for col, item in zip(cols, items):
        col.markdown(kpi_card(*item), unsafe_allow_html=True)


STOP_VIEWS = ["Où ?", "Quand ?", "Quel type ?", "Contexte"]
LINE_VIEWS = ["Retards : où ?", "Service non rendu", "Quand ?", "Contexte"]
ZONE_EXPLANATIONS = {
    CRITICAL: "Retards à la fois fréquents et longs : un problème installé, à traiter en priorité.",
    FREQUENT_SHORT: "La plupart des passages ont un peu de retard : signe d'une congestion récurrente "
                    "ou d'un temps de parcours prévu trop court.",
    RARE_LONG: "La plupart des passages sont à l'heure, mais une part notable subit de gros retards : "
               "incidents ou perturbations ponctuelles.",
    LOW: "Retards rares et courts.",
}


SCROLL_TARGETS = {"fiche-arret": ".fiche-arret", "fiche-ligne": ".fiche-ligne", "carte": ".carte-anchor"}


def panel_key(identifier: str) -> str:
    """Clé de conteneur d'une fiche, propre à l'arrêt ou à la ligne : changer de fiche rejoue son apparition."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(identifier))


def request_scroll(target: str) -> None:
    """Demande de faire défiler la page jusqu'à `target` au prochain affichage."""
    st.session_state["_scroll_to"] = target
    st.session_state["_scroll_nonce"] = st.session_state.get("_scroll_nonce", 0) + 1


def scroll_if_requested(target: str) -> None:
    """Fait défiler la page jusqu'à `target` si une action vient de le demander (une seule fois)."""
    if st.session_state.get("_scroll_to") != target:
        return
    st.session_state.pop("_scroll_to", None)
    nonce = st.session_state.get("_scroll_nonce", 0)
    st.html(
        f"<script>/* {nonce} */(function(){{const go=()=>{{const el=document.querySelector('{SCROLL_TARGETS[target]}');"
        "const calm=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;"
        "if(el){el.scrollIntoView({behavior:calm?'auto':'smooth',block:'start'});}};"
        "setTimeout(go,150);setTimeout(go,900);})();</script>",
        unsafe_allow_javascript=True,
    )


HISTORY_KEY = "_ctx_history"
HISTORY_MAX = 20
CONTEXT_KEYS = ("sidebar_nav", "map_view", "commune_idx", "line_id", "stop_id")


def context_snapshot(session) -> dict:
    return {k: session.get(k) for k in CONTEXT_KEYS}


def remember(session=None) -> None:
    """Mémorise ce que l'utilisateur regarde avant un changement, pour le bouton « Revenir »."""
    session = st.session_state if session is None else session
    snap = context_snapshot(session)
    history = list(session.get(HISTORY_KEY) or [])
    if not history or history[-1] != snap:
        history.append(snap)
    session[HISTORY_KEY] = history[-HISTORY_MAX:]


def go_back() -> None:
    history = list(st.session_state.get(HISTORY_KEY) or [])
    if not history:
        return
    snap = history.pop()
    st.session_state[HISTORY_KEY] = history
    for key, value in snap.items():
        if key in ("sidebar_nav", "map_view") and value is None:
            continue
        st.session_state[key] = value
    st.session_state.pop("_from_stop", None)


def back_label(snap: dict | None, communes: list[str], line_names: dict, stop_names: dict) -> str | None:
    """Ce que l'utilisateur retrouvera en cliquant « Revenir » (None s'il n'y a rien avant)."""
    if not snap:
        return None
    page = snap.get("sidebar_nav") or NAV_ITEMS[0]
    if snap.get("stop_id") and page == PAGE_TERRITORY:
        return f"l'arrêt {stop_names.get(snap['stop_id'], snap['stop_id'])}"
    if snap.get("line_id") and page == PAGE_LINE:
        return f"la ligne {line_names.get(snap['line_id'], snap['line_id'])}"
    idx = snap.get("commune_idx") or 0
    where = communes[idx - 1] if 0 < idx <= len(communes) else "tout le réseau"
    return f"« {page} », {where}"


def contention_view(session=None) -> bool:
    """Vrai quand la carte affichée est celle du temps perdu entre arrêts (période scolaire imposée)."""
    session = st.session_state if session is None else session
    return session.get("sidebar_nav") == PAGE_TERRITORY and session.get("map_view") == VIEW_CONTENTION


def select_stop(stop_id: str | None) -> None:
    remember()
    st.session_state["stop_id"] = stop_id


def show_stop(stop_id: str) -> None:
    """Ouvre la fiche d'un arrêt sur la page courante et y amène l'utilisateur."""
    select_stop(stop_id)
    request_scroll("fiche-arret")


def open_stop(stop_id: str) -> None:
    show_stop(stop_id)
    st.session_state["sidebar_nav"] = PAGE_TERRITORY
    st.session_state["map_view"] = VIEW_STOPS


def show_line(route_id: str) -> None:
    """Ouvre la fiche d'une ligne sur la page « Lignes » et y amène l'utilisateur."""
    remember()
    st.session_state["line_id"] = route_id
    st.session_state.pop("_from_stop", None)
    st.session_state.pop("_dir_from_stop", None)
    request_scroll("fiche-ligne")


def open_line(route_id: str) -> None:
    show_line(route_id)
    st.session_state["sidebar_nav"] = PAGE_LINE


def line_from_stop(route_id: str, stop_id: str, stop_name: str) -> None:
    """Depuis une fiche arrêt : ouvre la fiche de la ligne en gardant le chemin du retour."""
    open_line(route_id)
    st.session_state["_from_stop"] = (stop_id, stop_name)
    st.session_state["_dir_from_stop"] = stop_id


SEARCH_KEY = "ctx_search"


def _on_search() -> None:
    """Choix dans la recherche unique : commune (« c:3 »), ligne (« l:23 ») ou arrêt (« s:<quai> »)."""
    chosen = st.session_state.get(SEARCH_KEY)
    st.session_state[SEARCH_KEY] = None
    if not chosen:
        return
    kind, _, value = str(chosen).partition(":")
    if kind == "c":
        remember()
        st.session_state["commune_idx"] = int(value)
        st.session_state["stop_id"] = None
    elif kind == "l":
        open_line(value)
        st.session_state["stop_id"] = None
    elif kind == "s":
        open_stop(value)
        st.session_state["line_id"] = None


def _crumb_network() -> None:
    remember()
    st.session_state.update({"commune_idx": 0, "line_id": None, "stop_id": None})


def _crumb_commune() -> None:
    remember()
    st.session_state.update({"line_id": None, "stop_id": None})


def _crumb_line() -> None:
    remember()
    st.session_state["stop_id"] = None
    st.session_state["sidebar_nav"] = PAGE_LINE


def breadcrumb(commune: str | None, line_name: str | None, stop_name: str | None) -> list[tuple[str, object]]:
    """Étapes du fil d'Ariane (réseau, commune, ligne, arrêt) ; la dernière, ce qu'on regarde, n'est pas cliquable."""
    steps = [("Tout le réseau", _crumb_network)]
    if commune:
        steps.append((commune, _crumb_commune))
    if line_name:
        steps.append((f"Ligne {line_name}", _crumb_line))
    if stop_name:
        steps.append((stop_name, None))
    return [(label, None if i == len(steps) - 1 else cb) for i, (label, cb) in enumerate(steps)]


def _on_table_select(table_key: str, ids_key: str, opener) -> None:
    state = st.session_state.get(table_key) or {}
    rows = (state.get("selection") or {}).get("rows") or []
    ids = st.session_state.get(ids_key) or []
    if rows and rows[0] < len(ids):
        opener(ids[rows[0]])


def apply_query_params() -> None:
    """Ouvre la fiche désignée par l'URL (?arret=… ou ?ligne=…) au premier affichage."""
    if st.session_state.get("_query_applied"):
        return
    st.session_state["_query_applied"] = True
    params = st.query_params
    if params.get("lexique"):
        st.session_state["sidebar_nav"] = PAGE_DATA
        st.session_state["data_view"] = "Lexique"
        st.session_state["lexique_focus"] = params["lexique"]
    elif params.get("arret"):
        st.session_state["stop_id"] = params["arret"]
        st.session_state["sidebar_nav"] = PAGE_TERRITORY
        st.session_state["map_view"] = VIEW_STOPS
    elif params.get("ligne"):
        st.session_state["line_id"] = params["ligne"]
        st.session_state["sidebar_nav"] = PAGE_LINE


def network_score(ranking: pd.DataFrame) -> float:
    """Score de fiabilité de l'ensemble des lignes : composantes du score sommées sur toutes les lignes."""
    score = ind.score_of(float(ranking["assured_on_time"].sum()), float(ranking["expected"].sum()))
    return 0.0 if pd.isna(score) else float(score)


def evolution_words(delta: float) -> str:
    """Évolution d'un score en mots, sans couleur d'état : « ▼ 0,8 point », « stable »."""
    if abs(delta) < 0.05:
        return "stable"
    arrow = "▲" if delta > 0 else "▼"
    return f"{arrow} {format_number(abs(delta), 1)} point{'s' if abs(delta) >= 2 else ''}"


def _alerts_overlapping(history: pd.DataFrame, route_ids: set, bad_dates: list) -> pd.DataFrame:
    """Alertes des lignes données, avec un indicateur « recoupe un jour dégradé »."""
    if history is None or history.empty:
        return pd.DataFrame(columns=["ligne", "header_text", "debut_effectif", "fin_effective", "jour_degrade"])
    h = history[history["route_id"].isin(route_ids)].copy()
    if h.empty:
        return h.assign(jour_degrade=pd.Series(dtype=bool))
    starts = pd.to_datetime(h["debut_effectif"], format="%d/%m/%Y %H:%M").dt.normalize()
    ends = pd.to_datetime(h["fin_effective"], format="%d/%m/%Y %H:%M").dt.normalize()
    h["jour_degrade"] = [any(s <= d <= e for d in bad_dates) for s, e in zip(starts, ends)]
    return h.sort_values("jour_degrade", ascending=False)


def _render_brief(summary: list[str], hints: list[str]) -> None:
    items = "".join(f"<li>{html.escape(x)}</li>" for x in summary)
    st.markdown(f'<div class="insight brief"><b>En bref</b><ul>{items}</ul></div>', unsafe_allow_html=True)
    if hints:
        items = "".join(f"<li>{html.escape(x)}</li>" for x in hints)
        st.markdown(f'<div class="hints"><b>Pistes</b> <span class="hint-note">(indices à '
                    f'confirmer sur le terrain, pas des conclusions)</span><ul>{items}</ul></div>',
                    unsafe_allow_html=True)


def _render_alerts(alerts: pd.DataFrame) -> None:
    if alerts.empty:
        st.info("Aucune perturbation signalée par TBM sur ces lignes pendant la période.")
        return
    for r in alerts.head(8).itertuples():
        flag = " · <b>recoupe un jour dégradé</b>" if r.jour_degrade else ""
        st.markdown(
            f"**Ligne {html.escape(str(r.ligne))}** — {html.escape(r.header_text or '')}  \n"
            f"<span style='color:{INK_MUTED}'>{r.debut_effectif} → {r.fin_effective}{flag}</span>",
            unsafe_allow_html=True,
        )
    st.caption(CAUTION_TEXT)


def zone_banner_html(median_s: float | None, pct_gt300: float) -> str:
    """Bandeau « Type de retard » (zones de `palette.risk_zone`), vide sans retard médian. Retard médian
    négatif au-delà du seuil : « Écart à l'horaire publié », chiffres seuls, sans phrase d'interprétation."""
    if median_s is None or pd.isna(median_s):
        return ""
    zone = dg.display_zone(median_s, pct_gt300)
    if zone == dg.SCHEDULE_GAP:
        text = (f"Écart médian à l'horaire publié {format_seconds(median_s, signed=True)}, "
                f"{format_number(pct_gt300, 1)} % de passages à plus de 5 min de retard.")
    else:
        text = (f"{ZONE_EXPLANATIONS[zone]} Retard médian {format_seconds(median_s, signed=True)}, "
                f"{format_number(pct_gt300, 1)} % de passages à plus de 5 min.")
    return (f'<div class="zone-banner zone-{zone}"><span class="zone-kicker">Type de retard</span>'
            f'<span class="zone-name">{html.escape(dg.zone_label(zone))}</span>'
            f'<span class="zone-text">{html.escape(text)}</span></div>')


def _render_zone_banner(median_s: float | None, pct_gt300: float) -> None:
    banner = zone_banner_html(median_s, pct_gt300)
    if banner:
        st.markdown(banner, unsafe_allow_html=True)


def _slot_selectors(table: pd.DataFrame, peak: dict, key: str) -> tuple[int, int] | None:
    """Choix d'un jour et d'une heure ; par défaut, le moment qui ressort (ou la case la plus dégradée)."""
    cells = table[table["obs"] >= dg.MIN_CELL_PASSAGES] if not table.empty else table
    if cells.empty:
        return None
    days = sorted(int(x) for x in cells["weekday"].unique())
    hours = sorted(int(x) for x in cells["heure"].unique())
    if peak.get("weekday") in days and peak.get("hour") in hours:
        day0, hour0 = peak["weekday"], peak["hour"]
    else:
        worst = cells.loc[cells["pct_gt300"].idxmax()]
        day0, hour0 = int(worst["weekday"]), int(worst["heure"])
    left, right, _ = st.columns([1, 1, 2])
    with left:
        day = st.selectbox("Jour", days, index=days.index(day0), format_func=lambda d: dg.WEEKDAYS[d],
                           key=f"{key}_day")
    with right:
        hour = st.selectbox("Heure", hours, index=hours.index(hour0), key=f"{key}_hour",
                            format_func=lambda h: f"{h} h – {'0' if h == 23 else h + 1} h")
    return int(day), int(hour)


def _render_week_grid(table: pd.DataFrame, peak: dict) -> None:
    render_tier_legend("Passages à plus de 5 min", invert=True)
    st.markdown(week_hour_grid_html(table, peak), unsafe_allow_html=True)
    st.caption("Chaque case : % de passages à plus de 5 min pour ce jour et cette heure, sur toute la période. "
               "Case vide : moins de 3 passages. Case encadrée : moment qui ressort. Survolez une case pour "
               "le détail.")


def _commune_ids(profile: pd.DataFrame | None, commune: str | None) -> set:
    if not commune or profile is None or profile.empty:
        return set()
    return set(profile.loc[profile["commune"] == commune, "stop_id"])


def _render_stop_when(conn, cutoff: int, since_ts: int | None, end_ts: int | None, stop_id: str,
                      name: str, lines: pd.DataFrame, daily: pd.DataFrame, hourly_all: pd.DataFrame,
                      seg_ok: bool, commune: str | None) -> None:
    st.markdown("#### À quel moment de la semaine ?")
    names = dict(zip(lines["route_id"], lines["ligne"].astype(str)))
    labels = {"__toutes__": "Toutes les lignes",
              **{rid: f"{mode_glyph(rt)} {l}" for rid, rt, l in zip(lines["route_id"], lines["route_type"], lines["ligne"])}}
    chosen = st.segmented_control("Ligne", list(labels), format_func=lambda r: labels.get(r, r),
                                  default="__toutes__", key=f"when_line_{stop_id}") or "__toutes__"
    hourly = hourly_all if chosen == "__toutes__" else hourly_all[hourly_all["route_id"] == chosen]
    table = dg.week_hour_table(hourly)
    if table.empty:
        st.info("Aucune donnée horaire pour cet arrêt sur la période.")
        return
    peak = dg.find_peak(hourly)
    peak_route = chosen if chosen != "__toutes__" else None
    if "weekday" in peak and peak_route is None:
        in_slot = dg.slot_lines(hourly_all, peak["weekday"], peak["hour"])
        peak_route = in_slot["route_id"].iloc[0] if not in_slot.empty else None
    _render_week_grid(table, peak)
    sentence = dg.peak_sentence(peak, names.get(peak_route) if chosen == "__toutes__" else None)
    if sentence:
        _verdict(html.escape(sentence))

    route = peak_route or (chosen if chosen != "__toutes__" else str(lines["route_id"].iloc[0]))
    st.markdown(f"#### Ligne {html.escape(names.get(route, route))} à ce moment-là : le retard vient-il "
                "d'avant, et jusqu'où se prolonge-t-il ?")
    if not seg_ok:
        st.info("L'analyse le long de la ligne est en cours de constitution par le collecteur.")
    else:
        seg = load_route_segments(conn, cutoff, since_ts, end_ts, route)
        here = seg[seg["stop_id"] == stop_id] if not seg.empty else seg
        if here.empty:
            st.info("Pas de profil de ligne disponible pour cet arrêt sur la période.")
        else:
            profile_dir = seg[seg["direction_id"] == here.loc[here["obs"].idxmax(), "direction_id"]]
            route_table = dg.week_hour_table(hourly_all[hourly_all["route_id"] == route])
            choice = _slot_selectors(route_table, peak if peak_route == route else {}, f"slot_{stop_id}_{route}")
            if choice:
                wd, hr = choice
                sp = dg.slot_profile(load_route_hourly_stops(conn, cutoff, since_ts, end_ts, route), profile_dir, wd, hr)
                i = int(sp.index[sp["stop_id"] == stop_id][0])
                window = sp.iloc[max(0, i - 10):i + 12]
                hc_render(slot_profile_chart(window, dg.slot_label(wd, hr), highlight_stop_id=stop_id,
                                             commune_stop_ids=_commune_ids(profile_dir, commune),
                                             commune_label=commune), height=340)
                sentence = dg.propagation_sentence(dg.propagation(sp, stop_id), names.get(route, route), name)
                if sentence:
                    st.markdown(sentence)
                st.caption("Courbe pleine : retard moyen des passages de chaque arrêt à ce moment-là (tous les "
                           "jours choisis de la période, sur l'heure choisie) ; tirets : le reste du temps. "
                           "10 arrêts avant, 11 après.")
    with st.expander("Jour par jour sur la période"):
        render_tier_legend("Retards > 5 min", invert=True)
        hc_render(daily_status_chart(daily, RISK_PCT_GT300), height=240)


def status_kpi(score: float, sublabel: str) -> tuple:
    """Carte « État » : le mot du palier en grand, le score sur 100 et sa référence en dessous."""
    return ("État", tier_label(score).capitalize(), f"score {score:.0f} / 100 · {sublabel}",
            score_color(score), HELP_TEXTS["score"])


def _evolution_kpi(change: dict | None, prev_label: str | None) -> tuple:
    if prev_label is None:
        return ("Évolution", "—", "pas de période de comparaison", "neutral")
    if change is None:
        return ("Évolution", "—", f"pas de données sur {prev_label}", "neutral")
    return ("Évolution", evolution_words(change["delta"]), f"vs {prev_label} ({change['previous']:.0f} / 100)",
            "neutral")


def render_stop_panel(conn, cutoff: int, since_ts: int | None, end_ts: int | None, stop_id: str,
                      territorial_network: pd.DataFrame, groups: pd.DataFrame, reference_score: float,
                      commune: str | None = None, prev: tuple = (None, None, None),
                      detailed: bool = True, period_label: str = "la période") -> None:
    """Fiche diagnostic d'un arrêt : d'où vient le problème, quand, de quel type, et quelles pistes."""
    daily = load_stop_daily(conn, cutoff, since_ts, end_ts, stop_id)
    if daily.empty:
        st.info("Aucun passage analysé à cet arrêt sur la période.")
        return
    lines = stop_lines_table(daily)
    info = territorial_network[territorial_network["stop_id"] == stop_id]
    name = str(info["stop_name"].iloc[0]) if not info.empty else stop_id
    obs = int(lines["observations"].sum())
    eligible = int(lines["eligible"].sum())
    skipped = int(lines["skipped"].sum())
    cnt_gt300 = int(lines["cnt_gt300"].sum())
    pct_gt300 = cnt_gt300 / max(obs, 1) * 100
    pct_skip = skipped / eligible * 100 if eligible else 0.0
    score = dg.period_score(daily) or 0.0
    median = _median_from_hists(list(daily["hist"]))
    days = int(daily["date_service"].nunique())

    responsible = dg.responsible_line(lines)
    directions = load_stop_directions(conn)
    resp_dir = directions.get((responsible["route_id"], stop_id)) if responsible else None
    seg_ok = segments_available(conn)
    seg_row, profile_dir = None, None
    if responsible is not None and seg_ok:
        seg = load_route_segments(conn, cutoff, since_ts, end_ts, responsible["route_id"])
        here = seg[seg["stop_id"] == stop_id] if not seg.empty else seg
        if not here.empty:
            seg_row = here.loc[here["obs"].idxmax()]
            profile_dir = seg[seg["direction_id"] == seg_row["direction_id"]]
    carried = seg_row["carried_s"] if seg_row is not None else None
    gained = seg_row["gain_s"] if seg_row is not None else None
    prev_name = seg_row["prev_stop_name"] if seg_row is not None and isinstance(seg_row["prev_stop_name"], str) else None
    cause = dg.locate_cause(carried, gained, seg_row["delay_s"] if seg_row is not None else None)
    hotspot = dg.upstream_hotspot(profile_dir, stop_id) if profile_dir is not None else None

    rec = dg.recurrence(daily[["date_service", "obs", "cnt_gt300"]])
    hourly_all = load_stop_hourly(conn, cutoff, since_ts, end_ts, stop_id)
    periods = dg.period_table(hourly_all)
    weekdays = dg.weekday_table(daily)
    conc = dg.concentration(periods, "période") or dg.concentration(weekdays, "jour")
    peak = dg.find_peak(hourly_all)
    peak_line = None
    if peak.get("verdict") == "récurrent":
        in_slot = dg.slot_lines(hourly_all, peak["weekday"], peak["hour"])
        if not in_slot.empty:
            peak_line = dict(zip(lines["route_id"], lines["ligne"].astype(str))).get(in_slot["route_id"].iloc[0])
    zone = dg.display_zone(median, pct_gt300) if median is not None else None
    prev_since, prev_end, prev_label = prev
    change = (dg.score_change(daily, load_stop_daily(conn, cutoff, prev_since, prev_end, stop_id))
              if prev_since is not None else None)
    peers = territorial_network.loc[territorial_network["observations"] >= MIN_OBSERVATIONS, "score_fiabilite"]
    rank = dg.percentile_rank(score, peers)
    alerts = _alerts_overlapping(load_perturbation_history(conn, since_ts, end_ts, cutoff),
                                 set(lines["route_id"]), rec["bad_dates"])
    summary = dg.stop_summary(responsible, resp_dir, cause, carried, gained, prev_name, hotspot, rec, conc, zone,
                              peak, peak_line)
    hints = dg.stop_hints(cause, hotspot, prev_name, name, responsible, rec, pct_skip,
                          bool(not alerts.empty and alerts["jour_degrade"].any()), carried, peak)

    st.query_params["arret"] = stop_id
    if "ligne" in st.query_params:
        del st.query_params["ligne"]
    glyph_lines = " · ".join(f"{mode_glyph(rt)} {html.escape(str(l))}"
                             for rt, l in zip(lines["route_type"], lines["ligne"]))
    direction = info["direction"].iloc[0] if not info.empty else ""
    st.markdown(
        f'<div class="fiche-title fiche-arret">{html.escape(name)}'
        f'{", " + html.escape(direction) if direction else ""}</div>'
        f'<div class="fiche-sub">Lignes : {glyph_lines}</div>',
        unsafe_allow_html=True,
    )
    scroll_if_requested("fiche-arret")
    _verdict(verdict_sentence(f"l'arrêt {html.escape(name)}", period_label, score,
                              {"ecart": change["delta"], "marge": None, "significatif": None} if change else None,
                              prev_label))
    group = groups[groups["members"].map(lambda m: stop_id in m)] if not groups.empty else groups
    siblings = []
    if not group.empty:
        others = territorial_network[territorial_network["stop_id"].isin(group.iloc[0]["members"])
                                     & (territorial_network["stop_id"] != stop_id)].sort_values("score_fiabilite")
        siblings = [(r.stop_id, r.direction or r.stop_name, r.lignes, r.score_fiabilite)
                    for r in others.itertuples()][:3]
    if siblings:
        st.markdown('<div class="sibling-label">Autres quais de cet arrêt (autre sens ou autres lignes) :</div>',
                    unsafe_allow_html=True)
    cols = st.columns(len(siblings) + 1 if siblings else 2)
    for col, (sid, label, lignes, sc) in zip(cols, siblings):
        col.button(f"{label} ({lignes}) · {sc:.0f}/100", key=f"sibling_{sid}", on_click=show_stop, args=(sid,),
                   width="stretch")
    cols[-1 if siblings else 0].button("↑ Voir la carte", key="back_to_map", on_click=request_scroll,
                                       args=("carte",))
    kpis = [
        status_kpi(score, f"réseau : {reference_score:.0f} / 100"),
        _evolution_kpi(change, prev_label),
        ("Passages à plus de 5 min", f"{format_number(pct_gt300, 1)} %", f"{cnt_gt300 / max(days, 1):.0f} par jour en moyenne",
         palette_kpi_tier({"retard_5min": pct_gt300}, "retard_5min")),
        ("Arrêts non desservis", f"{format_number(pct_skip, 1)} %", f"{skipped:,} passages non desservis".replace(",", " "),
         palette_kpi_tier({"skip_rate": pct_skip}, "skip_rate"), HELP_TEXTS["skip"]),
    ]
    if detailed:
        kpis.append(("Échantillon", f"{obs:,}".replace(",", " ") + " passages",
                     f"{days} jour{'s' if days > 1 else ''} · "
                     + ("échantillon faible" if obs < MIN_OBSERVATIONS else "échantillon suffisant"), "neutral"))
    render_kpis(kpis)
    if not detailed and obs < MIN_OBSERVATIONS:
        st.caption(f"Peu de passages mesurés ici ({obs}) : ces chiffres sont à lire avec prudence.")
    _render_brief(summary, hints)
    if responsible is not None:
        st.button(f"Ouvrir la fiche de la ligne {responsible['ligne']} → où se forme son retard, à quel moment",
                  type="primary", on_click=line_from_stop, args=(responsible["route_id"], stop_id, name),
                  key="open_line_from_stop")
    if not detailed:
        st.caption(DETAIL_HINT)
        return

    view = st.segmented_control("Détail", STOP_VIEWS, default=STOP_VIEWS[0], key="stop_view",
                                label_visibility="collapsed") or STOP_VIEWS[0]
    if view == "Où ?":
        left, right = st.columns([0.8, 1.2], gap="large")
        with left:
            st.markdown("#### Passages problématiques par ligne")
            hc_render(stop_lines_chart(lines), height=max(160, 60 + 34 * len(lines)))
        with right:
            if responsible is None:
                st.info("Aucun passage problématique à cet arrêt sur la période.")
            elif not seg_ok:
                st.info("L'analyse amont (retard pris tronçon par tronçon) est en cours de constitution "
                        "par le collecteur.")
            elif profile_dir is None:
                st.info("Pas de données de tronçon pour cette ligne à cet arrêt sur la période.")
            else:
                st.markdown(f"#### Ligne {html.escape(responsible['ligne'])} : d'où vient le retard ?")
                p = profile_dir.sort_values("order").reset_index(drop=True)
                i = int(p.index[p["stop_id"] == stop_id][0])
                window = p.iloc[max(0, i - 8):i + 3]
                hot = {hotspot["stop_id"]} if dg.is_dominant_hotspot(hotspot, carried) else set()
                hc_render(line_profile_chart(window, highlight_stop_id=stop_id, hotspot_stop_ids=hot,
                                             commune_stop_ids=_commune_ids(profile_dir, commune),
                                             commune_label=commune), height=360)
                st.caption("Colonnes : retard pris sur chaque tronçon (en orange foncé, le tronçon amont qui en "
                           "prend le plus, s'il pèse au moins un quart du retard importé) ; courbe : retard "
                           "moyen à l'arrêt. 8 arrêts en amont, 2 en aval.")
    elif view == "Quand ?":
        _render_stop_when(conn, cutoff, since_ts, end_ts, stop_id, name, lines, daily, hourly_all, seg_ok, commune)
    elif view == "Quel type ?":
        if median is None:
            st.info("Pas assez de passages pour qualifier le type de retard.")
        else:
            _render_zone_banner(median, pct_gt300)
        if rank is not None:
            st.markdown(f"Cet arrêt fait partie des **{rank:.0f} %** d'arrêts les moins fiables du réseau."
                        if rank <= 50 else f"Cet arrêt est plus fiable que **{rank:.0f} %** des arrêts du réseau.")
        with st.expander("Répartition des écarts à l'horaire"):
            render_tier_legend("Écart à l'horaire", invert=True)
            hc_render(delay_distribution_chart(distribution_from_hists(daily["hist"])), height=280)
    else:
        _render_alerts(alerts)


LINE_DIRECTION_KEYS = ("line_dir_Retards : où ?", "line_dir_Service non rendu", "line_when_dir")


def stop_direction_in(per_dir: dict, stop_id: str | None):
    """Direction de la ligne dans laquelle se trouve l'arrêt (la plus observée s'il est dans les deux)."""
    if not stop_id:
        return None
    best, best_obs = None, -1
    for d, info in per_dir.items():
        rows = info["profile"][info["profile"]["stop_id"] == stop_id]
        if not rows.empty and int(rows["obs"].iloc[0]) > best_obs:
            best, best_obs = d, int(rows["obs"].iloc[0])
    return best


def _direction_choice(per_dir: dict, default, key: str):
    dirs = list(per_dir)
    if st.session_state.get(key) not in dirs:
        st.session_state[key] = default
    return st.radio("Direction", dirs, horizontal=True, format_func=lambda d: per_dir[d]["terminus"], key=key)


def _render_line_when(conn, cutoff: int, since_ts: int | None, end_ts: int | None, route_id: str,
                      core: pd.DataFrame, hourly: pd.DataFrame, peak: dict, per_dir: dict, main_dir,
                      commune: str | None, focus_stop: str | None = None) -> None:
    st.markdown("#### À quel moment de la semaine ?")
    table = dg.week_hour_table(hourly)
    if table.empty:
        st.info("Aucune donnée horaire pour cette ligne sur la période.")
        return
    _render_week_grid(table, peak)
    sentence = dg.peak_sentence(peak)
    if sentence:
        _verdict(html.escape(sentence))
    if per_dir:
        st.markdown("#### À ce moment-là, où le retard s'aggrave-t-il le long de la ligne ?")
        chosen = _direction_choice(per_dir, main_dir, "line_when_dir")
        choice = _slot_selectors(table, peak, f"slot_line_{route_id}")
        if choice:
            wd, hr = choice
            profile = per_dir[chosen]["profile"]
            sp = dg.slot_profile(load_route_hourly_stops(conn, cutoff, since_ts, end_ts, route_id), profile, wd, hr)
            _verdict(html.escape(dg.slot_hotspot_sentence(dg.slot_hotspot(sp))))
            hc_render(slot_profile_chart(sp, dg.slot_label(wd, hr), highlight_stop_id=focus_stop,
                                         commune_stop_ids=_commune_ids(profile, commune),
                                         commune_label=commune), height=340)
            st.caption("Courbe pleine : retard moyen des passages de chaque arrêt à ce moment-là ; tirets : le "
                       "reste du temps. L'écart entre les deux courbes montre où ce moment-là se dégrade.")
    if not core.empty:
        with st.expander("Jour par jour sur la période"):
            render_tier_legend("Retards > 5 min", invert=True)
            hc_render(daily_status_chart(core, RISK_PCT_GT300), height=240)


def excluded_line_message(conn, cutoff: int, since_ts: int | None, end_ts: int | None, route_id: str) -> str | None:
    """Phrase expliquant qu'une ligne est écartée de la période (temps réel douteux), sinon None."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff)
    if route_id not in load_exclusions(conn, since_day, end_day).lignes:
        return None
    share = ind.doubtful_routes(conn, since_day, end_day).get(route_id)
    detail = f" ({share:.0f} % de retards exactement nuls)" if share is not None else ""
    return (f"Ligne écartée sur la période : son temps réel est douteux{detail}, ses chiffres ne seraient pas "
            "fiables. Voir « Méthode et lexique ».")


def render_line_panel(conn, cutoff: int, since_ts: int | None, end_ts: int | None, route_id: str,
                      ranking_net: pd.DataFrame, disturbed: set, commune: str | None = None,
                      prev: tuple = (None, None, None), detailed: bool = True, period_label: str = "la période") -> None:
    """Fiche diagnostic d'une ligne : pourquoi elle n'est pas fiable, où, quand, et quelles pistes."""
    row = ranking_net[ranking_net["route_id"] == route_id]
    if row.empty:
        unmeasured = load_unmeasured_lines(conn, cutoff, since_ts, end_ts)
        match = unmeasured[unmeasured["route_id"] == route_id]
        st.info(excluded_line_message(conn, cutoff, since_ts, end_ts, route_id)
                or (unmeasured_line_message(str(match["ligne"].iloc[0])) if not match.empty else None)
                or "Aucun passage analysé pour cette ligne sur la période.")
        return
    line = row.iloc[0]
    breakdown = dg.score_breakdown(line["score_fiabilite"], line["pct_service"])
    same_mode = ranking_net[(ranking_net["route_type"] == line["route_type"])
                            & (ranking_net["observations"] >= MIN_OBSERVATIONS)]
    mode_median = float(same_mode["score_fiabilite"].median()) if not same_mode.empty else None
    core = _load_daily_core(conn, cutoff, since_ts, end_ts, route_id=route_id)
    prev_since, prev_end, prev_label = prev
    change = (dg.score_change(core, _load_daily_core(conn, cutoff, prev_since, prev_end, route_id=route_id))
              if prev_since is not None and not core.empty else None)
    hourly = _load_hourly_core(conn, cutoff, since_ts, end_ts, route_id=route_id)
    periods = dg.period_table(hourly)
    weekdays = dg.weekday_table(core)
    rec = dg.recurrence(core[["date_service", "obs", "cnt_gt300"]] if not core.empty else core)
    conc = dg.concentration(periods, "période") or dg.concentration(weekdays, "jour")
    peak = dg.find_peak(hourly)
    canc = load_line_cancellations(conn, cutoff, since_ts, end_ts, route_id)
    cancelled = int(canc["cancelled"].sum()) if not canc.empty else 0
    trips = int(canc["trips"].sum()) if not canc.empty else 0
    stops = load_line_stops(conn, cutoff, since_ts, end_ts, route_id)
    seg_ok = segments_available(conn)
    seg = load_route_segments(conn, cutoff, since_ts, end_ts, route_id) if seg_ok else pd.DataFrame()

    per_dir = {}
    for d, prof in (seg.groupby("direction_id") if not seg.empty else []):
        per_dir[d] = {"profile": prof, "terminus": prof["terminus"].iloc[0],
                      "origin": dg.classify_delay_origin(prof), "skips": dg.classify_skips(prof)}
    main_dir = max(per_dir, key=lambda d: per_dir[d]["origin"]["peak_delay"] or 0) if per_dir else None
    skip_dir = max(per_dir, key=lambda d: per_dir[d]["skips"]["rate"]) if per_dir else None
    origin = per_dir[main_dir]["origin"] if main_dir is not None else dg.classify_delay_origin(None)
    skips = per_dir[skip_dir]["skips"] if skip_dir is not None else dg.classify_skips(None)
    by_dir = (stops[stops["direction"] != ""].groupby("direction")
              .agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index()
              .rename(columns={"direction": "terminus"})) if not stops.empty else pd.DataFrame()
    imbalance = dg.direction_imbalance(by_dir)
    commune_info = dg.commune_share(seg, commune) if not seg.empty else None
    ligne = str(line["ligne"])
    summary = dg.line_summary(ligne, breakdown, cancelled, origin, imbalance, skips, rec, conc, peak, commune_info)
    hints = dg.line_hints(origin, skips, cancelled, peak)

    st.query_params["ligne"] = route_id
    if "arret" in st.query_params:
        del st.query_params["arret"]
    termini = " ↔ ".join(dict.fromkeys(v["terminus"].removeprefix("vers ") for v in per_dir.values()))
    n_communes = int(stops["commune"].nunique()) if not stops.empty else 0
    days = int(core["date_service"].nunique()) if not core.empty else 0
    marker = " ⚠" if route_id in disturbed else ""
    per_day = f"{int(line['observations']) / max(days, 1):,.0f}".replace(",", " ")
    st.markdown(
        f'<div class="fiche-title fiche-ligne">{mode_glyph(line["route_type"])} Ligne {html.escape(ligne)}{marker} · '
        f'{html.escape(str(line["mode"]))}</div>'
        f'<div class="fiche-sub">{html.escape(termini) + " · " if termini else ""}'
        f'{n_communes} commune(s) desservie(s) · {per_day} passages par jour</div>',
        unsafe_allow_html=True,
    )
    scroll_if_requested("fiche-ligne")
    margins = load_score_change(conn, cutoff, since_ts, end_ts, prev_since, prev_end, route_id=route_id)
    comparison = (margins or {}).get("compare") or (
        {"ecart": change["delta"], "marge": None, "significatif": None} if change else None)
    _verdict(verdict_sentence(f"la ligne {html.escape(ligne)}", period_label, float(line["score_fiabilite"]),
                              comparison, prev_label))
    back = st.session_state.get("_from_stop")
    focus_stop = back[0] if back else None
    from_dir = stop_direction_in(per_dir, focus_stop)
    if st.session_state.pop("_dir_from_stop", None) and from_dir is not None:
        for key in LINE_DIRECTION_KEYS:
            st.session_state[key] = from_dir
    if route_id in disturbed:
        st.warning(CAUTION_TEXT)
    ref = f"réseau : {network_score(ranking_net):.0f}"
    if mode_median is not None:
        ref += f" · médiane {str(line['mode']).lower()} : {mode_median:.0f}"
    if not detailed:
        render_kpis([
            status_kpi(line["score_fiabilite"], ref),
            _evolution_kpi(change, prev_label),
            ("À l'heure", f"{line['pct_a_l_heure']:.0f} %", "des passages, de 1 min d'avance à 5 min de retard",
             palette_kpi_tier({"ponctualite": line["pct_a_l_heure"]}, "ponctualite"), HELP_TEXTS["ponctualite"]),
            ("Courses supprimées", f"{cancelled}", f"sur {trips:,} courses connues".replace(",", " "), "neutral"),
        ])
        render_line_map(conn, cutoff, since_ts, end_ts, line)
        _render_zone_banner(line["retard_median_s"], float(line["pct_retard_5min"]))
        _render_brief(summary, hints)
        st.caption(DETAIL_HINT)
        return
    render_kpis([
        status_kpi(line["score_fiabilite"], ref),
        _evolution_kpi(change, prev_label),
        ("Points perdus : horaires", f"{breakdown['lost_delay']:.0f}",
         f"{format_number(line['pct_retard_5min'], 1)} % de passages à plus de 5 min, "
         f"{format_number(line['pct_avance_1min'], 1)} % en avance",
         palette_kpi_tier({"retard_5min": line["pct_retard_5min"]}, "retard_5min")),
        ("Points perdus : service non rendu", f"{breakdown['lost_service']:.0f}",
         f"{format_number(line['pct_arrets_sautes'], 1)} % d'arrêts non desservis, courses supprimées",
         palette_kpi_tier({"skip_rate": line["pct_arrets_sautes"]}, "skip_rate")),
        ("Courses supprimées", f"{cancelled}", f"sur {trips:,} courses connues".replace(",", " "),
         "neutral"),
    ])
    render_line_map(conn, cutoff, since_ts, end_ts, line)
    _render_zone_banner(line["retard_median_s"], float(line["pct_retard_5min"]))
    v2_text = method_v2_caption(load_method_v2(conn, cutoff, since_ts, end_ts, route_id=route_id), route_id)
    if v2_text:
        st.caption(v2_text + " Voir « Méthode et lexique ».")
    _render_brief(summary, hints)

    view = st.segmented_control("Détail", LINE_VIEWS, default=LINE_VIEWS[0], key="line_view",
                                label_visibility="collapsed") or LINE_VIEWS[0]
    commune_note = (f" Fond crème : arrêts situés à {commune} "
                    f"({commune_info['n_stops']} arrêt(s) de la ligne)." if commune_info and commune_info["n_stops"] else "")
    if view in ("Retards : où ?", "Service non rendu") and not per_dir:
        st.info("L'analyse tronçon par tronçon est en cours de constitution par le collecteur."
                if not seg_ok else "Pas de données de tronçon pour cette ligne sur la période.")
    elif view in ("Retards : où ?", "Service non rendu"):
        default = main_dir if view == "Retards : où ?" else skip_dir
        chosen = _direction_choice(per_dir, default, f"line_dir_{view}")
        d = per_dir[chosen]
        ids = _commune_ids(d["profile"], commune)
        if view == "Retards : où ?":
            hot = {h["stop_id"] for h in d["origin"]["hotspots"]}
            hc_render(line_profile_chart(d["profile"], highlight_stop_id=focus_stop, hotspot_stop_ids=hot,
                                         commune_stop_ids=ids, commune_label=commune), height=360)
            labels = {"départ": "retard déjà présent dès le départ", "localisé": "retard formé sur quelques tronçons",
                      "diffus": "retard réparti sur tout le parcours", "aucun": "pas de retard notable"}
            st.caption(f"Tous les arrêts de la ligne, sur tout le réseau. {d['terminus'].capitalize()} : "
                       f"{labels[d['origin']['verdict']]}. En orange foncé, les 3 tronçons qui prennent le plus "
                       f"de retard.{commune_note}")
            if d["origin"]["hotspots"]:
                top = pd.DataFrame(d["origin"]["hotspots"])
                top["Tronçon"] = [dg.segment_path(a, b) for a, b in zip(top["from"], top["to"])]
                top["Retard pris"] = top["gain_s"].map(lambda v: format_seconds(v, signed=True))
                st.dataframe(top[["Tronçon", "commune", "Retard pris"]].rename(columns={"commune": "Commune"}),
                             hide_index=True, width="stretch")
        else:
            if cancelled:
                st.markdown(f"#### Courses supprimées ({cancelled} sur {trips:,})".replace(",", " "))
                hc_render(cancellations_chart(canc[canc["cancelled"] > 0]), height=240)
            st.markdown("#### Arrêts non desservis le long de la ligne")
            render_tier_legend("Arrêts non desservis", invert=True)
            hc_render(skip_profile_chart(d["profile"], commune_stop_ids=ids, commune_label=commune,
                                         highlight_stop_id=focus_stop), height=300)
            labels = {"extrémités": "surtout aux extrémités (prises ou fins de service en cours de ligne)",
                      "bloc": "en bloc sur une section (déviation probable)",
                      "dispersé": "dispersés le long de la ligne", "aucun": "rares"}
            st.caption(f"{d['terminus'].capitalize()} : arrêts non desservis {labels[d['skips']['verdict']]}.{commune_note}")
    elif view == "Quand ?":
        _render_line_when(conn, cutoff, since_ts, end_ts, route_id, core, hourly, peak, per_dir, main_dir, commune,
                          focus_stop)
    else:
        st.markdown("#### Perturbations signalées")
        _render_alerts(_alerts_overlapping(load_perturbation_history(conn, since_ts, end_ts, cutoff),
                                           {route_id}, rec["bad_dates"]))
        with st.expander("Répartition des écarts à l'horaire"):
            render_tier_legend("Écart à l'horaire", invert=True)
            hc_render(delay_distribution_chart(load_distribution(conn, cutoff, since_ts, route_id, end_ts)),
                      height=280)

    if not stops.empty:
        st.markdown("#### Arrêts les plus touchés de la ligne")
        st.caption("Sélectionnez une ligne du tableau pour ouvrir la fiche de l'arrêt.")
        top = stops[stops["obs"] >= MIN_OBSERVATIONS].head(10)
        if top.empty:
            st.info(f"Aucun arrêt de la ligne ne compte au moins {MIN_OBSERVATIONS} passages sur la période.")
            return
        st.session_state["_line_stop_ids"] = top["stop_id"].tolist()
        table = top[["stop_name", "direction", "commune", "cnt_gt300", "skipped", "score_fiabilite"]].copy()
        table.columns = ["Arrêt", "Direction", "Commune", "Passages > 5 min", "Arrêts non desservis", "Score / 100"]
        st.dataframe(
            table.style.map(_score_tier_style, subset=["Score / 100"]).format(
                {"Score / 100": "{:.0f}", "Passages > 5 min": fmt_int, "Arrêts non desservis": fmt_int}),
            hide_index=True, width="stretch", key="line_stops_table", on_select=lambda: _on_table_select(
                "line_stops_table", "_line_stop_ids", open_stop), selection_mode="single-row",
        )


@dataclass
class PageContext:
    conn: sqlite3.Connection
    cutoff: int
    since_ts: int | None
    end_ts: int | None
    commune: str | None
    ranking: pd.DataFrame
    visible_ranking: pd.DataFrame
    ranking_net: pd.DataFrame
    disturbed: set
    total: int
    period: Period
    prev_since_ts: int | None
    prev_end_ts: int | None
    detailed: bool = False


def _verdict(text: str) -> None:
    st.markdown(f'<div class="insight">{text}</div>', unsafe_allow_html=True)


def _tier_word(score: float) -> str:
    return tier_label(score, "score")


PRIORITY_RULE = ("À retenir : trois écarts qui dépassent la marge d'incertitude à 95 % (évolution du score, "
                 "ligne en plus forte baisse, ligne nettement la moins fiable). Arrêts à traiter en premier : les "
                 "plus de passages ratés (attendus moins assurés et à l'heure), quais d'un même arrêt comptés "
                 f"ensemble, arrêts d'au moins {MIN_OBSERVATIONS} passages.")


def map_legend_html() -> str:
    """Légende de la carte : couleur (état, dégradé continu des scores) et forme (mode)."""
    scale = (f'<span class="tl-scale"><span class="tl-bar" style="background:{score_gradient_css()}"></span>'
             '<span class="tl-ticks"><span style="left:25%">80 : fiable</span>'
             '<span style="left:62.5%">50 : problématique</span></span></span>')
    shapes = "".join(f'<span class="lg-item"><span class="lg-glyph">{mode_glyph(rt)}</span>{label}</span>'
                     for rt, label in ((0, "Tramway"), (3, "Bus"), (4, "Ferry")))
    return (f'<div class="map-legend"><div><b>Couleur : score de l\'arrêt</b>{scale}</div>'
            f'<div><b>Forme : mode</b>{shapes}</div></div>')


def render_page_territory(c: PageContext) -> None:
    territorial = load_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    territorial_network = load_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=None)
    reference = network_score(c.ranking_net)
    if territorial.empty:
        st.info("Aucun arrêt exploitable ici pour cette période. Choisissez une autre période ou tout le réseau.")
    groups = grouped_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    st.markdown('<div class="carte-anchor"></div>', unsafe_allow_html=True)
    scroll_if_requested("carte")
    st.markdown(map_legend_html(), unsafe_allow_html=True)
    selected = st.session_state.get("stop_id")
    _territorial_map(territorial, groups, c.commune, selected,
                     selected_stop_paths(c.conn, c.cutoff, c.since_ts, c.end_ts, selected),
                     routes=map_routes(c.conn, c.ranking_net), stop_routes=load_stop_route_keys(c.conn))
    if st.session_state.get("stop_id"):
        with st.container(key=f"fiche_{panel_key(st.session_state['stop_id'])}"):
            render_stop_panel(c.conn, c.cutoff, c.since_ts, c.end_ts, st.session_state["stop_id"],
                              territorial_network,
                              grouped_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=None),
                              reference, c.commune, (c.prev_since_ts, c.prev_end_ts, c.period.prev_label),
                              detailed=c.detailed, period_label=c.period.label)
    else:
        st.caption("Cliquez sur un arrêt pour ouvrir sa fiche, ou cherchez-le en haut de la page.")

    if not c.detailed:
        return
    if c.commune is None:
        with st.expander("Comparer les communes"):
            communes = load_commune_stats(c.conn, c.cutoff, c.since_ts, c.end_ts)
            if communes.empty:
                st.info("Aucune donnée par commune pour cette période.")
            else:
                worst_c, best_c = communes.iloc[0], communes.iloc[-1]
                st.markdown(
                    f'Sur {len(communes)} communes, la fiabilité s’étend de '
                    f'**{best_c["commune"]}** ({format_number(best_c["score_fiabilite"], 1)}/100) à '
                    f'**{worst_c["commune"]}** ({format_number(worst_c["score_fiabilite"], 1)}/100).'
                )
                left, right = st.columns([1.0, 1.0], gap="large")
                with left:
                    render_tier_legend("Score de fiabilité")
                    hc_render(commune_ranking_chart(communes), height=460)
                with right:
                    table = communes[["commune", "score_fiabilite", "pct_a_l_heure", "pct_retard_5min",
                                      "retard_moyen_s", "pct_arrets_sautes", "n_lignes", "observations"]].copy()
                    table.columns = ["Commune", "Score / 100", "À l'heure", "Retards > 5 min",
                                     "Retard moyen", "Arrêts non desservis", "Lignes", "Passages"]
                    st.dataframe(
                        table.style.map(_score_tier_style, subset=["Score / 100"]).format({
                            "Score / 100": number_formatter(1), "À l'heure": number_formatter(1, " %"),
                            "Retards > 5 min": number_formatter(1, " %"), "Retard moyen": lambda x: format_seconds(x),
                            "Arrêts non desservis": number_formatter(2, " %"), "Lignes": "{:.0f}", "Passages": fmt_int}),
                        width="stretch", hide_index=True, height=460)
    if not territorial.empty:
        with st.expander("Tous les arrêts du périmètre (du moins fiable au plus fiable)"):
            st.caption("Sélectionnez une ligne du tableau pour ouvrir la fiche de l'arrêt sous la carte.")
            ordered = territorial.sort_values("score_fiabilite").reset_index(drop=True)
            st.session_state["_territory_stop_ids"] = ordered["stop_id"].tolist()
            tdisp = ordered[["stop_name", "direction", "ligne", "lignes", "score_fiabilite",
                             "pct_retard_5min", "observations"]].copy()
            tdisp.columns = ["Arrêt", "Direction", "Ligne principale", "Lignes desservies", "Score / 100",
                             "Retards > 5 min", "Passages"]
            st.dataframe(
                tdisp.style.map(_score_tier_style, subset=["Score / 100"]).format(
                    {"Score / 100": number_formatter(1), "Retards > 5 min": number_formatter(1, " %"), "Passages": fmt_int}),
                width="stretch", hide_index=True, height=320, key="territory_table",
                on_select=lambda: _on_table_select("territory_table", "_territory_stop_ids", show_stop),
                selection_mode="single-row")


def lines_table(ranking: pd.DataFrame, disturbed: set) -> pd.DataFrame:
    """Tableau des lignes (vue détaillée) : score, retards, arrêts non desservis et passages."""
    return pd.DataFrame({
        "Ligne": [f"{mode_glyph(rt)} {'⚠ ' if rid in disturbed else ''}{l}"
                  for rt, rid, l in zip(ranking["route_type"], ranking["route_id"], ranking["ligne"])],
        "Score / 100": ranking["score_fiabilite"].to_numpy(),
        "Retards > 5 min": ranking["pct_retard_5min"].to_numpy(),
        "Arrêts non desservis": ranking["pct_arrets_sautes"].to_numpy(),
        "Passages": ranking["observations"].to_numpy(),
    })


def _on_ranking_click() -> None:
    state = st.session_state.get("ranking_chart") or {}
    clicked = state.get("clicked") if hasattr(state, "get") else getattr(state, "clicked", None)
    if clicked:
        show_line(clicked)


def _ranking_block(c: PageContext) -> None:
    st.markdown("#### Les 15 lignes les moins fiables")
    render_tier_legend()
    chart_data = c.visible_ranking.head(15).sort_values("score_fiabilite").copy()
    chart_data["ligne_plot"] = [f"⚠ {l}" if rid in c.disturbed else l
                                for l, rid in zip(chart_data["ligne"], chart_data["route_id"])]
    graphique_cliquable(ranking_chart(chart_data), key="ranking_chart", on_click=_on_ranking_click, height=420)


def _lines_overview(c: PageContext) -> None:
    if not c.detailed:
        _ranking_block(c)
    else:
        left, right = st.columns([1.0, 1.0], gap="large")
        with left:
            _ranking_block(c)
        with right:
            st.markdown("#### Toutes les lignes")
            table_src = c.visible_ranking.reset_index(drop=True)
            st.session_state["_lines_table_ids"] = table_src["route_id"].tolist()
            display = lines_table(table_src, c.disturbed)
            st.dataframe(
                display.style.map(_score_tier_style, subset=["Score / 100"]).format({
                    "Score / 100": "{:.0f}", "Retards > 5 min": number_formatter(1, " %"),
                    "Arrêts non desservis": number_formatter(2, " %"), "Passages": fmt_int}),
                width="stretch", hide_index=True, height=440, key="lines_table",
                on_select=lambda: _on_table_select("lines_table", "_lines_table_ids", show_line),
                selection_mode="single-row")
    if c.disturbed:
        st.caption(f"⚠ {len(c.disturbed & set(c.ranking['route_id']))} ligne(s) du périmètre font l'objet "
                   "d'une perturbation signalée par TBM sur la période, sans lien de causalité établi avec "
                   "les statistiques présentées.")
    if c.commune is not None:
        st.caption("Classement restreint aux arrêts de la commune ; la fiche porte sur toute la ligne, car "
                   "le retard subi dans une commune se forme souvent ailleurs sur le parcours.")


def render_page_lines(c: PageContext) -> None:
    line_id = st.session_state.get("line_id")
    if line_id:
        with st.container(key=f"fiche_ligne_{panel_key(line_id)}"):
            render_line_panel(c.conn, c.cutoff, c.since_ts, c.end_ts, line_id, c.ranking_net, c.disturbed, c.commune,
                              (c.prev_since_ts, c.prev_end_ts, c.period.prev_label), detailed=c.detailed,
                              period_label=c.period.label)
        with st.expander("Classement des lignes"):
            _lines_overview(c)
        return
    st.markdown("### Les lignes")
    st.caption("Cherchez votre ligne en haut de la page, ou cliquez sur une barre pour ouvrir sa fiche.")
    _lines_overview(c)


def _period_verdict(period: pd.DataFrame) -> str | None:
    table = period.assign(obs=period["observations"],
                          cnt_gt300=period["observations"] * period["pct_retard_5min"] / 100,
                          pct_gt300=period["pct_retard_5min"])
    conc = dg.concentration(table, "période")
    if conc is None:
        return ("Aucun créneau ne se détache nettement : les retards se répartissent sur l'ensemble de la "
                "journée et de la semaine.")
    return (f"Le créneau le plus difficile est <b>{html.escape(conc['label'])}</b> : "
            f"{format_number(conc['pct'], 1)} % de passages à plus de 5 min, contre {format_number(conc['rest_pct'], 1)} % le reste du temps.")


def _mode_choice(available: list[str], key: str) -> list[str]:
    """Pastilles « Ensemble » et modes ; « Ensemble » par défaut, au moins un groupe retenu."""
    options = [GROUP_ALL] + [m for m in MODE_ORDER if m in available]
    glyphs = {label: mode_glyph(rt) for rt, label in MODE_LABELS.items()}
    chosen = st.pills("Afficher", options, selection_mode="multi", default=[GROUP_ALL], key=key,
                      format_func=lambda g: g if g == GROUP_ALL else f"{glyphs.get(g, '◆')} {g}",
                      help="Ensemble du périmètre et/ou chaque mode de transport, avec sa forme de marqueur.")
    return [g for g in options if g in (chosen or [])] or [GROUP_ALL]


def _when_by_slot(c: PageContext) -> None:
    period = load_period_stats(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if period.empty:
        st.info("Aucune donnée horaire disponible sur ce périmètre pour la période.")
        return
    _verdict(_period_verdict(period))
    left, right = st.columns([1.05, 0.95], gap="large")
    with left:
        st.markdown("#### Score de fiabilité par créneau")
        render_tier_legend("Score de fiabilité")
        hc_render(period_score_chart(period), height=330)
        st.caption("Les arrêts non desservis et les courses supprimées n'ont pas d'heure dans les données : le score "
                   "d'un créneau est sa part de passages à l'heure × le service assuré de ses jours (semaine ou "
                   "week-end).")
    with right:
        st.markdown("#### Retards > 5 min par mode et créneau")
        render_tier_legend("Retards > 5 min", invert=True)
        pm = load_period_mode(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
        if pm.empty:
            st.info("Aucune donnée par mode sur ce périmètre.")
        else:
            hc_render(period_mode_chart(pm), height=330)
    st.markdown("#### Retards > 5 min selon l'heure")
    hourly = load_hourly_metrics(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if hourly.empty:
        st.info("Aucune donnée horaire sur ce périmètre pour la période.")
    else:
        groups = _mode_choice(sorted(set(hourly["groupe"])), "hourly_groups")
        render_tier_legend("Retards > 5 min", invert=True)
        hc_render(groups_hourly_chart(hourly, groups), height=330)
    if not c.detailed:
        return
    with st.expander("Lignes les moins ponctuelles d'un créneau"):
        selected_period = st.selectbox("Créneau", period["période"].tolist(), key="period_selector")
        lines = load_period_lines(c.conn, c.cutoff, c.since_ts, c.end_ts, periode=selected_period, commune=c.commune)
        if lines.empty:
            st.info(f"Aucune ligne n'atteint le seuil de {MIN_OBSERVATIONS} passages sur le créneau "
                    f"{selected_period}.")
        else:
            display = lines[["ligne", "mode", "observations", "pct_a_l_heure", "pct_retard_5min",
                             "retard_moyen_s"]].copy()
            display["ligne"] = [f"{mode_glyph(rt)} {l}" for rt, l in zip(lines["route_type"], lines["ligne"])]
            display.columns = ["Ligne", "Mode", "Passages", "À l'heure", "Retards > 5 min",
                               "Retard moyen"]
            st.dataframe(display.style.map(_score_tier_style, subset=["À l'heure"]).format({
                "Passages": fmt_int, "À l'heure": number_formatter(1, " %"),
                "Retards > 5 min": number_formatter(1, " %"), "Retard moyen": lambda x: format_seconds(x)}),
                width="stretch", hide_index=True, height=320)


def _when_over_time(c: PageContext) -> None:
    daily = load_daily_metrics(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if daily.empty:
        st.info("Aucune donnée quotidienne sur ce périmètre pour la période.")
        return
    now = period_totals(load_daily_metrics(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune, by_day=False))
    before = (period_totals(load_daily_metrics(c.conn, c.cutoff, c.prev_since_ts, c.prev_end_ts, commune=c.commune,
                                               by_day=False))
              if c.prev_since_ts is not None else None)
    if before is None:
        missing = (f"pas de données sur {c.period.prev_label}" if c.period.prev_label
                   else "pas de période de comparaison")
        _verdict(f'Sur {html.escape(c.period.label)}, le score de fiabilité est de '
                 f'<b>{format_number(now, 1)} / 100</b> ({_tier_word(now)} ; {missing}).')
    else:
        delta = now - before
        arrow, color = ("▲", INK) if delta >= 0 else ("▼", INK)
        _verdict(f'Sur {html.escape(c.period.label)}, le score de fiabilité est de <b>{format_number(now, 1)} / 100</b> '
                 f'({_tier_word(now)}), soit <span style="color:{color}"><b>{arrow} '
                 f'{format_number(abs(delta), 1)} point(s)</b></span> par rapport à {html.escape(c.period.prev_label)}.')
    st.markdown("#### Jour par jour")
    pick, _ = st.columns([1, 1.6])
    with pick:
        metric = st.selectbox("Indicateur", list(TREND_METRICS), format_func=lambda m: TREND_METRICS[m][0],
                              key="trend_metric")
    groups = _mode_choice(sorted(set(daily["groupe"])), "trend_groups")
    render_tier_legend(TREND_METRICS[metric][0], invert=TREND_METRICS[metric][3] != "score")
    hc_render(groups_trend_chart(daily, metric, groups), height=340)
    st.markdown("#### Lignes qui se dégradent ou s'améliorent")
    prog = load_engagement_progression(c.conn, c.cutoff, c.since_ts, c.end_ts, c.prev_since_ts, c.prev_end_ts,
                                       commune=c.commune)
    if prog.empty:
        st.info(f"Comparaison impossible : aucune ligne ne cumule au moins {MIN_OBSERVATIONS} passages sur "
                f"{c.period.label} et sur {c.period.prev_label or 'une période précédente'}.")
        return
    worst, best = prog.iloc[0], prog.iloc[-1]
    st.markdown(f"Par rapport à {c.period.prev_label}, la plus forte dégradation concerne la **ligne "
                f"{worst['ligne']}** ({format_number(worst['delta_score'], 1, signed=True)} points de score), la "
                f"meilleure progression la **ligne {best['ligne']}** "
                f"({format_number(best['delta_score'], 1, signed=True)} points).")
    hc_render(engagement_progression_chart(prog), height=330)
    if not c.detailed:
        return
    with st.expander("Détail par ligne"):
        table = prog[["ligne", "mode", "score_fiabilite_prev", "score_fiabilite", "delta_score",
                      "pct_a_l_heure", "pct_arrets_sautes", "observations"]].copy()
        table.columns = ["Ligne", "Mode", "Score avant", "Score", "Évolution", "Ponctualité", "Arrêts non desservis",
                         "Passages"]
        st.dataframe(table.style.map(_delta_style, subset=["Évolution"]).format({
            "Score avant": number_formatter(1), "Score": number_formatter(1),
            "Évolution": number_formatter(1, " pts", signed=True), "Ponctualité": number_formatter(1, " %"),
            "Arrêts non desservis": number_formatter(2, " %"), "Passages": fmt_int}),
            width="stretch", hide_index=True, height=330)
        st.caption(f"« Score avant » : {c.period.prev_label} ; les autres colonnes : {c.period.label}. Seuil : "
                   f"{MIN_OBSERVATIONS} passages sur chacune des deux périodes.")


def render_page_when(c: PageContext) -> None:
    st.markdown("### Quand les problèmes surviennent-ils ?")
    view = st.segmented_control("Vue", ["Selon le créneau", "Dans le temps"], default="Selon le créneau",
                                key="when_view", label_visibility="collapsed") or "Selon le créneau"
    if view == "Selon le créneau":
        _when_by_slot(c)
    else:
        _when_over_time(c)


def render_page_network(c: PageContext) -> None:
    st.markdown("### Les modes de transport, pour situer un problème")
    st.markdown("#### Carte de risque des lignes")
    hc_render(scatter_chart(c.visible_ranking), height=430)
    st.caption("Chaque bulle est une ligne. Couleur : son score de fiabilité ; forme : son mode (● tramway, ■ bus, "
               "▲ ferry) ; taille : son nombre de passages. Traits pointillés : seuils de "
               f"{RISK_MEDIAN_S:.0f} s de retard médian et {RISK_PCT_GT300:.0f} % de passages à plus de 5 min.")
    mode_stats = load_mode_stats(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if mode_stats.empty:
        st.warning("Aucune donnée exploitable par mode.")
        return
    st.markdown("#### Chaque mode en un coup d'œil")
    worst_mode = mode_stats.sort_values("pct_a_l_heure").iloc[0]
    _verdict(f'Le mode le moins ponctuel du périmètre est le <b>{html.escape(str(worst_mode["mode"]).lower())}</b> '
             f'({format_number(worst_mode["pct_a_l_heure"], 1)} % de passages à l’heure). Tram, bus et bateau n’ont '
             f'pas les mêmes contraintes : les comparer aide à distinguer un problème de ligne d’un problème de mode.')
    card_html = '<div style="display:flex;gap:1rem;margin-bottom:.2rem;flex-wrap:wrap">'
    for r in mode_stats.itertuples():
        ponct_color = score_color(r.pct_a_l_heure)
        card_html += (
            f'<div class="mode-card" style="border-left-color:{ponct_color}">'
            f'<div class="kpi-label">{mode_glyph(r.route_type)} {r.mode}</div>'
            f'<div class="kpi-value">{format_number(r.pct_a_l_heure, 1)} %</div>'
            f'<div class="kpi-sublabel">à l’heure · {fmt_int(r.observations)} passages · '
            f'retard médian {format_seconds(r.retard_median_s, signed=True)} · '
            f'{format_number(r.pct_retard_5min, 1)} % &gt; 5 min</div></div>'
        )
    st.markdown(card_html + '</div>', unsafe_allow_html=True)
    st.markdown("#### Comparaison des indicateurs")
    hc_render(mode_comparison_chart(mode_stats), height=330)
    with st.expander("Répartition des écarts à l'horaire"):
        distribution = load_distribution(c.conn, c.cutoff, c.since_ts, end_ts=c.end_ts, commune=c.commune)
        if not distribution.empty:
            render_tier_legend("Écart à l'horaire", invert=True)
            hc_render(delay_distribution_chart(distribution), height=280)
    with st.expander("Tableau détaillé des lignes"):
        display = c.visible_ranking[["ligne", "mode", "score_fiabilite", "pct_a_l_heure", "retard_moyen_s",
                                     "retard_median_s", "pct_retard_5min", "pct_arrets_sautes",
                                     "observations"]].copy()
        display["ligne"] = [f"⚠ {l}" if rid in c.disturbed else l
                            for l, rid in zip(display["ligne"], c.visible_ranking["route_id"])]
        display.columns = ["Ligne", "Mode", "Score / 100", "À l'heure", "Retard moyen (s)",
                           "Retard médian (s)", "Retards > 5 min", "Arrêts non desservis", "Passages"]
        st.dataframe(display.style.map(_score_tier_style, subset=["Score / 100"]).format({
            "Score / 100": number_formatter(1), "À l'heure": number_formatter(1, " %"),
            "Retard moyen (s)": "{:.0f}", "Retard médian (s)": "{:.0f}", "Retards > 5 min": number_formatter(1, " %"),
            "Arrêts non desservis": number_formatter(2, " %"), "Passages": fmt_int}),
            width="stretch", hide_index=True, height=330)
    with st.expander("Tableau par mode"):
        table = mode_stats[["mode", "observations", "pct_a_l_heure", "pct_retard_5min", "pct_avance_1min",
                            "retard_moyen_s", "retard_median_s", "pct_arrets_sautes"]].copy()
        table.columns = ["Mode", "Passages", "À l'heure", "Retards > 5 min", "En avance > 1 min",
                         "Retard moyen (s)", "Retard médian (s)", "Arrêts non desservis"]
        st.dataframe(table.style.format({
            "Passages": fmt_int, "À l'heure": number_formatter(1, " %"),
            "Retards > 5 min": number_formatter(1, " %"), "En avance > 1 min": number_formatter(1, " %"),
            "Retard moyen (s)": "{:.0f}", "Retard médian (s)": "{:.0f}",
            "Arrêts non desservis": number_formatter(2, " %")}), width="stretch", hide_index=True, height=220)


def render_page_perturbations(c: PageContext) -> None:
    st.markdown("### Perturbations sur la période")
    st.markdown('<div class="section-note">Perturbations diffusées par TBM dans le flux GTFS-RT Service '
                'Alerts. La cause indiquée par TBM (quasi toujours « inconnue ») n’est pas fiable et n’est pas '
                'affichée ; le titre, la description, les lignes concernées et la période restent en revanche '
                'exploitables.</div>', unsafe_allow_html=True)
    now_ts = int(datetime.now().timestamp())
    alerts_now = load_active_alerts(c.conn, now_ts)
    if c.commune is not None and not alerts_now.empty:
        commune_routes = load_commune_routes(c.conn, c.commune)
        alerts_now = alerts_now[(alerts_now["route_id"] == "") | (alerts_now["route_id"].isin(commune_routes))]
    history = load_perturbation_history(c.conn, c.since_ts, c.end_ts, c.cutoff, commune=c.commune)
    lignes_actives = int(alerts_now["route_id"].nunique()) if not alerts_now.empty else 0
    jours_lignes = int(history["jours_couverts"].sum()) if not history.empty else 0
    render_kpis([
        ("Lignes actuellement perturbées", str(lignes_actives), None, "negatif" if lignes_actives > 0 else "positif"),
        ("Jours-lignes cumulés perturbés", f"{jours_lignes:,}".replace(",", " "), None, "neutral"),
        ("Perturbations sur la période", str(len(history)), None, "neutral"),
    ])
    if c.commune is not None:
        st.caption("Périmètre restreint : lignes desservant des arrêts de la commune sélectionnée.")
    st.markdown("#### Historique des perturbations (période sélectionnée)")
    if history.empty:
        st.info("Aucune perturbation active sur tout ou partie de la période sélectionnée.")
    else:
        st.caption("Période effective tronquée à l'intersection avec la période sélectionnée ; lignes classées "
                   "par nombre total de jours perturbés décroissant.")
        search = st.text_input("Filtrer (ligne, titre, description)", key="pert_search")
        q = (search or "").strip().lower()
        by_line: dict[str, list] = {}
        for r in history.itertuples():
            hay = f"{r.ligne} {r.header_text} {r.description_text}".lower()
            if q and q not in hay:
                continue
            by_line.setdefault(r.ligne, []).append(r)
        if not by_line:
            st.info("Aucune perturbation ne correspond à ce filtre.")
        else:
            order = sorted(by_line.items(), key=lambda kv: sum(x.jours_couverts for x in kv[1]), reverse=True)
            for ligne, items in order:
                n = len(items)
                jours = int(sum(x.jours_couverts for x in items))
                acc = (f"**Ligne {ligne}** — {n} alerte{'s' if n > 1 else ''} · "
                       f"{jours} jour{'s' if jours > 1 else ''} cumulé{'s' if jours > 1 else ''}")
                inner = items if n <= 6 else sorted(items, key=lambda x: x.jours_couverts, reverse=True)[:6]
                with st.expander(acc):
                    for x in inner:
                        st.markdown(
                            f"**{html.escape(x.header_text or '')}**  \n"
                            f"<span style='color:{INK_MUTED}'>{x.debut_effectif} → "
                            f"{x.fin_effective} · {x.jours_couverts} j</span>",
                            unsafe_allow_html=True,
                        )
                        st.write(x.description_text or "(description non fournie)")
                        st.markdown("")
                    if len(items) > 6:
                        st.caption(f"+ {len(items) - 6} autre(s) alerte(s) sur cette ligne.")
    st.markdown(f'<div class="section-note">{CAUTION_TEXT}</div>', unsafe_allow_html=True)


def _render_method_v2(c: PageContext) -> None:
    st.markdown("#### Le score de fiabilité de la période")
    v2 = load_method_v2(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if not v2["disponible"]:
        st.info("Score indisponible pour cette période (historique en cours de calcul ou aucun jour complet).")
        return
    render_kpis([
        ("Score de fiabilité", _with_margin(v2["score"], v2["marge"]), "assurés et à l'heure / prévus",
         palette_kpi_tier({"fiability": v2["score"]}, "fiability")),
        ("À l'heure", f"{_with_margin(v2['ponctualite'], v2['marge_ponctualite'])} %",
         "de 1 min d'avance à 5 min de retard", palette_kpi_tier({"ponctualite": v2["ponctualite"]}, "ponctualite")),
        ("Service assuré", f"{format_number(v2['service'], 1)} %", "ni course supprimée, ni arrêt non desservi", "neutral"),
        ("Départs en avance", f"{format_number(v2['avance'], 1)} %", "plus d'une minute", "neutral"),
    ])
    discarded = ", ".join(f"{r} ({z:.0f} % de retards nuls)" for r, z in sorted(v2["lignes_ecartees"].items()))
    st.caption(f"{v2['jours']} jour(s) évalué(s). Jours écartés (collecte incomplète) : {_short_days(v2['jours_exclus'])}. "
               f"Jours à collecte dégradée (conservés) : {_short_days(v2['jours_degrades'])}. "
               f"Lignes écartées (temps réel douteux) : {discarded or 'aucune'}. "
               f"Passages de trajets sans temps réel, écartés de la ponctualité et des retards : "
               f"{format_number(v2.get('sans_temps_reel', 0), 0)}. La marge (±) est l'intervalle "
               "de confiance à 95 %, calculé sur les écarts d'un jour à l'autre.")


def _render_method(c: PageContext) -> None:
    st.markdown("### Ce que mesure ce tableau de bord")
    st.markdown("Les données viennent des flux GTFS-RT **TripUpdates** TBM. Une observation est considérée "
                "stabilisée après 20 minutes hors du flux temps réel ; cela évite d'interpréter comme final un "
                "retard qui peut encore évoluer.")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**Score de fiabilité**  \nSur 100 passages prévus, le nombre de passages **assurés et à "
                "l'heure**. Un passage est à l'heure s'il part entre 1 min d'avance et 5 min de retard ; il est "
                "assuré si l'arrêt est desservi et la course n'est pas supprimée (une course de remplacement "
                "ajoutée le même jour compense une suppression). Score = service assuré × part à l'heure.")
    c2.markdown("**Jours et lignes écartés**  \nLes jours où la collecte est incomplète, les lignes au temps "
                "réel douteux (au moins un quart des retards exactement nuls) et les lignes Flex' (à la "
                "demande) ne sont comptés dans **aucun** chiffre du tableau de bord ni des rapports. Les "
                "**trajets sans temps réel** (au moins deux départs relevés, tous exactement à l'heure à la "
                "seconde près) sont écartés de la ponctualité et des retards, mais comptés dans le service assuré.")
    c3.markdown(f"**Seuil d'échantillon**  \nUne ligne n'apparaît dans les classements et graphiques que si "
                f"elle totalise au moins **{MIN_OBSERVATIONS} passages** sur la période. Cela écarte les lignes "
                f"trop peu observées, dont les chiffres ne seraient pas statistiquement fiables.")
    st.markdown("#### Lire les fiches arrêt et ligne")
    st.markdown(
        "- **Retard déjà présent en arrivant / retard pris sur le tronçon** : pour chaque voyage, le retard "
        "à un arrêt est comparé à celui de l'arrêt précédent du même véhicule. Si le retard était déjà là, "
        "il est *importé* de l'amont ; s'il apparaît entre les deux arrêts, il naît sur ce tronçon.\n"
        f"- **Jour dégradé** : jour où au moins {RISK_PCT_GT300:.0f} % des passages ont plus de 5 min de "
        "retard. Un problème est *ponctuel* s'il touche moins d'un quart des jours, *chronique* au-delà de "
        "la moitié.\n"
        "- **Points perdus** : le score part de 100. Le service non rendu (arrêts non desservis, courses "
        "supprimées) et les horaires non tenus (retard de plus de 5 min, avance de plus d'une minute) en "
        "retirent ; la fiche ligne dit lequel des deux pèse le plus.\n"
        "- **Évolution** : le score de la période comparé à celui de la période précédente (le mois précédent "
        "pour un mois, la même durée juste avant pour les autres périodes).\n"
        "- **À retenir et arrêts à traiter** : " + PRIORITY_RULE + "\n"
        "- **Pistes** : ce sont des indices à confirmer sur le terrain, jamais des conclusions.\n"
        "- **Créneaux** : les arrêts non desservis et les courses supprimées n'ont pas d'heure dans les "
        "données ; le score d'un créneau est sa part de passages à l'heure × le service assuré de ses jours "
        "(semaine ou week-end)."
    )
    _render_method_v2(c)
    st.markdown(
        "<div class='section-note'><b>Pourquoi un tram peut-il avoir des arrêts non desservis ?</b> "
        "Un événement <code>SKIPPED</code> signifie que le véhicule ne dessert pas un arrêt alors "
        "que le trajet continue. Deux situations courantes l'expliquent :<br/>"
        "• <b>Prise / rendu de service en cours de ligne</b> : le tram ne dessert pas le terminus ou "
        "les premiers arrêts (départ ou fin de service plus loin sur la ligne, retour dépôt, rotation). "
        "Seuls quelques arrêts ne sont pas desservis, le reste du trajet circule normalement.<br/>"
        "• <b>Trajet entièrement annulé</b> : tous les arrêts de la course sont marqués "
        "<code>SKIPPED</code>. Il s'agit d'une annulation de course, pas d'un saut d'arrêt au sens "
        "strict : le véhicule ne circule pas du tout.</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"Fenêtre analysée : {fmt_int(c.total)} passages programmés stabilisés ; dernier point retenu "
               f"le {format_date(c.cutoff)}.")


def _render_open_data(c: PageContext) -> None:
    st.markdown("### Données ouvertes (CSV)")
    st.markdown('<div class="section-note">Les agrégats (pas les observations brutes) sont exportés en CSV — '
                'format stable, sans donnée nominative. Le collecteur régénère ces fichiers sur la période '
                '(`src/scripts/export_open_data.py`) ; les boutons ci-dessous produisent le même export pour la '
                'période sélectionnée.</div>', unsafe_allow_html=True)
    open_specs = [
        ("lignes_journalier", "lignes-journalier.csv", "Journalier par ligne",
         "Ponctualité, retards et arrêts non desservis par ligne et par jour (avec l'histogramme des écarts)."),
        ("arrets_journalier", "arrets-journalier.csv", "Journalier par arrêt",
         "Détail par arrêt, ligne et jour, avec commune, direction et coordonnées."),
        ("horaire", "horaire.csv", "Horaire par ligne", "Retards par tranche horaire et par ligne."),
        ("communes_journalier", "communes-journalier.csv", "Journalier par commune",
         "Agrégats par commune et par jour (code Insee, nombre de lignes)."),
    ]
    open_cols = st.columns(2)
    for i, (name, fname, title, desc) in enumerate(open_specs):
        df = load_open_dataset(c.conn, name, c.cutoff, c.since_ts, c.end_ts)
        with open_cols[i % 2]:
            st.markdown(f"**{title}**  \n{desc}  \n{len(df):,} lignes pour cette période".replace(",", " "))
            st.download_button("Télécharger le CSV", data=df.to_csv(index=False).encode("utf-8-sig"),
                               file_name=fname, mime="text/csv", key=f"open_{name}")


def _render_collection(c: PageContext) -> None:
    stats = load_collection_stats(c.conn)
    st.markdown("### Suivi de la collecte")
    st.markdown('<div class="section-note">Volume et continuité des données collectées via les flux GTFS-RT '
                'TripUpdates. Les « observations brutes » comptent toutes les lignes reçues du flux ; les '
                '« passages analysés » sont les passages programmés (SCHEDULED) dont le retard est connu, comptés '
                'dans les agrégats quotidiens ; les « courses suivies » sont les courses dont le flux a publié le '
                'statut, y compris les courses supprimées. Les arrêts non desservis (SKIPPED) sont suivis à part.</div>',
                unsafe_allow_html=True)
    render_kpis([
        ("Observations (brutes)", f"{stats['total']:,}".replace(",", " "), None, "neutral"),
        ("Passages analysés", f"{stats['analysed']:,}".replace(",", " "), "retard connu", "neutral"),
        ("Courses suivies", f"{stats['trajets']:,}".replace(",", " "), None, "neutral"),
        ("Première date", *_date_and_time(stats["first_ts"]), "neutral"),
        ("Dernière date", *_date_and_time(stats["last_ts"]), "neutral"),
    ])
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("#### Observations par minute")
        last_ts = stats["last_ts"]
        if not last_ts:
            st.info("Aucune donnée disponible.")
        else:
            c_end_ts = (int(last_ts) // 60) * 60
            minutely = load_collection_minutely(c.conn, c_end_ts - 7 * 24 * 3600, c_end_ts)
            if minutely.empty:
                st.info("Aucune donnée pour cette période.")
            else:
                hc_render(collection_minutely_chart(minutely), height=340, use_stock=True)
    with right:
        st.markdown(f"#### Répartition horaire ({COLLECTION_HOURLY_DAYS} derniers jours)")
        hourly = stats["hourly"]
        if hourly.empty:
            st.info("Aucune donnée horaire.")
        else:
            hc_render(hourly_distribution_chart(hourly), height=280)


SCORE_EXPLAINED = (
    "Le score répond à une question : **sur 100 passages prévus, combien ont eu lieu, et à l'heure ?**\n\n"
    "1. **Passages prévus** : chaque départ prévu d'un bus, d'un tram ou d'un bateau à un arrêt, y compris "
    "les arrêts où le véhicule ne s'est pas arrêté et les passages des courses supprimées (une course "
    "supprimée puis remplacée le même jour sur la ligne ne compte pas).\n"
    "2. **Passages assurés** : les passages prévus, moins les arrêts non desservis et les passages des "
    "courses supprimées.\n"
    "3. **À l'heure** : un passage part entre 1 minute d'avance et 5 minutes de retard sur l'horaire publié.\n"
    "4. **Score** = 100 × passages assurés et à l'heure ÷ passages prévus. Les passages assurés et à l'heure "
    "sont les passages assurés multipliés par la part des passages mesurés qui sont à l'heure.\n\n"
    "**Ce qui n'est pas compté** : les jours où la collecte est incomplète, les lignes dont le temps réel est "
    "douteux et les lignes Flex' (à la demande). Les trajets sans temps réel comptent dans les passages "
    "assurés, mais pas dans la ponctualité.\n\n"
    "**Comment on additionne** : le calcul se fait par ligne et par jour (par arrêt, ligne et jour pour un "
    "arrêt) ; on additionne ensuite les passages sur la période et le territoire choisis, puis on calcule le "
    "score. On ne fait jamais la moyenne de plusieurs scores.\n\n"
    "**Lecture** : fiable à partir de 80, à surveiller de 50 à 80, problématique sous 50. La **marge** (±) dit "
    "de combien le score peut varier d'un jour à l'autre : un écart plus petit que la marge peut venir du hasard."
)


def _render_lexique(c: PageContext) -> None:
    st.markdown("### Lexique")
    st.markdown("Les mots utilisés dans le tableau de bord et les rapports, en langage courant.")
    with st.container(key="score_explained"):
        st.markdown("#### Comment se calcule le score de fiabilité")
        st.markdown(SCORE_EXPLAINED)
    focus = st.session_state.get("lexique_focus")
    items = "".join(
        f'<div class="lexique-item{" lexique-focus" if lexique_slug(term) == focus else ""}" id="{lexique_slug(term)}">'
        f'<b>{html.escape(term)}</b> : {html.escape(definition)}</div>'
        for term, definition in LEXIQUE)
    st.markdown(f'<div class="lexique">{items}</div>', unsafe_allow_html=True)
    if focus:
        st.html(f"<script>setTimeout(()=>{{const el=document.getElementById({json.dumps(focus)});"
                "if(el){el.scrollIntoView({behavior:'smooth',block:'center'});}},400);</script>",
                unsafe_allow_javascript=True)


def render_page_data(c: PageContext) -> None:
    views = {"Lexique": _render_lexique, "Méthode": _render_method, "Données ouvertes": _render_open_data,
             "Suivi de la collecte": _render_collection}
    view = st.segmented_control("Vue", list(views), default="Lexique", key="data_view",
                                label_visibility="collapsed") or "Lexique"
    views[view](c)


REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports" / "output"


def list_reports(root: Path) -> dict:
    """Rapports PDF publiés, du mois le plus récent au plus ancien.

    {"AAAA-MM": {"reseau": chemin ou None, "communes": {dossier de la commune: chemin}}} ; seuls les mois
    qui ont au moins un PDF sont listés.
    """
    if not root.is_dir():
        return {}
    out = {}
    months = sorted((d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}", d.name)),
                    reverse=True)
    for month_dir in months:
        network = sorted((month_dir / "reseau").glob("*/*.pdf"))
        communes = {pdf.parent.name: pdf for pdf in sorted((month_dir / "communes").glob("*/*.pdf"))}
        if network or communes:
            out[month_dir.name] = {"reseau": network[0] if network else None, "communes": communes}
    return out


def month_name(month: str) -> str:
    year, number = month.split("-")
    return f"{MONTHS_FR[int(number) - 1].capitalize()} {year}"


def _download_pdf(label: str, path: Path, key: str) -> None:
    st.download_button(label, data=path.read_bytes(), file_name=path.name, mime="application/pdf", key=key,
                       width="stretch")


CONTENTION_DAYS = {"semaine": "Semaine type", "samedi": "Samedi type", "dimanche": "Dimanche type",
                   "date": "Une date précise"}
CONTENTION_LOW_SERVICE = 0.7
WEEKDAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
TRONCONS_TIMER = Path("/etc/systemd/system/urban-vision-troncons.timer")


def contention_empty_message(timer_installed: bool) -> str:
    """Message de la carte des tronçons jamais calculée ; sans le timer de nuit (poste local), la commande à lancer."""
    text = "La carte n'est pas encore calculée : elle l'est chaque nuit à 3 h 40."
    if timer_installed:
        return text
    return (f"{text}\n\nSur ce poste, le calcul de nuit n'est pas installé. Lancez-le depuis la racine du projet, "
            "puis rechargez la page (quelques minutes, davantage au premier passage) :\n\n"
            "```bash\n.venv/bin/python src/scripts/troncons.py\n```")


@st.cache_data(ttl=3600)
def load_troncons_meta(_conn) -> dict:
    """Période, date de calcul et qualité des lignes de la carte des tronçons ({} si jamais calculée)."""
    try:
        return {k: json.loads(v) for k, v in _conn.execute("SELECT cle, valeur FROM troncons_meta")}
    except sqlite3.Error:
        return {}


@st.cache_data(ttl=3600)
def load_troncons(_conn, jour_type: str, computed_at: int) -> dict:
    """Géométrie, cases du jour type, lignes et noms des tronçons, tracés du tram (tables troncons_*)."""
    geo = pd.read_sql_query("SELECT from_stop_id, to_stop_id, route_type, coords FROM troncons_geo", _conn)
    geo["coords"] = geo["coords"].map(json.loads)
    cells = pd.read_sql_query(
        "SELECT heure, from_stop_id, to_stop_id, statut, palier, mediane_s, mediane_pct, passages, part_2min "
        "FROM troncons_carte WHERE jour_type = ?", _conn, params=(jour_type,))
    lines: dict = {}
    route_ids: dict = {}
    for a, b, route_id, name in _conn.execute(
            "SELECT t.from_stop_id, t.to_stop_id, t.route_id, COALESCE(r.route_short_name, t.route_id) "
            "FROM troncons_lignes t LEFT JOIN routes r ON r.route_id = t.route_id ORDER BY t.passages DESC"):
        lines.setdefault((a, b), []).append(str(name))
        route_ids.setdefault((a, b), []).append(route_id)
    tram = [{"r": r, "p": json.loads(c)} for r, c in _conn.execute(
        "SELECT s.route_id, s.coords FROM route_shapes s JOIN routes r ON r.route_id = s.route_id "
        "WHERE r.route_type = 0")]
    try:
        tram_rows = pd.read_sql_query(
            "SELECT heure, route_id, direction_id, terminus, perte_s, prevu_s, trajets FROM troncons_tram "
            "WHERE jour_type = ?", _conn, params=(jour_type,))
    except (sqlite3.Error, pd.errors.DatabaseError):
        tram_rows = pd.DataFrame(columns=["heure", "route_id", "direction_id", "terminus", "perte_s", "prevu_s",
                                          "trajets"])
    tram_rows["terminus"] = tram_rows["terminus"].map(lambda t: t.title() if t.isupper() else t)
    tram_names = dict(_conn.execute(
        "SELECT route_id, COALESCE(route_short_name, route_id) FROM routes WHERE route_type = 0"))
    return {"geo": geo, "cells": cells, "lines": lines, "route_ids": route_ids,
            "names": dict(_conn.execute("SELECT stop_id, stop_name FROM stops")),
            "communes": dict(_conn.execute("SELECT stop_id, commune_name FROM stop_municipalities")),
            "tram": tram, "tram_lines": tram_lines_payload(tram_rows, tram_names)}


@st.cache_data(ttl=3600)
def load_troncons_days(_conn, computed_at: int) -> dict:
    """Dates calculées par `troncons.py` (table troncons_jours_faits) et leur nombre de passages gardés."""
    try:
        return dict(_conn.execute("SELECT date_service, passages FROM troncons_jours_faits ORDER BY 1"))
    except sqlite3.Error:
        return {}


@st.cache_data(ttl=3600, max_entries=20)
def load_troncons_date(_conn, day: str, computed_at: int, good: tuple) -> pd.DataFrame:
    """Cases d'une date précise, heure par heure (`troncons.date_cells`), gardées en cache une heure."""
    return troncons.date_cells(_conn, day, set(good))


def contention_dates(days: dict, since: str, until: str, excluded: list) -> list[str]:
    """Dates proposées : jours calculés de la période, hors jours exclus (collecte incomplète)."""
    return [d for d in days if since <= d < until and d not in set(excluded)]


def date_coverage_reason(day: str, passages: dict, excluded: list) -> str | None:
    """Raison d'un jour de peu de service, ajoutée à l'avertissement d'une date : jour férié, week-end, ou jour de
    semaine sous CONTENTION_LOW_SERVICE des passages d'un jour de semaine médian ; None pour un jour ordinaire."""
    weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
    holiday = ind.public_holiday(day)
    if holiday:
        return f"C'est un jour férié ({holiday}) : peu de bus circulent."
    if weekday >= 5:
        return f"C'est un {WEEKDAY_NAMES[weekday]} : peu de bus circulent."
    normal = [n for d, n in passages.items()
              if d not in set(excluded) and datetime.strptime(d, "%Y-%m-%d").weekday() < 5
              and not ind.public_holiday(d)]
    if not normal or day not in passages:
        return None
    reference = float(np.median(normal))
    share = passages[day] / reference if reference else 1.0
    if share < CONTENTION_LOW_SERVICE:
        return (f"Le service était plus faible qu'un jour de semaine habituel ({share * 100:.0f} % "
                "des passages).")
    return None


def date_label(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{WEEKDAY_NAMES[d.weekday()]} {d:%d/%m/%Y}"


def contention_scope(commune: str | None, route_id: str | None, line_name: str | None, stop_id: str | None,
                     stop_name: str | None) -> dict:
    """Périmètre de la carte des tronçons d'après le fil d'Ariane, avec ses textes.

    `place` complète le verdict (« à Mérignac », « sur la ligne 23 à Mérignac », « autour de l'arrêt
    Gare ») ; `label` est le nom affiché sur la carte, le plus précis des trois.
    """
    parts = []
    if stop_id:
        parts.append(f"autour de l'arrêt {stop_name or stop_id}")
    elif route_id:
        parts.append(f"sur la ligne {line_name or route_id}")
    if commune:
        parts.append(f"à {commune}")
    label = stop_name if stop_id else (f"Ligne {line_name or route_id}" if route_id else commune)
    return {"commune": commune, "route_id": route_id, "stop_id": stop_id,
            "place": " ".join(parts) or "sur le réseau", "label": label or ""}


def render_page_contention(c: PageContext) -> None:
    meta = load_troncons_meta(c.conn)
    choice, pick, _ = st.columns([0.26, 0.26, 0.48], vertical_alignment="bottom")
    if not meta.get("calcule_le"):
        st.info(contention_empty_message(TRONCONS_TIMER.exists()))
        return
    with choice:
        jour = st.selectbox("Jour", list(CONTENTION_DAYS), format_func=CONTENTION_DAYS.get, key="contention_jour",
                            label_visibility="collapsed")
    since = datetime.strptime(meta["depuis"], "%Y-%m-%d")
    until = datetime.strptime(meta["jusqu_a"], "%Y-%m-%d") - timedelta(days=1)
    day = None
    if jour == "date":
        passages = load_troncons_days(c.conn, int(meta["calcule_le"]))
        excluded = meta.get("jours_exclus", [])
        dates = contention_dates(passages, meta["depuis"], meta["jusqu_a"], excluded)
        if not dates:
            st.info("Aucune date calculée sur la période scolaire.")
            return
        with pick:
            picked = st.date_input("Date", value=datetime.strptime(dates[-1], "%Y-%m-%d").date(),
                                   min_value=datetime.strptime(dates[0], "%Y-%m-%d").date(),
                                   max_value=datetime.strptime(dates[-1], "%Y-%m-%d").date(),
                                   format="DD/MM/YYYY", key="contention_date", label_visibility="collapsed")
        day = picked.strftime("%Y-%m-%d")
        if day not in dates:
            st.warning(f"Pas de carte pour le {date_label(day)} : "
                       + ("la collecte de ce jour est incomplète, il est exclu de toutes les analyses."
                          if day in excluded else "ce jour n'a pas été calculé.")
                       + " Choisissez une autre date.")
            return
    when = (f"le {date_label(day)}, heure par heure" if day
            else f"période scolaire du {since:%d/%m} au {until:%d/%m/%Y}")
    st.markdown(
        '<div class="section-note contention-note">Temps que les bus perdent entre deux arrêts (circulation, feux '
        f"ou montée des voyageurs), {when}.</div>",
        unsafe_allow_html=True)
    data = load_troncons(c.conn, ind.day_type(day) if day else jour, int(meta["calcule_le"]))
    route_id, stop_id = st.session_state.get("line_id"), st.session_state.get("stop_id")
    line_name = None
    if route_id:
        row = c.conn.execute("SELECT COALESCE(route_short_name, route_id) FROM routes WHERE route_id = ?",
                             (route_id,)).fetchone()
        line_name = row[0] if row else route_id
    scope = contention_scope(c.commune, route_id, line_name, stop_id, data["names"].get(stop_id) if stop_id else None)
    measurable = {r for r, q in meta.get("lignes", {}).items() if q.get("ok")}
    if route_id and route_id not in measurable and route_id not in {t["r"] for t in data["tram"]}:
        scope["unmeasured"] = True
    cells, tram_lines, date_info = data["cells"], data["tram_lines"], None
    if day:
        reason = date_coverage_reason(day, passages, meta.get("jours_exclus", []))
        cells = load_troncons_date(c.conn, day, int(meta["calcule_le"]), tuple(sorted(measurable)))
        tram_lines = {}
        date_info = {"day": day, "label": date_label(day), "reason": reason}
    payload = troncons_payload(data["geo"], cells, data["lines"], data["names"], data["communes"],
                               data["tram"], scope, data["route_ids"], measurable, tram_lines, date_info=date_info)
    carte_troncons(payload, key="carte_troncons")


def render_page_reports(c: PageContext) -> None:
    st.markdown("### Rapports mensuels")
    st.markdown("Chaque début de mois, un rapport PDF est produit pour l'ensemble du réseau et pour chacune "
                "des 28 communes de Bordeaux Métropole : une page de synthèse à lire en premier, puis le détail "
                "par ligne, les graphiques et la méthode.")
    reports = list_reports(REPORTS_DIR)
    if not reports:
        st.info("Aucun rapport n'est encore disponible.")
        return
    month = st.selectbox("Mois", list(reports), format_func=month_name, key="report_month")
    entry = reports[month]
    names = {report_slug(name): name for name in load_communes(c.conn)}
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("#### Réseau complet")
        if entry["reseau"] is not None:
            _download_pdf(f"Télécharger le rapport réseau — {month_name(month)}", entry["reseau"], "dl_reseau")
        else:
            st.info("Pas de rapport réseau pour ce mois.")
    with right:
        st.markdown("#### Une commune")
        slugs = sorted(entry["communes"], key=lambda sl: names.get(sl, sl))
        if not slugs:
            st.info("Pas de rapport communal pour ce mois.")
        else:
            preferred = report_slug(c.commune) if c.commune else None
            chosen = st.selectbox("Commune", slugs, index=slugs.index(preferred) if preferred in slugs else 0,
                                  format_func=lambda sl: names.get(sl, sl), key="report_commune")
            _download_pdf(f"Télécharger le rapport de {names.get(chosen, chosen)}", entry["communes"][chosen],
                          "dl_commune")
    st.caption("Les rapports sont produits automatiquement le 1er de chaque mois à partir des données TBM "
               "(Licence Ouverte 2.0). Ils peuvent être librement réutilisés en citant « Urban Vision, d'après "
               "les données TBM ». Le score de fiabilité a changé de calcul le 11/10/2026 (voir « Méthode et lexique ») : "
               "les rapports antérieurs, jusqu'à celui de septembre 2026, utilisent l'ancien calcul (ponctualité "
               "− 2 × arrêts non desservis) et ne se comparent pas directement aux suivants.")


def render_page_about(c: PageContext) -> None:
    st.markdown("### À propos d'Urban Vision")
    st.markdown(
        "Urban Vision mesure, à partir des données en temps réel publiées par TBM, si les bus, trams et bateaux "
        "du réseau de Bordeaux Métropole passent à l'heure, où et quand les retards apparaissent, et à qui "
        "s'adresser pour les réduire. Les chiffres décrivent ce qui a été observé ; les causes proposées sont des "
        "pistes à confirmer sur le terrain.\n\n"
        "C'est un projet **indépendant**, conçu et maintenu par Elias Khallouk. Il n'est affilié ni à "
        "l'exploitant du réseau TBM ni à Bordeaux Métropole. Contact : "
        "[eliaskhallouk@gmail.com](mailto:eliaskhallouk@gmail.com)."
    )
    st.markdown("#### Sources et licences")
    st.markdown(
        "- **Données** : flux temps réel GTFS-RT (TripUpdates, Service Alerts) et horaires GTFS du réseau TBM, "
        "publiés sous [Licence Ouverte 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/) via "
        "[transport.data.gouv.fr](https://transport.data.gouv.fr).\n"
        "- **Données dérivées** (rapports, exports CSV) : Licence Ouverte 2.0, à citer « Urban Vision, d'après "
        "les données TBM ».\n"
        "- **Code source** : libre et ouvert. Chacun peut le lire, le réutiliser et le modifier, à condition de "
        "publier ses modifications sous la même licence (licence GNU AGPL 3.0, "
        "[texte de la licence](https://www.gnu.org/licenses/agpl-3.0.html)) ; dépôt : "
        "[github.com/EliasKhallouk/Urban-Vision](https://github.com/EliasKhallouk/Urban-Vision).\n"
        "- **Bibliothèques et services** : Streamlit (Apache 2.0) ; Highcharts (Highsoft AS, licence gratuite "
        "pour un usage non commercial) ; deck.gl (MIT) et MapLibre GL JS (BSD-3-Clause) ; fond de carte © CARTO, "
        "données © contributeurs OpenStreetMap (ODbL) ; contours des communes : Bordeaux Métropole."
    )
    st.markdown("#### Mentions légales")
    st.markdown(
        "- **Éditeur et directeur de la publication** : Elias Khallouk, personne physique, "
        "eliaskhallouk@gmail.com.\n"
        "- **Hébergeur** : Oracle Cloud Infrastructure, Oracle Corporation, 2300 Oracle Way, Austin, TX 78741, "
        "États-Unis (+1 737 867 1000) ; serveur situé dans la région de Paris (France).\n"
        "- **Adresse** : urban-vision.duckdns.org (nom de domaine fourni par DuckDNS)."
    )
    st.markdown("#### Confidentialité")
    st.markdown(
        "- Aucun compte, aucun formulaire, aucun cookie publicitaire ni outil de mesure d'audience tiers. Le site "
        "dépose un cookie technique de sécurité (protection contre la falsification de requêtes), nécessaire à son "
        "fonctionnement, et un cookie `uv_guide_vu` (un an) qui évite de réafficher le guide de lecture.\n"
        "- **Journaux du serveur** : adresse IP, date, page demandée et navigateur, conservés 14 jours pour la "
        "sécurité du site et des statistiques globales de fréquentation, produites avec des adresses IP "
        "anonymisées.\n"
        "- **Statistiques de visite** : l'adresse IP est tronquée (dernier nombre mis à zéro) avant tout "
        "traitement ; la ville approximative est déterminée sur cette adresse tronquée par les services ipwho.is "
        "et BigDataCloud ; ces données sont conservées 30 jours.\n"
        "- **Services appelés par votre navigateur** pour afficher les graphiques et la carte : "
        "cdn.jsdelivr.net et basemaps.cartocdn.com ; ils reçoivent votre adresse IP.\n"
        "- Base légale : intérêt légitime de l'éditeur (sécurité et mesure de fréquentation). Vous pouvez demander "
        "l'accès à vos données, leur effacement ou vous opposer à leur traitement en écrivant à "
        "eliaskhallouk@gmail.com, et adresser une réclamation à la [CNIL](https://www.cnil.fr)."
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_score_change(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None, prev_since_ts: int | None,
                      prev_end_ts: int | None, commune: str | None = None, route_id: str | None = None) -> dict | None:
    """Score de la période et de la précédente, avec leurs marges à 95 % et leur comparaison (`ind.compare`)."""
    def at(lo, hi):
        return ind.indicators(_conn, *_day_bounds(lo, hi, cutoff_ts), routes=[route_id] if route_id else None,
                              communes=[commune] if commune else None)
    now = at(since_ts, end_ts)
    if not now.disponible:
        return None
    out = {"score": now.score, "marge": now.marge, "previous": None, "compare": None}
    if prev_since_ts is not None:
        prev = at(prev_since_ts, prev_end_ts)
        if prev.disponible:
            out["previous"] = prev.score
            out["compare"] = ind.compare(now, prev)
    return out


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_route_indicators(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                          commune: str | None = None) -> pd.DataFrame:
    """Score et marge à 95 % de chaque ligne (`ind.route_indicators`), sur les arrêts de la commune s'il y en a une."""
    return ind.route_indicators(_conn, *_day_bounds(since_ts, end_ts, cutoff_ts),
                                communes=[commune] if commune else None)


def _points(value: float) -> str:
    return f"{format_number(abs(value), 1)} point{'s' if abs(value) >= 2 else ''}"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:1].isalpha() and not text[1:2].isupper() else text


def verdict_sentence(subject: str, period_label: str, score: float, comparison: dict | None,
                     prev_label: str | None) -> str:
    """Phrase verdict en tête d'écran : l'état en un mot, le score en clair, l'évolution jugée contre la marge."""
    text = (f"Sur {html.escape(_lower_first(period_label))}, {subject} est <b>{tier_label(score)}</b> : "
            f"{score:.0f} passages sur 100 ont été assurés et à l'heure")
    if not prev_label or not comparison:
        return text + "."
    gap, margin, significant = comparison["ecart"], comparison["marge"], comparison["significatif"]
    prev = html.escape(_lower_first(prev_label))
    if significant is None:
        return text + f", {evolution_words(gap)} par rapport à {prev}."
    if significant:
        return text + (f", <b>{'en baisse' if gap < 0 else 'en hausse'} de {_points(gap)}</b> par rapport à {prev} "
                       f"(plus que la marge d'incertitude de ± {format_number(margin, 1)}).")
    return text + (f", stable par rapport à {prev} : l'écart ({format_number(gap, 1, signed=True)}) reste dans la "
                   f"marge d'incertitude de ± {format_number(margin, 1)}.")


def key_points(scope: str, of_scope: str, change: dict | None, prev_label: str | None, lines_now: pd.DataFrame,
               lines_before: pd.DataFrame | None, line_names: dict) -> list[dict]:
    """Trois choses à retenir, chacune fondée sur un écart qui dépasse la marge d'incertitude à 95 %.

    1. L'évolution du score du périmètre (stable si l'écart reste dans la marge) ;
    2. la ligne dont la baisse dépasse le plus sa marge combinée (√(m₁² + m₂²)) ;
    3. la ligne la moins fiable dont le score, marge comprise, reste sous celui du périmètre marge comprise.
    Les lignes au temps réel douteux sont écartées. Chaque point : chiffre (`figure`), libellé court
    (`label`), précision (`detail`), phrase complète (`text`) et `route_id` (ou None).
    """
    out = []
    prev = html.escape(_lower_first(prev_label)) if prev_label else None
    if change is None:
        return out
    cmp = change.get("compare")
    if cmp is None or cmp["significatif"] is None:
        out.append({"figure": f"{change['score']:.0f}/100", "label": f"Score {of_scope}",
                    "detail": f"pas de comparaison possible avec {prev}" if prev else "pas de période de comparaison",
                    "text": f"Le score {of_scope} est de <b>{change['score']:.0f} sur 100</b>"
                            + (f" (pas de comparaison possible avec {prev})." if prev else "."), "route_id": None})
    elif cmp["significatif"]:
        down = cmp["ecart"] < 0
        out.append({"figure": f"{'▼' if down else '▲'} {format_number(abs(cmp['ecart']), 1)} pt{'s' if abs(cmp['ecart']) >= 2 else ''}",
                    "label": f"Score {'en baisse' if down else 'en hausse'} depuis {prev}",
                    "detail": f"{change['previous']:.0f} puis {change['score']:.0f} sur 100, au-delà de la marge "
                              f"(± {format_number(cmp['marge'], 1)})",
                    "text": f"Le score {of_scope} a <b>{'baissé' if down else 'progressé'} de {_points(cmp['ecart'])}</b> "
                            f"par rapport à {prev} ({change['previous']:.0f} puis {change['score']:.0f} sur 100), "
                            f"plus que la marge d'incertitude de ± {format_number(cmp['marge'], 1)}.", "route_id": None})
    else:
        out.append({"figure": "Stable", "label": f"Score stable depuis {prev}",
                    "detail": f"écart de {format_number(cmp['ecart'], 1, signed=True)}, dans la marge "
                              f"(± {format_number(cmp['marge'], 1)})",
                    "text": f"Le score {of_scope} est <b>stable</b> par rapport à {prev} : l'écart "
                            f"({format_number(cmp['ecart'], 1, signed=True)}) reste dans la marge d'incertitude "
                            f"de ± {format_number(cmp['marge'], 1)}.", "route_id": None})
    now = lines_now[~lines_now["temps_reel_douteux"].astype(bool)] if not lines_now.empty else lines_now
    if lines_before is not None and not lines_before.empty and not now.empty:
        both = now.merge(lines_before[["route_id", "score_v2", "marge_v2"]], on="route_id", suffixes=("", "_avant"))
        both = both.dropna(subset=["score_v2", "marge_v2", "score_v2_avant", "marge_v2_avant"])
        both["ecart"] = both["score_v2"] - both["score_v2_avant"]
        both["marge_ecart"] = (both["marge_v2"] ** 2 + both["marge_v2_avant"] ** 2) ** 0.5
        drops = both[both["ecart"] < -both["marge_ecart"]].sort_values("ecart")
        if drops.empty:
            out.append({"figure": "Aucune", "label": "ligne en baisse au-delà de sa marge",
                        "detail": f"par rapport à {prev}",
                        "text": f"Aucune ligne n'a baissé plus que sa marge d'incertitude par rapport à {prev}.",
                        "route_id": None})
        else:
            r = drops.iloc[0]
            name = html.escape(line_names.get(r["route_id"], r["route_id"]))
            out.append({"figure": f"{r['score_v2_avant']:.0f} → {r['score_v2']:.0f}",
                        "label": f"Ligne {name} : la plus forte baisse",
                        "detail": f"{format_number(r['ecart'], 1, signed=True)} points, au-delà de sa marge "
                                  f"(± {format_number(r['marge_ecart'], 1)})",
                        "text": f"La ligne <b>{name}</b> a le plus baissé : {r['score_v2_avant']:.0f} puis "
                                f"<b>{r['score_v2']:.0f} sur 100</b>, une baisse de {_points(r['ecart'])}, plus que "
                                f"sa marge d'incertitude de ± {format_number(r['marge_ecart'], 1)}.",
                        "route_id": str(r["route_id"]), "line": line_names.get(r["route_id"], r["route_id"])})
    if change.get("marge") is not None and not now.empty:
        floor = change["score"] - change["marge"]
        clear = now.dropna(subset=["score_v2", "marge_v2"])
        clear = clear[clear["score_v2"] + clear["marge_v2"] < floor]
        if clear.empty:
            out.append({"figure": "Aucune", "label": f"ligne nettement en dessous {of_scope}",
                        "detail": "une fois la marge d'incertitude prise en compte",
                        "text": f"Aucune ligne n'est nettement en dessous {of_scope} une fois la marge "
                                "d'incertitude prise en compte.", "route_id": None})
        else:
            r = clear.assign(haut=clear["score_v2"] + clear["marge_v2"]).sort_values("haut").iloc[0]
            name = html.escape(line_names.get(r["route_id"], r["route_id"]))
            out.append({"figure": f"{r['score_v2']:.0f}/100", "label": f"Ligne {name} : la moins fiable",
                        "detail": f"± {format_number(r['marge_v2'], 1)}, contre {change['score']:.0f} pour {scope}",
                        "text": f"La ligne <b>{name}</b> est la moins fiable, même en tenant compte de "
                                f"l'incertitude : <b>{r['score_v2']:.0f} sur 100</b> (± {format_number(r['marge_v2'], 1)}), "
                                f"contre {change['score']:.0f} pour {scope}.", "route_id": str(r["route_id"]),
                        "line": line_names.get(r["route_id"], r["route_id"])})
    return out[:3]


PROBLEM_SCORE = 50.0


def stops_to_treat(territorial: pd.DataFrame, stop_communes: dict, limit: int = 5,
                   max_score: float | None = PROBLEM_SCORE) -> pd.DataFrame:
    """Arrêts à traiter en premier : quais regroupés par nom et commune ; seuls les arrêts problématiques
    (score sous `max_score`, seuil du palier négatif) sont retenus, puis classés par passages ratés (attendus
    moins assurés et à l'heure). `max_score=None` : tous les arrêts, classés par passages ratés.
    `stop_id` désigne le quai qui rate le plus de passages."""
    columns = ["stop_id", "stop_name", "commune", "rates", "attendus", "score", "route_type"]
    df = territorial[territorial["observations"] >= MIN_OBSERVATIONS] if not territorial.empty else territorial
    if df.empty:
        return pd.DataFrame(columns=columns)
    df = df.assign(commune=df["stop_id"].map(stop_communes).fillna(""))
    rows = []
    for (name, commune), group in df.groupby(["stop_name", "commune"], sort=False):
        rep = group.sort_values("rates", ascending=False).iloc[0]
        expected = float(group["attendus"].sum())
        missed = float(group["rates"].sum())
        rows.append({"stop_id": rep["stop_id"], "stop_name": name, "commune": commune, "rates": missed,
                     "attendus": expected, "score": 100 * (expected - missed) / expected if expected else float("nan"),
                     "route_type": rep["route_type"]})
    out = pd.DataFrame(rows, columns=columns)
    if max_score is not None:
        out = out[out["score"] < max_score]
    return out.sort_values("rates", ascending=False).head(limit).reset_index(drop=True)


def _scope_words(commune: str | None) -> tuple[str, str]:
    """Sujet et complément du périmètre : (« le réseau », « du réseau ») ou (« Mérignac », « de Mérignac »)."""
    if not commune:
        return "le réseau", "du réseau"
    name = html.escape(commune)
    return name, (f"d'{name}" if name[:1].lower() in "aeiouyéèêâîôûh" else f"de {name}")


def evolution_chip(change: dict | None, prev_label: str | None) -> str:
    """Pastille d'évolution du bloc principal : neutre, jugée contre la marge (« écart réel » ou « stable »)."""
    cmp = (change or {}).get("compare")
    if not prev_label or not cmp:
        return '<span class="hero-chip">pas de comparaison</span>'
    prev = html.escape(_lower_first(prev_label))
    if cmp["significatif"] is None:
        return f'<span class="hero-chip">{evolution_words(cmp["ecart"])} depuis {prev}</span>'
    if cmp["significatif"]:
        return (f'<span class="hero-chip strong">{evolution_words(cmp["ecart"])} depuis {prev}</span>'
                f'<span class="hero-why">plus que la marge d\'incertitude (± {format_number(cmp["marge"], 1)})</span>')
    return (f'<span class="hero-chip">stable depuis {prev}</span>'
            f'<span class="hero-why">écart {format_number(cmp["ecart"], 1, signed=True)}, dans la marge '
            f'(± {format_number(cmp["marge"], 1)})</span>')


def render_home_header(c: PageContext) -> None:
    ranking = c.ranking
    total = int(ranking["observations"].sum())
    on_time = float((ranking["observations"] * ranking["pct_a_l_heure"]).sum() / max(total, 1))
    delay = float((ranking["observations"] * ranking["retard_moyen_s"]).sum() / max(total, 1))
    skip_rate = int(ranking["skipped"].sum()) / max(int(ranking["eligible"].sum()), 1) * 100
    score = network_score(ranking)
    change = load_score_change(c.conn, c.cutoff, c.since_ts, c.end_ts, c.prev_since_ts, c.prev_end_ts, commune=c.commune)
    st.title(f"{c.commune or 'Le réseau'} en {_lower_first(c.period.label)}")
    hero, curve = st.columns([1, 1.7], gap="large", vertical_alignment="center")
    with hero:
        st.markdown(
            f'<div class="hero" role="group" aria-label="Score de fiabilité">'
            f'<div class="hero-kicker">Score de fiabilité</div>'
            f'<div class="hero-score"><span class="hero-dot" style="background:{score_color(score)}"></span>'
            f'{score:.0f}<span class="hero-max">/100</span></div>'
            f'<div class="hero-word">{tier_label(score).capitalize()}</div>'
            f'<div class="hero-sub">{score:.0f} passages sur 100 assurés et à l\'heure</div>'
            f'<div class="hero-evo">{evolution_chip(change, c.period.prev_label)}</div></div>',
            unsafe_allow_html=True)
    with curve:
        daily = load_daily_metrics(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
        daily = daily[daily["groupe"] == GROUP_ALL] if not daily.empty else daily
        if not daily.empty and "score_fiabilite" in daily:
            st.markdown('<div class="hero-curve-title">Jour par jour</div>', unsafe_allow_html=True)
            hc_render(score_sparkline(daily), height=190)
    render_kpis([
        ("À l'heure", f"{on_time:.0f} %", "de 1 min d'avance à 5 min de retard",
         palette_kpi_tier({"ponctualite": on_time}, "ponctualite"), HELP_TEXTS["ponctualite"]),
        ("Retard moyen", format_seconds(delay, signed=True), "par passage",
         palette_kpi_tier({"retard": delay}, "retard"), HELP_TEXTS["retard"]),
        ("Arrêts non desservis", f"{format_number(skip_rate, 1)} %", "des arrêts prévus",
         palette_kpi_tier({"skip_rate": skip_rate}, "skip_rate"), HELP_TEXTS["skip"]),
    ])
    note = exclusion_note(load_exclusions(c.conn, *_day_bounds(c.since_ts, c.end_ts, c.cutoff)))
    if note:
        st.caption(note)


def _key_tiles(points: list[dict]) -> None:
    cols = st.columns(len(points) or 1, gap="medium")
    for i, (col, point) in enumerate(zip(cols, points)):
        with col.container(key=f"tile_{i}"):
            st.markdown(f'<div class="tile-figure">{html.escape(point["figure"])}</div>'
                        f'<div class="tile-label">{point["label"]}</div>'
                        f'<div class="tile-detail">{html.escape(point["detail"])}</div>', unsafe_allow_html=True)
            if point["route_id"]:
                st.button(f"Voir la ligne {point['line']} ›", key=f"tile_btn_{i}", on_click=open_line,
                          args=(point["route_id"],), type="tertiary")


def _stop_rows(top: pd.DataFrame, prefix: str) -> None:
    """Une ligne par arrêt : score en pastille, nom cliquable (ouvre la fiche), commune, barre et chiffres."""
    peak = float(top["rates"].max()) if not top.empty else 1.0
    for r in top.itertuples():
        with st.container(key=f"{prefix}_{r.Index}"):
            badge, name, bar = st.columns([0.45, 2.6, 3.2], vertical_alignment="center", gap="small")
            badge.markdown(f'<span class="sr-score" style="background:{score_color(r.score)};'
                           f'color:{ink_on(score_color(r.score))}" aria-label="score {r.score:.0f} sur 100">'
                           f'{r.score:.0f}</span>', unsafe_allow_html=True)
            with name:
                st.button(f"{r.stop_name} ›", key=f"{prefix}_btn_{r.Index}", on_click=open_stop, args=(r.stop_id,),
                          type="tertiary", help="Ouvrir la fiche de l'arrêt sur la carte")
                if r.commune:
                    st.markdown(f'<span class="sr-where">{html.escape(r.commune)}</span>', unsafe_allow_html=True)
            bar.markdown(f'<div class="sr-right"><span class="sr-bar" style="width:{max(2, r.rates / peak * 45):.0f}%">'
                         f'</span><span class="sr-fig">{fmt_int(r.rates)} passages ratés sur {fmt_int(r.attendus)}'
                         f'</span></div>', unsafe_allow_html=True)


def render_page_home(c: PageContext) -> None:
    render_home_header(c)
    scope, of_scope = _scope_words(c.commune)
    change = load_score_change(c.conn, c.cutoff, c.since_ts, c.end_ts, c.prev_since_ts, c.prev_end_ts, commune=c.commune)
    lines_now = load_route_indicators(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    lines_before = (load_route_indicators(c.conn, c.cutoff, c.prev_since_ts, c.prev_end_ts, commune=c.commune)
                    if c.prev_since_ts is not None else None)
    names = {str(r.route_id): str(r.ligne) for r in c.ranking_net.itertuples()}
    st.markdown("### À retenir", help="Un écart n'est retenu que s'il dépasse la marge d'incertitude à 95 %, due "
                                      "aux différences d'un jour à l'autre : en dessous, il peut venir du hasard.")
    _key_tiles(key_points(scope, of_scope, change, c.period.prev_label, lines_now, lines_before, names))

    territorial = load_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    _, stop_communes = load_place_names(c.conn)
    st.markdown("### Arrêts à traiter en premier",
                help="Les arrêts problématiques (score sous 50), classés par passages ratés : course supprimée, "
                     "arrêt non desservi ou départ hors de la plage −1 min / +5 min. Les quais d'un même arrêt sont "
                     "comptés ensemble.")
    top = stops_to_treat(territorial, stop_communes)
    if top.empty:
        st.markdown('<div class="empty-ok">Aucun arrêt problématique ici sur la période.</div>',
                    unsafe_allow_html=True)
    else:
        _stop_rows(top, "atraiter")

    st.markdown("### Voir le détail")
    detail = st.segmented_control("Détail", HOME_DETAILS, key="home_detail", label_visibility="collapsed")
    if detail == "Quand ?":
        render_page_when(c)
    elif detail == "Modes de transport":
        render_page_network(c)
    elif detail == "Perturbations":
        render_page_perturbations(c)
    elif detail == "Arrêts qui ratent le plus de passages":
        st.caption("Tous les arrêts, classés par nombre de passages ratés : les gros arrêts un peu en dessous du "
                   "réseau passent devant les petits arrêts très en retard.")
        _stop_rows(stops_to_treat(territorial, stop_communes, limit=10, max_score=None), "plusrates")


def render_page_map(c: PageContext) -> None:
    st.session_state.setdefault("map_view", VIEW_STOPS)
    view = st.segmented_control("Ce que montre la carte", MAP_VIEWS, key="map_view",
                                label_visibility="collapsed") or VIEW_STOPS
    if view == VIEW_CONTENTION:
        render_page_contention(c)
    else:
        render_page_territory(c)


PAGES = {
    PAGE_HOME: lambda c: render_page_home(c),
    PAGE_TERRITORY: lambda c: render_page_map(c),
    PAGE_LINE: render_page_lines,
    PAGE_REPORTS: render_page_reports,
    PAGE_DATA: render_page_data,
    PAGE_ABOUT: render_page_about,
}


@st.cache_data(ttl=3600)
def load_place_names(_conn) -> tuple[dict, dict]:
    """Nom long de chaque ligne (route_id -> terminus) et commune de chaque arrêt (stop_id -> commune)."""
    lines = dict(_conn.execute("SELECT route_id, COALESCE(route_long_name, '') FROM routes"))
    communes = dict(_conn.execute("SELECT stop_id, commune_name FROM stop_municipalities"))
    return lines, communes


def _natural(text: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(text)))


def search_options(communes: list[str], ranking: pd.DataFrame, unmeasured: pd.DataFrame | None,
                   stops: pd.DataFrame, line_names: dict, stop_communes: dict) -> dict:
    """Choix de la recherche unique : communes, lignes (avec leur état, ou « sans temps réel ») et arrêts
    regroupés par nom et commune, tous quais confondus (la clé désigne le quai le plus fréquenté)."""
    out = {f"c:{i}": f"Commune : {name}" for i, name in enumerate(communes, start=1)}
    lines = {}
    for r in ranking.itertuples():
        lines[str(r.route_id)] = (str(r.ligne), tier_label(float(r.score_fiabilite)))
    if unmeasured is not None:
        for r in unmeasured.itertuples():
            lines.setdefault(str(r.route_id), (str(r.ligne), UNMEASURED_LABEL))
    for rid, (name, state) in sorted(lines.items(), key=lambda kv: _natural(kv[1][0])):
        long_name = line_names.get(rid) or ""
        out[f"l:{rid}"] = f"Ligne {name}{f' : {long_name}' if long_name else ''} ({state})"
    if not stops.empty:
        df = stops.assign(commune=stops["stop_id"].map(stop_communes).fillna(""))
        reps = df.sort_values("observations", ascending=False).drop_duplicates(["stop_name", "commune"])
        served = df.groupby(["stop_name", "commune"])["lignes"].agg(
            lambda v: sorted({ln.strip() for x in v for ln in str(x).split(",") if ln.strip()}, key=_natural))
        for r in reps.itertuples():
            lines_here = served[(r.stop_name, r.commune)]
            shown = ", ".join(lines_here[:4]) + ("…" if len(lines_here) > 4 else "")
            where = f", {r.commune}" if r.commune else ""
            out[f"s:{r.stop_id}"] = (f"Arrêt : {r.stop_name}{where} (lignes {shown})" if shown
                                     else f"Arrêt : {r.stop_name}{where}")
    return dict(sorted(out.items(), key=lambda kv: (kv[0][0] != "c", kv[0][0] != "l", _natural(kv[1]))))


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_search_options(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                        communes: tuple, _ranking: pd.DataFrame) -> dict:
    """Choix de la recherche unique pour la période (`search_options`), gardés en cache."""
    line_names, stop_communes = load_place_names(_conn)
    return search_options(list(communes), _ranking, load_unmeasured_lines(_conn, cutoff_ts, since_ts, end_ts, commune=None),
                          load_territorial(_conn, cutoff_ts, since_ts, end_ts, commune=None), line_names, stop_communes)


def render_context_bar(conn, cutoff: int, since_ts, end_ts, commune: str | None, communes: list[str],
                       ranking_net: pd.DataFrame, cols, crumbs_box) -> None:
    """Barre persistante : « Revenir », recherche unique (commune, ligne, arrêt) et fil d'Ariane cliquable."""
    line_id = st.session_state.get("line_id")
    stop_id = st.session_state.get("stop_id")
    network = load_territorial(conn, cutoff, since_ts, end_ts, commune=None)
    unmeasured = load_unmeasured_lines(conn, cutoff, since_ts, end_ts, commune=None)
    options = load_search_options(conn, cutoff, since_ts, end_ts, tuple(communes), ranking_net)
    short = {str(r.route_id): str(r.ligne) for r in ranking_net.itertuples()}
    if unmeasured is not None:
        short.update({str(r.route_id): str(r.ligne) for r in unmeasured.itertuples() if str(r.route_id) not in short})
    plain = {}
    for sid in {stop_id} | {snap.get("stop_id") for snap in (st.session_state.get(HISTORY_KEY) or [])[-1:]}:
        row = network[network["stop_id"] == sid] if sid else network.head(0)
        if not row.empty:
            r = row.iloc[0]
            plain[sid] = f"{r['stop_name']}, {r['direction']}" if r["direction"] else str(r["stop_name"])
    history = st.session_state.get(HISTORY_KEY) or []
    target = back_label(history[-1] if history else None, communes, short, plain)
    with cols[0]:
        st.button("← Revenir", key="ctx_back", on_click=go_back, disabled=target is None, width="stretch",
                  help=f"Revenir à {target}" if target else "Rien à quoi revenir pour l'instant.")
    with cols[1]:
        st.session_state.setdefault(SEARCH_KEY, None)
        st.selectbox("Votre commune, votre ligne ou votre arrêt", list(options), index=None,
                     format_func=lambda k: options.get(k, k), key=SEARCH_KEY, on_change=_on_search,
                     placeholder="Tapez un nom : Mérignac, 23, Quinconces…")
    steps = breadcrumb(commune, short.get(line_id, line_id) if line_id else None,
                       plain.get(stop_id, stop_id) if stop_id else None)
    with crumbs_box:
        with st.container(horizontal=True, gap="small", vertical_alignment="center", key="crumbs"):
            st.markdown('<span class="crumb-label">Vous regardez :</span>', unsafe_allow_html=True)
            for i, (label, callback) in enumerate(steps):
                if i:
                    st.markdown('<span class="crumb-sep" aria-hidden="true">›</span>', unsafe_allow_html=True)
                if callback is None:
                    st.markdown(f'<span class="crumb-here" aria-current="location">{html.escape(label)}</span>',
                                unsafe_allow_html=True)
                else:
                    st.button(label, key=f"crumb_{i}", on_click=callback, type="tertiary")


def exclusion_note(excl: ind.Exclusions) -> str | None:
    """Phrase sous le bandeau : jours et lignes écartés de tous les chiffres de la période, sinon None."""
    parts = []
    if excl.jours:
        parts.append(f"{len(excl.jours)} jour(s) à collecte incomplète ({_short_days(list(excl.jours))})")
    if excl.lignes:
        parts.append(f"{len(excl.lignes)} ligne(s) au temps réel douteux")
    if not parts:
        return None
    return f"Chiffres calculés hors {' et '.join(parts)} : détail dans « Méthode et lexique »."


def header_kpis(score: float, on_time: float, delay_s: float, skip_rate: float, total: int, n_lines: int,
                skipped_total: int, eligible_total: int, local: bool, detailed: bool) -> list[tuple]:
    """Cartes du bandeau : quatre repères en mots en vue rapide, les cinq mesures en vue détaillée."""
    kpi_ponct = palette_kpi_tier({"ponctualite": on_time}, "ponctualite")
    kpi_retard = palette_kpi_tier({"retard": delay_s}, "retard")
    kpi_skip = palette_kpi_tier({"skip_rate": skip_rate}, "skip_rate")
    if not detailed:
        return [
            status_kpi(score, "arrêts de la commune" if local else "ensemble du réseau"),
            ("À l'heure", f"{on_time:.0f} %", "des passages, de 1 min d'avance à 5 min de retard", kpi_ponct,
             HELP_TEXTS["ponctualite"]),
            ("Retard moyen", format_seconds(delay_s, signed=True), "par passage", kpi_retard, HELP_TEXTS["retard"]),
            ("Arrêts non desservis", f"{format_number(skip_rate, 1)} %", "des arrêts prévus", kpi_skip, HELP_TEXTS["skip"]),
        ]
    return [
        ("Passages analysés", f"{total:,}".replace(",", " "), None, "neutral", HELP_TEXTS["passage"]),
        ("Ponctualité" if local else "Ponctualité réseau", f"{format_number(on_time, 1)} %", "de −1 à +5 min", kpi_ponct,
         HELP_TEXTS["ponctualite"]),
        ("Retard moyen", format_seconds(delay_s, signed=True), None, kpi_retard, HELP_TEXTS["retard"]),
        ("Lignes suivies", f"{n_lines}", None, "neutral", None),
        ("Arrêts non desservis", f"{format_number(skip_rate, 2)} %",
         f"{skipped_total:,} / {eligible_total:,} attendus".replace(",", " "), kpi_skip, HELP_TEXTS["skip"]),
    ]


def main() -> None:
    st.set_page_config(page_title="Urban Vision | Fiabilité", page_icon=str(Path(__file__).resolve().parents[1] / "assets" / "logo" / "urban-vision-logo-color.png"), layout="wide")
    inject_style()
    apply_query_params()
    page, detailed = render_sidebar()
    if should_show_guide(st.session_state, st.query_params, st.context.cookies):
        st.session_state["guide_seen"] = True
        remember_guide_seen()
        show_guide()
    if not DB_PATH.exists():
        st.error(f"Base SQLite introuvable : {DB_PATH}")
        return
    conn = get_connection()
    try:
        cutoff = get_cutoff_ts(conn)
        if cutoff is None:
            st.warning("Aucune observation disponible pour le moment.")
            return
        communes = load_communes(conn)
        st.session_state.setdefault("commune_idx", 0)
        if not 0 <= int(st.session_state["commune_idx"] or 0) <= len(communes):
            st.session_state["commune_idx"] = 0
        commune = communes[st.session_state["commune_idx"] - 1] if st.session_state["commune_idx"] else None

        with st.container(key="ctx_bar"):
            cols = st.columns([0.75, 2.6, 1.05], gap="small", vertical_alignment="bottom")
            crumbs_box = st.container()
        with cols[2]:
            period = period_picker(conn, cutoff, locked=contention_view())
        since_ts, end_ts = _ts(period.start), _ts(period.end)

        ranking_net = make_ranking(*load_network_data(conn, cutoff, since_ts, end_ts, commune=None))
        render_context_bar(conn, cutoff, since_ts, end_ts, commune, communes,
                           ranking_net[ranking_net["observations"] >= MIN_OBSERVATIONS], cols, crumbs_box)
        scheduled, skipped = load_network_data(conn, cutoff, since_ts, end_ts, commune=commune)
        if scheduled.empty:
            st.warning("Aucun passage exploitable pour cette période ici. Choisissez une autre période, ou "
                       "« Tout le réseau » dans le fil d'Ariane.")
            return
        ranking = make_ranking(scheduled, skipped) if commune is not None else ranking_net
        visible_ranking = ranking[ranking["observations"] >= MIN_OBSERVATIONS].copy()
        if visible_ranking.empty:
            visible_ranking = ranking.copy()

        total = int(ranking["observations"].sum())
        disturbed = load_disturbed_route_ids(conn, since_ts, end_ts, cutoff, commune=commune)
        ctx = PageContext(conn, cutoff, since_ts, end_ts, commune, ranking, visible_ranking,
                          ranking_net, disturbed, total, period, _ts(period.prev_start), _ts(period.prev_end),
                          detailed)
        PAGES[page](ctx)
    finally:
        conn.close()


if __name__ == "__main__":
    main()