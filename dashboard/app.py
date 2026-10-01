"""Tableau de bord de fiabilite des passages TBM.

Charte graphique « Urban Vision » (fond blanc) : Black Forest #283618 (marque,
titres), Olive Leaf #606c38 (texte secondaire), Sunlit Clay #DDA15E (bordures),
Cornsilk #FEFAE0 (accent ponctuel). Couleur de performance à 3 paliers, partagée
avec les rapports via reports/palette.py : Olive Leaf = positif, Sunlit Clay =
moyen, Copperwood = négatif (pas de dégradé continu). Les modes de transport se
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
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from carte import carte_arrets, map_payload

import diagnostic as dg

from diagnostic import (
    PERIOD_ORDER,
    format_seconds,
    reliability_score,
    median_from_hists as _median_from_hists,
    period_labels as _period_labels,
)
from highcharts import (
    render as hc_render,
    ranking_chart,
    scatter_chart,
    network_daily_chart,
    network_hourly_chart,
    commune_ranking_chart,
    mode_comparison_chart,
    mode_daily_chart,
    mode_hourly_chart,
    period_punctuality_chart,
    period_mode_chart,
    engagement_trend_chart,
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

# Constantes couleur : importées du module partagé reports/palette.py pour garantir
# une seule source de vérité (palette et seuils de couleur identiques aux rapports).
_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
from palette import (  # noqa: E402
    BLACK_FOREST,
    COPPERWOOD,
    OLIVE_LEAF,
    SUNLIT_CLAY,
    CORNSILK,
    WHITE,
    hex as palette_hex,
    kpi_tier as palette_kpi_tier,
    mode_glyph,
    mode_marker,
    risk_zone,
    CRITICAL,
    FREQUENT_SHORT,
    LOW,
    RARE_LONG,
    RISK_MEDIAN_S,
    RISK_PCT_GT300,
    RISK_ZONE_LABELS,
)

SUNLIT_CLAY_40 = "rgba(221, 161, 94, 0.40)"
SUNLIT_CLAY_30 = "rgba(221, 161, 94, 0.30)"
OLIVE_LEAF_70 = "rgba(96, 108, 56, 0.70)"

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
    """Style de cellule selon le palier « score » des rapports (positif/moyen/négatif)."""
    color = palette_hex(value, "score")
    foreground = CORNSILK if color in (OLIVE_LEAF, COPPERWOOD) else BLACK_FOREST
    return f"background-color: {color}; color: {foreground};"


def _delta_style(value: float, threshold: float = 2.0) -> str:
    """Style d'une cellule d'évolution (Δ) : positif → Olive Leaf, négatif → Copperwood."""
    if value >= threshold:
        return f"color: {OLIVE_LEAF}; font-weight: 600;"
    if value <= -threshold:
        return f"color: {COPPERWOOD}; font-weight: 600;"
    return f"color: {SUNLIT_CLAY};"


def render_tier_legend(title: str = "", left_label: str = "à surveiller",
                       right_label: str = "bon", invert: bool = False) -> None:
    """Légende à 3 paliers, alignée sur les seuils des rapports.

    Trois pastilles (positif = Olive Leaf, moyen = Sunlit Clay, négatif =
    Copperwood), sans dégradé continu. `invert=True` pour les métriques où
    « plus = pire » (retards, arrêts sautés, dérive à l'horaire) : le palier
    positif s'affiche à gauche.
    """
    if invert:
        tiers = [
            ("positif", OLIVE_LEAF, left_label),
            ("moyen", SUNLIT_CLAY, ""),
            ("négatif", COPPERWOOD, right_label),
        ]
    else:
        tiers = [
            ("négatif", COPPERWOOD, left_label),
            ("moyen", SUNLIT_CLAY, ""),
            ("positif", OLIVE_LEAF, right_label),
        ]
    chips = ""
    for name, color, label in tiers:
        extra = f" <span style='color:{OLIVE_LEAF_70}'>({label})</span>" if label else ""
        chips += (
            '<span style="display:inline-flex;align-items:center;gap:.35rem;margin-right:1rem">'
            f'<span style="width:.72rem;height:.72rem;border-radius:3px;background:{color}"></span>'
            f'<b>{name}</b>{extra}</span>'
        )
    st.markdown(
        f'<div style="margin:.15rem 0 .6rem;font-size:.78rem;color:{OLIVE_LEAF_70}">'
        f'{(f"<b>{title}</b>&nbsp; " if title else "")}{chips}</div>',
        unsafe_allow_html=True,
    )





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
    "CREATE INDEX IF NOT EXISTS idx_observations_sched_delay"
    " ON observations(schedule_relationship, departure_delay, last_seen_at, route_id)",
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


def period_picker(conn, cutoff_ts: int) -> Period:
    """Choix de la période : un mois complet par défaut, ou une autre période."""
    first_day, last_day = load_service_days(conn, cutoff_ts)
    options = period_options(first_day, last_day)
    if st.session_state.get("period_choice") not in options:
        st.session_state["period_choice"] = default_period_choice(first_day, last_day)
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
    return resolve_period(choice, first_day, last_day, custom)


def inject_style() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{ background: #FFFFFF; color: #283618; }}
        [data-testid="stHeader"] {{ background: transparent; }}
        .block-container {{ max-width: 1500px; padding-top: 2.1rem; padding-bottom: 3rem; }}

        /* ---- Sidebar : seule zone de couleur pleine forte de l'interface ---- */
        [data-testid="stSidebar"] {{ background: #283618; }}
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {{ display: none; }}
        [data-testid="stSidebar"] > div:first-child {{ padding-top: 1.2rem; }}
        .sidebar-brand {{ display: flex; flex-direction: column; align-items: flex-start; gap: .55rem; margin: .1rem 0 1rem; }}
        .sidebar-brand img {{ height: 74px; width: auto; max-width: 100%; }}
        .sidebar-brand span {{ color: #FEFAE0; font-size: 1.45rem; font-weight: 700; letter-spacing: -.02em; line-height: 1.1; }}
        .sidebar-nav-label {{ color: #FFFFFF; font-size: .76rem; text-transform: uppercase; letter-spacing: .16em; font-weight: 600; margin: .8rem 0 .4rem; opacity: .9; }}
        [data-testid="stSidebar"] hr {{ border-color: rgba(221, 161, 94, .3); }}
        /* Items de navigation : actif = pastille Cornsilk / inactif = blanc opaque, gros */
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label {{
            color: #FFFFFF; opacity: 1; padding: .5rem .6rem; border-radius: 8px; font-size: 1.02rem; font-weight: 600;
            margin-bottom: .15rem; cursor: pointer; transition: background .12s ease, color .12s ease;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:hover {{ background: rgba(254, 250, 224, .14); }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {{
            background: #FEFAE0; font-weight: 700;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label [data-testid="stMarkdownContainer"] p {{
            color: #FFFFFF !important; margin: 0; font-weight: 600;
        }}
        [data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) [data-testid="stMarkdownContainer"] p {{
            color: #283618 !important; font-weight: 700;
        }}

        /* ---- Titres ---- */
        h1, h2, h3, h4 {{ color: #283618 !important; letter-spacing: -.025em; }}
        h1 {{ font-size: 2rem !important; font-weight: 700 !important; margin-bottom: .15rem !important; }}
        h2 {{ font-size: 1.375rem !important; font-weight: 600 !important; }}
        h3 {{ font-size: 1.125rem !important; font-weight: 600 !important; }}
        h4 {{ font-size: .94rem !important; font-weight: 600 !important; }}

        /* ---- Cartes KPI : fond blanc uniforme + bordure gauche sémantique ---- */
        .kpi-card {{ background: #ffffff; border: 1px solid rgba(221, 161, 94, .35); border-left: 3px solid rgba(221, 161, 94, .5); border-radius: 12px; padding: .95rem 1.05rem; height: 100%; box-shadow: 0 1px 3px rgba(40, 54, 24, .08); }}
        .kpi-label {{ color: #606c38; font-size: 12.5px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }}
        .kpi-value {{ color: #283618; font-size: 23px; font-weight: 700; margin-top: .3rem; line-height: 1.15; }}
        .kpi-sublabel {{ color: rgba(96, 108, 56, .70); font-size: 12px; margin-top: .25rem; font-weight: 500; }}

        .eyebrow {{ color: #283618; font-size: .76rem; text-transform: uppercase; letter-spacing: .15em; font-weight: 700; }}
        .hero-subtitle {{ color: rgba(96, 108, 56, .70); font-size: .94rem; font-weight: 500; margin-bottom: 1.4rem; }}
        .section-note {{ color: rgba(96, 108, 56, .70); font-size: .88rem; margin-top: -.45rem; margin-bottom: .75rem; }}
        .insight {{ background: #ffffff; border-left: 3px solid #283618; border-radius: 8px; padding: .8rem 1rem; color: #283618; box-shadow: 0 1px 3px rgba(40, 54, 24, .08); }}
        .insight.brief {{ margin: 1rem 0 .6rem; }}
        .insight.brief ul, .hints ul {{ margin: .35rem 0 0 1.1rem; padding: 0; }}
        .insight.brief li {{ margin-bottom: .3rem; line-height: 1.45; }}
        .hints {{ border: 1px dashed rgba(221, 161, 94, .7); border-radius: 8px; padding: .7rem 1rem; color: #283618; margin-bottom: 1rem; }}
        .hints li {{ margin-bottom: .25rem; }}
        .hint-note {{ color: rgba(96, 108, 56, .70); font-size: .8rem; }}
        .fiche-title, .carte-anchor {{ scroll-margin-top: 1.2rem; }}
        .sibling-label {{ color: rgba(96, 108, 56, .70); font-size: .82rem; font-weight: 600; margin-bottom: -.3rem; }}
        .fiche-title {{ color: #283618; font-size: 1.35rem; font-weight: 700; letter-spacing: -.02em; margin-top: 1.4rem; padding-top: 1rem; border-top: 2px solid #283618; }}
        .fiche-sub {{ color: rgba(96, 108, 56, .70); font-size: .88rem; margin: .2rem 0 .9rem; }}
        .wh-wrap {{ overflow-x: auto; margin: .2rem 0 .3rem; }}
        .wh-grid {{ border-collapse: separate; border-spacing: 2px; font-size: 11px; }}
        .wh-grid th {{ color: rgba(96, 108, 56, .70); font-weight: 600; padding: 0 3px; text-align: center; white-space: nowrap; }}
        .wh-grid td {{ min-width: 26px; height: 22px; text-align: center; border-radius: 3px; font-weight: 600; cursor: default; }}
        .wh-grid td.wh-empty {{ background: #FFFFFF; border: 1px dashed rgba(221, 161, 94, .35); }}
        .wh-grid td.wh-total {{ box-shadow: inset 0 0 0 1px rgba(40, 54, 24, .45); }}
        .watch-title {{ color: #283618; font-size: 1.05rem; font-weight: 700; margin-top: .3rem; line-height: 1.25; }}
        .zone-badge {{ display: inline-block; background: #FEFAE0; border: 1px solid #283618; border-radius: 999px; padding: .3rem .9rem; font-weight: 700; color: #283618; }}
        .stTabs [data-baseweb="tab-list"] {{ gap: 1.3rem; border-bottom: 1px solid rgba(221, 161, 94, .35); }}
        .stTabs [data-baseweb="tab"] {{ color: rgba(96, 108, 56, .70); padding: .55rem .15rem; font-size: .95rem; font-weight: 500; }}
        .stTabs [aria-selected="true"] {{ color: #283618; border-bottom-color: #283618; }}
        .stDataFrame {{ border: 1px solid rgba(221, 161, 94, .35); border-radius: 10px; overflow: hidden; }}
        .topbar-logo {{ display: flex; align-items: center; gap: .6rem; font-size: 1.05rem; font-weight: 700; color: #283618; letter-spacing: -.02em; white-space: nowrap; }}
        .topbar-logo img {{ height: 28px; width: auto; }}
        .topbar-logo .dot {{ width: .65rem; height: .65rem; border-radius: 50%; background: #283618; }}
        [data-testid="stPopoverButton"] {{ background: #ffffff; border: 1px solid rgba(221, 161, 94, .40); border-radius: 8px; font-size: .88rem; font-weight: 500; color: #283618; padding: .4rem .8rem; white-space: nowrap; }}
        [data-testid="stPopoverButton"]:hover, [data-testid="stPopoverButton"][aria-expanded="true"] {{ background: #283618; border-color: #283618; color: #ffffff; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def _load_daily_core(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                     route_id: str | None = None, commune: str | None = None) -> pd.DataFrame:
    """Lignes des agrégats journaliers (réseau ou commune restreinte).

    En mode réseau, lit agg_daily (une ligne par ligne et par jour). En mode
    commune, lit agg_daily_stop (une ligne par arrêt, ligne et jour) puis groupe
    au niveau ligne sur la période, pour restreindre aux arrêts de la commune.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    if commune is None:
        route_sql, route_params = (" AND d.route_id = ?", [route_id]) if route_id else ("", [])
        df = pd.read_sql_query(
            f"""
            SELECT d.date_service, d.route_id,
                   COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
                   d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60,
                   d.skipped, d.eligible, d.histogram
            FROM agg_daily d LEFT JOIN routes r ON r.route_id = d.route_id
            WHERE d.date_service >= ? AND d.date_service < ? {route_sql}
            ORDER BY d.date_service
            """, _conn, params=(since_day, end_day, *route_params),
        )
    else:
        route_sql, route_params = (" AND d.route_id = ?", [route_id]) if route_id else ("", [])
        df = pd.read_sql_query(
            f"""
            SELECT d.date_service, d.route_id, d.stop_id,
                   COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
                   d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.cnt_lt60,
                   d.skipped, d.eligible, d.histogram
            FROM agg_daily_stop d
            JOIN routes r ON r.route_id = d.route_id
            JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
            WHERE sm.commune_name = ? AND d.date_service >= ? AND d.date_service < ? {route_sql}
            ORDER BY d.date_service
            """, _conn, params=(commune, since_day, end_day, *route_params),
        )
        if not df.empty:
            df = _group_daily_stop_to_route(df)
    if not df.empty:
        df["hist"] = df["histogram"].map(json.loads)
        df["date_service"] = pd.to_datetime(df["date_service"])
    return df


def _group_daily_stop_to_route(df: pd.DataFrame) -> pd.DataFrame:
    """Agrège les lignes de agg_daily_stop au niveau (date_service, route_id).

    Somme les compteurs et fusionne les histogrammes JSON arrêt par arrêt.
    """
    agg = (
        df.groupby(["date_service", "route_id", "ligne", "route_type"], sort=False)
        .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"),
             cnt_le300=("cnt_le300", "sum"), cnt_gt300=("cnt_gt300", "sum"),
             cnt_lt60=("cnt_lt60", "sum"), skipped=("skipped", "sum"),
             eligible=("eligible", "sum"))
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
    if commune is None:
        route_sql, route_params = (" AND h.route_id = ?", [route_id]) if route_id else ("", [])
        return pd.read_sql_query(
            f"""
            SELECT h.date_service, h.route_id, h.heure, r.route_type,
                   h.obs, h.sum_delay, h.cnt_le300, h.cnt_gt300
            FROM agg_hourly h LEFT JOIN routes r ON r.route_id = h.route_id
            WHERE h.date_service >= ? AND h.date_service < ? {route_sql}
            ORDER BY h.heure
            """, _conn, params=(since_day, end_day, *route_params),
        )
    route_sql, route_params = (" AND h.route_id = ?", [route_id]) if route_id else ("", [])
    df = pd.read_sql_query(
        f"""
        SELECT h.date_service, h.route_id, h.heure, r.route_type,
               h.obs, h.sum_delay, h.cnt_le300, h.cnt_gt300
        FROM agg_hourly_stop h
        JOIN routes r ON r.route_id = h.route_id
        JOIN stop_municipalities sm ON sm.stop_id = h.stop_id
        WHERE sm.commune_name = ? AND h.date_service >= ? AND h.date_service < ? {route_sql}
        ORDER BY h.heure
        """, _conn, params=(commune, since_day, end_day, *route_params),
    )
    if df.empty:
        return df
    return (
        df.groupby(["date_service", "route_id", "heure", "route_type"], sort=False)
        .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"),
             cnt_le300=("cnt_le300", "sum"), cnt_gt300=("cnt_gt300", "sum"))
        .reset_index()
    )


def _daily_to_network(core: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Déduit du core journalier les deux tables du classement (scheduled/skipped)."""
    if core.empty:
        empty_s = core[["route_id", "ligne", "route_type"]].copy()
        for col in ("observations", "retard_moyen_s", "retard_median_s",
                    "pct_a_l_heure", "pct_retard_5min", "pct_avance_1min"):
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
            "pct_a_l_heure": sub["cnt_le300"].sum() / max(obs, 1) * 100,
            "pct_retard_5min": sub["cnt_gt300"].sum() / max(obs, 1) * 100,
            "pct_avance_1min": sub["cnt_lt60"].sum() / max(obs, 1) * 100,
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
    ranking = scheduled.merge(skipped, on=["route_id", "ligne", "route_type"], how="left").fillna({"skipped": 0, "eligible": 0})
    ranking["pct_arrets_sautes"] = np.where(ranking["eligible"] > 0, ranking["skipped"] / ranking["eligible"] * 100, 0)
    ranking["score_fiabilite"] = (ranking["pct_a_l_heure"] - ranking["pct_arrets_sautes"] * 2).clip(0, 100)
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
            "pct_a_l_heure": sub["cnt_le300"].sum() / max(obs, 1) * 100,
            "pct_retard_5min": sub["cnt_gt300"].sum() / max(obs, 1) * 100,
            "pct_avance_1min": sub["cnt_lt60"].sum() / max(obs, 1) * 100,
            "skipped": int(sub["skipped"].sum()),
            "eligible": int(sub["eligible"].sum()),
        }
    g = pd.DataFrame(rows.values() if rows else rows)
    g["retard_median_s"] = [_median_from_hists(hists[rt]) for rt in g["route_type"]]
    g["pct_arrets_sautes"] = np.where(g["eligible"] > 0, g["skipped"] / g["eligible"] * 100, 0)
    g["mode"] = g["route_type"].map(MODE_LABELS).fillna("Autre")
    return g.sort_values("observations", ascending=False)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_network_daily(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                       commune: str | None = None) -> pd.DataFrame:
    """Retards > 5 min par jour, réseau entier (ou commune), depuis les agrégats."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return core[["date_service"]].head(0)
    d = (core.groupby("date_service", sort=False)
         .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"), cnt_gt300=("cnt_gt300", "sum"))
         .reset_index())
    d["observations"] = d["obs"]
    d["retard_moyen_s"] = d["sum_delay"] / d["obs"].replace(0, np.nan)
    d["pct_retard_5min"] = d["cnt_gt300"] / d["obs"].replace(0, np.nan) * 100
    return d[["date_service", "observations", "retard_moyen_s", "pct_retard_5min"]]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_engagement_trend(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                          commune: str | None = None) -> pd.DataFrame:
    """Série quotidienne du réseau (ou commune) pour suivre l'évolution de la fiabilité.

    Une ligne par jour de service : ponctualité ≤ 5 min, retards > 5 min,
    arrêts sautés, avance, retard moyen, depuis les agrégats précalculés.
    """
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return core[["date_service"]].head(0)
    g = (core.groupby("date_service", sort=False)
         .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"),
              cnt_le300=("cnt_le300", "sum"), cnt_gt300=("cnt_gt300", "sum"),
              cnt_lt60=("cnt_lt60", "sum"), skipped=("skipped", "sum"),
              eligible=("eligible", "sum"))
         .reset_index())
    g["observations"] = g["obs"]
    g["pct_a_l_heure"] = g["cnt_le300"] / g["obs"].replace(0, np.nan) * 100
    g["pct_retard_5min"] = g["cnt_gt300"] / g["obs"].replace(0, np.nan) * 100
    g["pct_avance_1min"] = g["cnt_lt60"] / g["obs"].replace(0, np.nan) * 100
    g["pct_arrets_sautes"] = np.where(g["eligible"] > 0, g["skipped"] / g["eligible"] * 100, 0)
    g["retard_moyen_s"] = g["sum_delay"] / g["obs"].replace(0, np.nan)
    return g[["date_service", "observations", "pct_a_l_heure", "pct_retard_5min",
              "pct_avance_1min", "pct_arrets_sautes", "retard_moyen_s"]]


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


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_hourly(_conn, cutoff_ts: int, since_ts: int | None, route_id: str | None = None,
                end_ts: int | None = None, commune: str | None = None) -> pd.DataFrame:
    """Risque par tranche horaire (réseau, commune ou ligne), depuis les agrégats."""
    core = _load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, route_id=route_id, commune=commune)
    if core.empty:
        return core[["heure"]].head(0)
    h = (core.groupby("heure", sort=False)
         .agg(obs=("obs", "sum"), sum_delay=("sum_delay", "sum"), cnt_gt300=("cnt_gt300", "sum"))
         .reset_index())
    h["observations"] = h["obs"]
    h["retard_moyen_s"] = h["sum_delay"] / h["obs"].replace(0, np.nan)
    h["pct_retard_5min"] = h["cnt_gt300"] / h["obs"].replace(0, np.nan) * 100
    return h[["heure", "observations", "retard_moyen_s", "pct_retard_5min"]]


def _sort_periods(df: pd.DataFrame) -> pd.DataFrame:
    """Reclasse par l'ordre canonique PERIOD_ORDER, sans ajouter de colonne."""
    order = {p: i for i, p in enumerate(PERIOD_ORDER)}
    return df.assign(_ord=df["période"].map(order).fillna(len(PERIOD_ORDER))) \
             .sort_values("_ord", kind="stable").drop(columns="_ord")


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_period_stats(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                      commune: str | None = None) -> pd.DataFrame:
    """Fiabilité par créneau (Matin/Journée/Pointe du soir/Soirée & nuit/Week-end)."""
    core = _load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return pd.DataFrame(columns=["période", "observations", "retard_moyen_s",
                                     "pct_a_l_heure", "pct_retard_5min"])
    core = core.copy()
    core["période"] = _period_labels(core["date_service"], core["heure"])
    g = (core.groupby("période", sort=False)
         .agg(observations=("obs", "sum"), sum_delay=("sum_delay", "sum"),
              cnt_le300=("cnt_le300", "sum"), cnt_gt300=("cnt_gt300", "sum"))
         .reset_index())
    g["retard_moyen_s"] = g["sum_delay"] / g["observations"].replace(0, np.nan)
    g["pct_a_l_heure"] = g["cnt_le300"] / g["observations"].replace(0, np.nan) * 100
    g["pct_retard_5min"] = g["cnt_gt300"] / g["observations"].replace(0, np.nan) * 100
    return _sort_periods(g).reset_index(drop=True)[
        ["période", "observations", "retard_moyen_s", "pct_a_l_heure", "pct_retard_5min"]]


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
                  cnt_le300=("cnt_le300", "sum"), cnt_gt300=("cnt_gt300", "sum"))
             .reset_index())
    lines = lines[lines["observations"] >= MIN_OBSERVATIONS]
    if lines.empty:
        return empty
    lines["retard_moyen_s"] = lines["sum_delay"] / lines["observations"].replace(0, np.nan)
    lines["pct_a_l_heure"] = lines["cnt_le300"] / lines["observations"].replace(0, np.nan) * 100
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


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_mode_daily(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                    commune: str | None = None) -> pd.DataFrame:
    """Retards > 5 min par jour et par mode, depuis les agrégats."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return core[["date_service", "route_type"]].head(0)
    m = (core.groupby(["date_service", "route_type"], sort=False)
         .agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index())
    m = m[m["obs"] > 0]
    m["pct_retard_5min"] = m["cnt_gt300"] / m["obs"] * 100
    return _attach_mode(m[["date_service", "route_type", "pct_retard_5min"]])


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_mode_hourly(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                     commune: str | None = None) -> pd.DataFrame:
    """Risque horaire par mode, depuis les agrégats."""
    core = _load_hourly_core(_conn, cutoff_ts, since_ts, end_ts, commune=commune)
    if core.empty:
        return core[["heure", "route_type"]].head(0)
    m = (core.groupby(["heure", "route_type"], sort=False)
         .agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index())
    m = m[m["obs"] > 0]
    m["pct_retard_5min"] = m["cnt_gt300"] / m["obs"] * 100
    return _attach_mode(m[["heure", "route_type", "pct_retard_5min"]])


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
    hourly = pd.read_sql_query(
        """
        SELECT CAST(strftime('%H', datetime(last_seen_at, 'unixepoch', 'localtime')) AS INTEGER) AS heure,
               COUNT(*) AS observations
        FROM observations
        GROUP BY heure ORDER BY heure
        """, _conn,
    )
    first = _conn.execute("SELECT MIN(last_seen_at) FROM observations").fetchone()[0]
    last = _conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    total = _conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    n_trajets = _conn.execute("SELECT COUNT(DISTINCT trip_id || start_date) FROM observations").fetchone()[0]
    n_lignes = _conn.execute("SELECT COUNT(DISTINCT route_id) FROM observations").fetchone()[0]
    cutoff = None if last is None else int(last) - FRESHNESS_BUFFER_SECONDS
    if cutoff is None:
        analysed = 0
    else:
        analysed = _conn.execute(
            "SELECT COUNT(*) FROM observations WHERE last_seen_at < ? AND schedule_relationship = 'SCHEDULED' AND departure_delay IS NOT NULL",
            (cutoff,),
        ).fetchone()[0]
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
def load_commune_stats(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None) -> pd.DataFrame:
    """Fiabilité par commune, agrégée depuis agg_daily_stop (jamais la table brute)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    rows = _conn.execute(
        """
        SELECT sm.commune_name AS commune,
               SUM(d.obs) AS obs, SUM(d.sum_delay) AS sum_delay,
               SUM(d.cnt_le300) AS cnt_le300, SUM(d.cnt_gt300) AS cnt_gt300,
               SUM(d.skipped) AS skipped, SUM(d.eligible) AS eligible,
               COUNT(DISTINCT d.route_id) AS n_lignes
        FROM agg_daily_stop d
        JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
        WHERE d.date_service >= ? AND d.date_service < ?
        GROUP BY sm.commune_name
        """, (since_day, end_day),
    ).fetchall()
    empty = pd.DataFrame(columns=[
        "commune", "observations", "retard_moyen_s", "pct_a_l_heure",
        "pct_retard_5min", "pct_arrets_sautes", "score_fiabilite", "n_lignes",
    ])
    if not rows:
        return empty
    df = pd.DataFrame(rows, columns=[
        "commune", "obs", "sum_delay", "cnt_le300", "cnt_gt300", "skipped", "eligible", "n_lignes",
    ])
    df["observations"] = df["obs"]
    df["retard_moyen_s"] = df["sum_delay"] / df["obs"].replace(0, np.nan)
    df["pct_a_l_heure"] = df["cnt_le300"] / df["obs"].replace(0, np.nan) * 100
    df["pct_retard_5min"] = df["cnt_gt300"] / df["obs"].replace(0, np.nan) * 100
    df["pct_arrets_sautes"] = np.where(df["eligible"] > 0, df["skipped"] / df["eligible"] * 100, 0)
    df["score_fiabilite"] = (df["pct_a_l_heure"] - df["pct_arrets_sautes"] * 2).clip(0, 100)
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
        color = palette_hex(pct, "pourcent")
        fg = CORNSILK if color in (OLIVE_LEAF, COPPERWOOD) else BLACK_FOREST
        ring = ";outline:2px solid #283618;outline-offset:1px" if outline else ""
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
    """Arrêts du périmètre avec leur ligne principale et le score de fiabilité.

    Agrège depuis agg_daily_stop (jamais la table brute). Le score de l'arrêt
    porte sur toutes ses lignes (ponctualité ≤ 5 min − 2 × arrêts sautés, comme
    le classement des lignes) ; la ligne la plus fréquentée devient la ligne
    « principale », dont le mode donne la forme du marqueur.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    if commune is None:
        rows = _conn.execute(
            """
            SELECT d.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                   d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
                   d.sum_delay, d.cnt_le300, d.cnt_gt300, d.obs, d.skipped, d.eligible
            FROM agg_daily_stop d
            JOIN stops s ON s.stop_id = d.stop_id
            JOIN routes r ON r.route_id = d.route_id
            WHERE d.date_service >= ? AND d.date_service < ?
            """, (since_day, end_day),
        ).fetchall()
    else:
        rows = _conn.execute(
            """
            SELECT d.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                   d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne, r.route_type,
                   d.sum_delay, d.cnt_le300, d.cnt_gt300, d.obs, d.skipped, d.eligible
            FROM agg_daily_stop d
            JOIN stops s ON s.stop_id = d.stop_id
            JOIN routes r ON r.route_id = d.route_id
            JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
            WHERE sm.commune_name = ? AND d.date_service >= ? AND d.date_service < ?
            """, (commune, since_day, end_day),
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "stop_id", "stop_name", "direction", "lat", "lon", "route_id", "ligne", "route_type",
            "pct_a_l_heure", "pct_retard_5min", "pct_arrets_sautes", "observations",
            "score_fiabilite", "lignes",
        ])
    df = pd.DataFrame(rows, columns=[
        "stop_id", "stop_name", "stop_lat", "stop_lon", "route_id", "ligne", "route_type",
        "sum_delay", "cnt_le300", "cnt_gt300", "obs", "skipped", "eligible",
    ])
    directions = load_stop_directions(_conn)
    out = []
    best = df.sort_values("obs", ascending=False).drop_duplicates("stop_id", keep="first")
    by_stop = df.groupby("stop_id", sort=False)
    for _, top in best.iterrows():
        sub = by_stop.get_group(top["stop_id"])
        obs = int(sub["obs"].sum())
        pct_le300 = sub["cnt_le300"].sum() / max(obs, 1) * 100
        pct_gt300 = sub["cnt_gt300"].sum() / max(obs, 1) * 100
        eligible = int(sub["eligible"].sum())
        pct_skip = sub["skipped"].sum() / eligible * 100 if eligible else 0.0
        score = min(100.0, max(0.0, pct_le300 - 2 * pct_skip))
        lignes = ", ".join(sorted(set(sub["ligne"].astype(str))))
        out.append({
            "stop_id": top["stop_id"],
            "stop_name": top["stop_name"],
            "direction": directions.get((top["route_id"], top["stop_id"]), ""),
            "lat": float(top["stop_lat"]),
            "lon": float(top["stop_lon"]),
            "route_id": top["route_id"],
            "ligne": top["ligne"],
            "route_type": top["route_type"],
            "pct_a_l_heure": round(pct_le300, 1),
            "pct_retard_5min": round(pct_gt300, 1),
            "pct_arrets_sautes": round(pct_skip, 2),
            "observations": obs,
            "score_fiabilite": round(score, 1),
            "lignes": lignes,
        })
    return pd.DataFrame(out)


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
    """Compteurs quotidiens d'un arrêt, par ligne (agg_daily_stop, index sur stop_id)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    df = pd.read_sql_query(
        """
        SELECT d.date_service, d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne,
               r.route_type, d.obs, d.sum_delay, d.cnt_le300, d.cnt_gt300, d.skipped,
               d.eligible, d.histogram
        FROM agg_daily_stop d LEFT JOIN routes r ON r.route_id = d.route_id
        WHERE d.stop_id = ? AND d.date_service >= ? AND d.date_service < ?
        ORDER BY d.date_service
        """, _conn, params=(stop_id, since_day, end_day),
    )
    df["hist"] = df["histogram"].map(json.loads)
    df["date_service"] = pd.to_datetime(df["date_service"])
    return df.drop(columns="histogram")


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_stop_hourly(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                     stop_id: str) -> pd.DataFrame:
    """Compteurs horaires d'un arrêt, toutes lignes (agg_hourly_stop, index sur stop_id)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    return pd.read_sql_query(
        """
        SELECT date_service, route_id, heure, obs, cnt_gt300
        FROM agg_hourly_stop
        WHERE stop_id = ? AND date_service >= ? AND date_service < ?
        """, _conn, params=(stop_id, since_day, end_day),
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
        pct_ok = sub["cnt_le300"].sum() / obs * 100 if obs else 0.0
        pct_skip = sub["skipped"].sum() / eligible * 100 if eligible else 0.0
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
            "score_fiabilite": reliability_score(pct_ok, pct_skip),
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
    df = pd.read_sql_query(
        """
        SELECT g.direction_id, g.stop_id, s.stop_name, sm.commune_name AS commune,
               SUM(g.eligible) eligible, SUM(g.skipped) skipped, SUM(g.sum_seq) sum_seq,
               SUM(g.obs) obs, SUM(g.sum_delay) sum_delay, SUM(g.pairs) pairs,
               SUM(g.sum_prev_delay) sum_prev_delay, SUM(g.sum_gain) sum_gain,
               SUM(g.cnt_gain_gt120) cnt_gain_gt120
        FROM agg_daily_segment g
        LEFT JOIN stops s ON s.stop_id = g.stop_id
        LEFT JOIN stop_municipalities sm ON sm.stop_id = g.stop_id
        WHERE g.route_id = ? AND g.date_service >= ? AND g.date_service < ?
        GROUP BY g.direction_id, g.stop_id
        """, _conn, params=(route_id, since_day, end_day),
    )
    if df.empty:
        return df
    prev = pd.read_sql_query(
        """
        SELECT direction_id, stop_id, prev_stop_id, SUM(pairs) n
        FROM agg_daily_segment
        WHERE route_id = ? AND date_service >= ? AND date_service < ? AND prev_stop_id IS NOT NULL
        GROUP BY direction_id, stop_id, prev_stop_id
        """, _conn, params=(route_id, since_day, end_day),
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
    """Courses supprimées (trip_status = CANCELED) et courses connues, par jour de service."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    df = pd.read_sql_query(
        """
        SELECT substr(start_date, 1, 4) || '-' || substr(start_date, 5, 2) || '-'
                   || substr(start_date, 7, 2) AS date_service,
               SUM(CASE WHEN schedule_relationship = 'CANCELED' THEN 1 ELSE 0 END) AS cancelled,
               COUNT(*) AS trips
        FROM trip_status
        WHERE route_id = ? AND start_date >= ? AND start_date < ? AND last_seen_at < ?
        GROUP BY start_date
        ORDER BY start_date
        """, _conn, params=(route_id, since_day.replace("-", ""), end_day.replace("-", ""), cutoff_ts),
    )
    df["date_service"] = pd.to_datetime(df["date_service"])
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_line_stops(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                    route_id: str) -> pd.DataFrame:
    """Arrêts d'une ligne avec passages > 5 min, arrêts sautés et direction (agg_daily_stop)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    df = pd.read_sql_query(
        """
        SELECT d.stop_id, s.stop_name, sm.commune_name AS commune,
               SUM(d.obs) obs, SUM(d.cnt_le300) cnt_le300, SUM(d.cnt_gt300) cnt_gt300,
               SUM(d.skipped) skipped, SUM(d.eligible) eligible
        FROM agg_daily_stop d
        LEFT JOIN stops s ON s.stop_id = d.stop_id
        LEFT JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
        WHERE d.route_id = ? AND d.date_service >= ? AND d.date_service < ?
        GROUP BY d.stop_id
        """, _conn, params=(route_id, since_day, end_day),
    )
    if df.empty:
        return df
    directions = load_stop_directions(_conn)
    df["direction"] = [directions.get((route_id, sid), "") for sid in df["stop_id"]]
    df["pct_retard_5min"] = df["cnt_gt300"] / df["obs"].where(df["obs"] > 0) * 100
    df["pct_arrets_sautes"] = df["skipped"] / df["eligible"].where(df["eligible"] > 0) * 100
    df["score_fiabilite"] = [
        reliability_score(le / o * 100 if o else 0.0, sk / el * 100 if el else 0.0)
        for le, o, sk, el in zip(df["cnt_le300"], df["obs"], df["skipped"], df["eligible"])
    ]
    df["impact"] = df["cnt_gt300"] + df["skipped"]
    return df.sort_values("impact", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_route_hourly_stops(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None,
                            route_id: str) -> pd.DataFrame:
    """Agrégats horaires d'une ligne, arrêt par arrêt (agg_hourly_stop, index route_id + date_service)."""
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    return pd.read_sql_query(
        """
        SELECT date_service, stop_id, heure, obs, sum_delay, cnt_gt300
        FROM agg_hourly_stop
        WHERE route_id = ? AND date_service >= ? AND date_service < ?
        """, _conn, params=(route_id, since_day, end_day),
    )


def _territorial_score(df: pd.DataFrame) -> float:
    """Score de fiabilité moyen (pondéré par les passages) du périmètre territorial.

    Utilisé pour le verdict de la page « Mon territoire » (« La fiabilité de la
    commune est de X/100, contre Y/100 pour l'ensemble du réseau »).
    """
    if df is None or df.empty:
        return 0.0
    total = float(df["observations"].sum())
    if total <= 0:
        return round(float(df["score_fiabilite"].mean()), 1)
    return round(float((df["score_fiabilite"] * df["observations"]).sum() / total), 1)


def _on_map_click() -> None:
    state = st.session_state.get("carte_arrets") or {}
    clicked = state.get("clicked") if hasattr(state, "get") else getattr(state, "clicked", None)
    if clicked:
        show_stop(clicked)


def _territorial_map(stops: pd.DataFrame, groups: pd.DataFrame, commune: str | None,
                     selected_id: str | None) -> None:
    """Carte des arrêts (composant `carte.carte_arrets`, deck.gl).

    Couleur = palier du score de fiabilité de l'arrêt (Olive Leaf ≥ 80/100,
    Sunlit Clay 50–80, Copperwood < 50) ; forme = mode de la ligne principale
    (● tram, ■ bus, ▲ ferry) ; taille = nombre de passages analysés, qui suit
    le zoom. Les quais d'un même arrêt sont regroupés en vue éloignée et
    séparés en vue rapprochée ; l'arrêt sélectionné est entouré d'un halo.
    """
    if stops.empty:
        st.info("Aucun arrêt exploitable sur ce périmètre pour la période.")
        return
    carte_arrets(map_payload(stops, groups, selected_id, commune, focus_on_load=selected_id is not None),
                 key="carte_arrets", on_click=_on_map_click)
    st.caption(
        "Cliquez sur un arrêt pour ouvrir sa fiche. En vue éloignée, les quais d'un même arrêt (les deux sens, "
        "parfois d'autres lignes) forment un seul marqueur, à la couleur du quai le moins fiable ; en zoomant, "
        "chaque quai apparaît séparément. Lecture non visuelle de la carte : "
        f"{len(stops)} quais ({len(groups)} arrêts), score de fiabilité de {stops['score_fiabilite'].min():.0f} à "
        f"{stops['score_fiabilite'].max():.0f}/100 (Olive Leaf ≥ 80 = bon, Sunlit Clay 50–80 = moyen, "
        "Copperwood < 50 = à surveiller ; ● tram, ■ bus, ▲ ferry)."
    )


NAV_ITEMS = [
    "Mon territoire", "Lignes", "Quand ?", "Réseau & modes", "Perturbations", "Données & méthode",
]

def _logo_data_uri() -> str:
    logo = Path(__file__).resolve().parents[1] / "assets" / "logo" / "urban-vision-logo-white.png"
    try:
        return "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode("ascii")
    except OSError:
        return ""

def render_sidebar() -> str:
    with st.sidebar:
        st.markdown(
            f'<div class="sidebar-brand"><img src="{_logo_data_uri()}" alt="Urban Vision"/>'
            f'<span>Urban Vision</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="sidebar-nav-label">Navigation</div>', unsafe_allow_html=True)
        page = st.radio("Navigation", NAV_ITEMS, index=0, label_visibility="collapsed", key="sidebar_nav")
    return page

def _kpi_border(polarity: str) -> str:
    if polarity in ("positif", "good"):
        return OLIVE_LEAF
    if polarity in ("negatif", "négatif", "bad"):
        return COPPERWOOD
    if polarity in ("moyen",):
        return SUNLIT_CLAY
    return "rgba(221, 161, 94, 0.50)"

def kpi_card(label: str, value: str, sublabel: str | None = None, polarity: str = "neutral") -> str:
    border = _kpi_border(polarity)
    sub = f'<div class="kpi-sublabel">{sublabel}</div>' if sublabel else ""
    return (
        f'<div class="kpi-card" style="border-left-color:{border}">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>{sub}</div>'
    )

def render_kpis(items) -> None:
    cols = st.columns(len(items))
    for col, (label, value, sublabel, polarity) in zip(cols, items):
        col.markdown(kpi_card(label, value, sublabel, polarity), unsafe_allow_html=True)


PAGE_TERRITORY = "Mon territoire"
PAGE_LINE = "Lignes"
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
        "if(el){el.scrollIntoView({behavior:'smooth',block:'start'});}};"
        "setTimeout(go,150);setTimeout(go,900);})();</script>",
        unsafe_allow_javascript=True,
    )


def select_stop(stop_id: str | None) -> None:
    st.session_state["stop_id"] = stop_id


def show_stop(stop_id: str) -> None:
    """Ouvre la fiche d'un arrêt sur la page courante et y amène l'utilisateur."""
    select_stop(stop_id)
    request_scroll("fiche-arret")


def open_stop(stop_id: str) -> None:
    show_stop(stop_id)
    st.session_state["sidebar_nav"] = PAGE_TERRITORY


def show_line(route_id: str) -> None:
    """Ouvre la fiche d'une ligne sur la page « Lignes » et y amène l'utilisateur."""
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


def _on_line_pick() -> None:
    show_line(st.session_state.get("line_pick"))


def _on_stop_search() -> None:
    show_stop(st.session_state.get("stop_search"))


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
    if params.get("arret"):
        st.session_state["stop_id"] = params["arret"]
        st.session_state["sidebar_nav"] = PAGE_TERRITORY
    elif params.get("ligne"):
        st.session_state["line_id"] = params["ligne"]
        st.session_state["sidebar_nav"] = PAGE_LINE


def stop_labels(territorial: pd.DataFrame) -> dict:
    """Libellé de recherche de chaque arrêt : nom, direction et lignes desservies."""
    out = {}
    for r in territorial.itertuples():
        direction = f" — {r.direction}" if r.direction else ""
        out[r.stop_id] = f"{r.stop_name}{direction} ({r.lignes})"
    return out


def network_score(ranking: pd.DataFrame) -> float:
    """Score de fiabilité de l'ensemble des lignes (totaux pondérés par les passages)."""
    obs = float(ranking["observations"].sum())
    eligible = float(ranking["eligible"].sum())
    if obs <= 0:
        return 0.0
    on_time = float((ranking["observations"] * ranking["pct_a_l_heure"]).sum()) / obs
    skip = float(ranking["skipped"].sum()) / eligible * 100 if eligible else 0.0
    return reliability_score(on_time, skip)


def _delta_polarity(delta: float | None) -> str:
    if delta is None:
        return "neutral"
    if delta >= 2:
        return "positif"
    if delta <= -2:
        return "negatif"
    return "moyen"


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
            f"<span style='color:{OLIVE_LEAF_70}'>{r.debut_effectif} → {r.fin_effective}{flag}</span>",
            unsafe_allow_html=True,
        )
    st.caption(CAUTION_TEXT)


def _render_zone(median_s: float | None, pct_gt300: float, rank_text: str | None) -> None:
    if median_s is None:
        st.info("Pas assez de passages pour qualifier le type de retard.")
        return
    zone = risk_zone(median_s, pct_gt300)
    st.markdown(
        f'<div class="zone-badge">{html.escape(RISK_ZONE_LABELS[zone])}</div>'
        f'<div class="section-note" style="margin-top:.4rem">{html.escape(ZONE_EXPLANATIONS[zone])} '
        f'Retard médian {format_seconds(median_s, signed=True)}, '
        f'{pct_gt300:.1f} % de passages à plus de 5 min (seuils : '
        f'{RISK_MEDIAN_S:.0f} s et {RISK_PCT_GT300:.0f} %).</div>',
        unsafe_allow_html=True,
    )
    if rank_text:
        st.markdown(rank_text)


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
    render_tier_legend("Passages à plus de 5 min", "bon", "à surveiller", invert=True)
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
        render_tier_legend("Retards > 5 min", "bon", "jour dégradé", invert=True)
        hc_render(daily_status_chart(daily, RISK_PCT_GT300), height=240)


def _evolution_kpi(change: dict | None, prev_label: str | None) -> tuple:
    if prev_label is None:
        return ("Évolution", "—", "pas de période de comparaison", "neutral")
    if change is None:
        return ("Évolution", "—", f"pas de données sur {prev_label}", "neutral")
    return ("Évolution", f"{change['delta']:+.1f} pts", f"vs {prev_label} ({change['previous']:.0f} / 100)",
            _delta_polarity(change["delta"]))


def render_stop_panel(conn, cutoff: int, since_ts: int | None, end_ts: int | None, stop_id: str,
                      territorial_network: pd.DataFrame, groups: pd.DataFrame, reference_score: float,
                      commune: str | None = None, prev: tuple = (None, None, None)) -> None:
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
    pct_ok = float(daily["cnt_le300"].sum()) / max(obs, 1) * 100
    pct_gt300 = cnt_gt300 / max(obs, 1) * 100
    pct_skip = skipped / eligible * 100 if eligible else 0.0
    score = reliability_score(pct_ok, pct_skip)
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
    zone = risk_zone(median, pct_gt300) if median is not None else None
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
        f'{" — " + html.escape(direction) if direction else ""}</div>'
        f'<div class="fiche-sub">Lignes : {glyph_lines} · lien direct : cette page (paramètre '
        f'<code>?arret={html.escape(stop_id)}</code>)</div>',
        unsafe_allow_html=True,
    )
    scroll_if_requested("fiche-arret")
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
    cols[-1 if siblings else 0].button("↑ Revenir à la carte", key="back_to_map", on_click=request_scroll,
                                       args=("carte",))
    render_kpis([
        ("Score de fiabilité", f"{score:.0f} / 100", f"réseau : {reference_score:.0f} / 100",
         palette_kpi_tier({"fiability": score}, "fiability")),
        _evolution_kpi(change, prev_label),
        ("Passages à plus de 5 min", f"{pct_gt300:.1f} %", f"{cnt_gt300 / max(days, 1):.0f} par jour en moyenne",
         palette_kpi_tier({"retard_5min": pct_gt300}, "retard_5min")),
        ("Arrêts sautés", f"{pct_skip:.1f} %", f"{skipped:,} passages non desservis".replace(",", " "),
         palette_kpi_tier({"skip_rate": pct_skip}, "skip_rate")),
        ("Échantillon", f"{obs:,}".replace(",", " ") + " passages",
         f"{days} jour{'s' if days > 1 else ''} · "
         + ("échantillon faible" if obs < MIN_OBSERVATIONS else "échantillon suffisant"),
         "neutral"),
    ])
    _render_brief(summary, hints)
    if responsible is not None:
        st.button(f"Ouvrir la fiche de la ligne {responsible['ligne']} → où se forme son retard, à quel moment",
                  type="primary", on_click=line_from_stop, args=(responsible["route_id"], stop_id, name),
                  key="open_line_from_stop")

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
                st.caption("Colonnes : retard pris sur chaque tronçon (en Copperwood, le tronçon amont qui en "
                           "prend le plus, s'il pèse au moins un quart du retard importé) ; courbe : retard "
                           "moyen à l'arrêt. 8 arrêts en amont, 2 en aval.")
    elif view == "Quand ?":
        _render_stop_when(conn, cutoff, since_ts, end_ts, stop_id, name, lines, daily, hourly_all, seg_ok, commune)
    elif view == "Quel type ?":
        rank_text = None
        if rank is not None:
            rank_text = (f"Cet arrêt fait partie des **{rank:.0f} %** d'arrêts les moins fiables du réseau."
                         if rank <= 50 else f"Cet arrêt est plus fiable que **{rank:.0f} %** des arrêts du réseau.")
        _render_zone(median, pct_gt300, rank_text)
        with st.expander("Répartition des écarts à l'horaire"):
            render_tier_legend("Écart à l'horaire", "proche de l'horaire", "dérive", invert=True)
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
            hc_render(slot_profile_chart(sp, dg.slot_label(wd, hr), highlight_stop_id=focus_stop,
                                         commune_stop_ids=_commune_ids(profile, commune),
                                         commune_label=commune), height=340)
            st.markdown(dg.slot_hotspot_sentence(dg.slot_hotspot(sp)))
            st.caption("Courbe pleine : retard moyen des passages de chaque arrêt à ce moment-là ; tirets : le "
                       "reste du temps. L'écart entre les deux courbes montre où ce moment-là se dégrade.")
    if not core.empty:
        with st.expander("Jour par jour sur la période"):
            render_tier_legend("Retards > 5 min", "bon", "jour dégradé", invert=True)
            hc_render(daily_status_chart(core, RISK_PCT_GT300), height=240)


def render_line_panel(conn, cutoff: int, since_ts: int | None, end_ts: int | None, route_id: str,
                      ranking_net: pd.DataFrame, disturbed: set, commune: str | None = None,
                      prev: tuple = (None, None, None)) -> None:
    """Fiche diagnostic d'une ligne : pourquoi elle n'est pas fiable, où, quand, et quelles pistes."""
    row = ranking_net[ranking_net["route_id"] == route_id]
    if row.empty:
        st.info("Aucun passage analysé pour cette ligne sur la période.")
        return
    line = row.iloc[0]
    breakdown = dg.score_breakdown(line["pct_a_l_heure"], line["pct_arrets_sautes"])
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
        f'{n_communes} commune(s) desservie(s) · {per_day} passages par jour · lien direct : paramètre '
        f'<code>?ligne={html.escape(route_id)}</code></div>',
        unsafe_allow_html=True,
    )
    scroll_if_requested("fiche-ligne")
    back = st.session_state.get("_from_stop")
    focus_stop = back[0] if back else None
    from_dir = stop_direction_in(per_dir, focus_stop)
    if st.session_state.pop("_dir_from_stop", None) and from_dir is not None:
        for key in LINE_DIRECTION_KEYS:
            st.session_state[key] = from_dir
    if back:
        st.button(f"← Revenir à l'arrêt {back[1]}", key="back_to_stop", on_click=open_stop, args=(back[0],))
    if route_id in disturbed:
        st.warning(CAUTION_TEXT)
    ref = f"réseau : {network_score(ranking_net):.0f}"
    if mode_median is not None:
        ref += f" · médiane {str(line['mode']).lower()} : {mode_median:.0f}"
    render_kpis([
        ("Score de fiabilité", f"{line['score_fiabilite']:.0f} / 100", ref,
         palette_kpi_tier({"fiability": line["score_fiabilite"]}, "fiability")),
        _evolution_kpi(change, prev_label),
        ("Points perdus : retards", f"{breakdown['lost_delay']:.0f}",
         f"{line['pct_retard_5min']:.1f} % de passages à plus de 5 min",
         palette_kpi_tier({"retard_5min": line["pct_retard_5min"]}, "retard_5min")),
        ("Points perdus : arrêts sautés", f"{breakdown['lost_skip']:.0f}",
         f"{line['pct_arrets_sautes']:.1f} % d'arrêts sautés",
         palette_kpi_tier({"skip_rate": line["pct_arrets_sautes"]}, "skip_rate")),
        ("Courses supprimées", f"{cancelled}", f"sur {trips:,} courses connues".replace(",", " "),
         "neutral"),
    ])
    _render_brief(summary, hints)

    view = st.segmented_control("Détail", LINE_VIEWS, default=LINE_VIEWS[0], key="line_view",
                                label_visibility="collapsed") or LINE_VIEWS[0]
    commune_note = (f" Fond Cornsilk : arrêts situés à {commune} "
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
                       f"{labels[d['origin']['verdict']]}. En Copperwood, les 3 tronçons qui prennent le plus "
                       f"de retard.{commune_note}")
            if d["origin"]["hotspots"]:
                top = pd.DataFrame(d["origin"]["hotspots"])
                top["Tronçon"] = top["from"] + " → " + top["to"]
                top["Retard pris"] = top["gain_s"].map(lambda v: format_seconds(v, signed=True))
                st.dataframe(top[["Tronçon", "commune", "Retard pris"]].rename(columns={"commune": "Commune"}),
                             hide_index=True, width="stretch")
        else:
            if cancelled:
                st.markdown(f"#### Courses supprimées ({cancelled} sur {trips:,})".replace(",", " "))
                hc_render(cancellations_chart(canc[canc["cancelled"] > 0]), height=240)
            st.markdown("#### Arrêts sautés le long de la ligne")
            render_tier_legend("Arrêts sautés", "bon", "à surveiller", invert=True)
            hc_render(skip_profile_chart(d["profile"], commune_stop_ids=ids, commune_label=commune,
                                         highlight_stop_id=focus_stop), height=300)
            labels = {"extrémités": "surtout aux extrémités (prises ou fins de service en cours de ligne)",
                      "bloc": "en bloc sur une section (déviation probable)",
                      "dispersé": "dispersés le long de la ligne", "aucun": "rares"}
            st.caption(f"{d['terminus'].capitalize()} : arrêts sautés {labels[d['skips']['verdict']]}.{commune_note}")
    elif view == "Quand ?":
        _render_line_when(conn, cutoff, since_ts, end_ts, route_id, core, hourly, peak, per_dir, main_dir, commune,
                          focus_stop)
    else:
        _render_zone(line["retard_median_s"], float(line["pct_retard_5min"]), None)
        st.markdown("#### Perturbations signalées")
        _render_alerts(_alerts_overlapping(load_perturbation_history(conn, since_ts, end_ts, cutoff),
                                           {route_id}, rec["bad_dates"]))
        with st.expander("Répartition des écarts à l'horaire"):
            render_tier_legend("Écart à l'horaire", "proche de l'horaire", "dérive", invert=True)
            hc_render(delay_distribution_chart(load_distribution(conn, cutoff, since_ts, route_id, end_ts)),
                      height=280)

    if not stops.empty:
        st.markdown("#### Arrêts les plus touchés de la ligne")
        st.caption("Sélectionnez une ligne du tableau pour ouvrir la fiche de l'arrêt.")
        top = stops[stops["obs"] >= 1].head(10)
        st.session_state["_line_stop_ids"] = top["stop_id"].tolist()
        table = top[["stop_name", "direction", "commune", "cnt_gt300", "skipped", "score_fiabilite"]].copy()
        table.columns = ["Arrêt", "Direction", "Commune", "Passages > 5 min", "Arrêts sautés", "Score / 100"]
        st.dataframe(
            table.style.map(_score_tier_style, subset=["Score / 100"]).format(
                {"Score / 100": "{:.0f}", "Passages > 5 min": fmt_int, "Arrêts sautés": fmt_int}),
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


def _verdict(text: str) -> None:
    st.markdown(f'<div class="insight">{text}</div>', unsafe_allow_html=True)


def _tier_word(score: float) -> str:
    return {"positif": "bon", "moyen": "moyen", "negatif": "à surveiller"}[
        palette_kpi_tier({"fiability": score}, "fiability")]


def watchlist_rule(prev_label: str | None) -> str:
    """Règle de choix du bloc « À surveiller », affichée sous les cartes et dans la méthode."""
    decline = (f"la ligne dont la baisse de score par rapport à {prev_label} pèse le plus (baisse d'au moins "
               "5 points, pondérée par les passages)" if prev_label else
               "la ligne dont la baisse de score pèse le plus (seulement si une période de comparaison existe)")
    return ("Comment ces éléments sont choisis : **ligne en baisse** = " + decline + " ; **arrêt** = l'arrêt "
            "qui a le plus de passages à plus de 5 min parmi ceux dont le score est inférieur à 80/100 (les "
            "quais d'un même arrêt sont comptés ensemble) ; **ligne** = la ligne qui a le plus de passages à "
            "plus de 5 min parmi celles sous 80/100.")


def _render_watchlist(items: list[dict], prev_label: str | None = None) -> None:
    if not items:
        return
    st.markdown("#### À surveiller")
    cols = st.columns(len(items))
    for i, (col, item) in enumerate(zip(cols, items)):
        with col:
            st.markdown(
                f'<div class="kpi-card" style="border-left-color:{COPPERWOOD}">'
                f'<div class="kpi-label">{html.escape(item["kind"].capitalize())}</div>'
                f'<div class="watch-title">{html.escape(str(item["title"]))}</div>'
                f'<div class="kpi-sublabel">{html.escape(item["reason"])}</div></div>',
                unsafe_allow_html=True,
            )
            opener = open_line if item["kind"] == "ligne" else show_stop
            st.button("Ouvrir la fiche", key=f"watch_{i}", on_click=opener, args=(item["id"],))
    st.caption(watchlist_rule(prev_label))


def render_page_territory(c: PageContext) -> None:
    territorial = load_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    territorial_network = load_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=None)
    reference = network_score(c.ranking_net)
    if territorial.empty:
        st.info("Aucun arrêt exploitable sur ce périmètre pour la période.")
    elif c.commune is not None:
        local = _territorial_score(territorial)
        gap = local - _territorial_score(territorial_network)
        comparison = ("au niveau de l’ensemble du réseau" if abs(gap) < 1
                      else f"{abs(gap):.0f} point(s) {'au-dessus' if gap > 0 else 'en dessous'} de l’ensemble du réseau")
        _verdict(f'La fiabilité de <b>{html.escape(c.commune)}</b> est de <b>{local:.0f}/100</b> '
                 f'({_tier_word(local)}), {comparison}.')
    else:
        _verdict(f'Le réseau obtient un score de fiabilité de <b>{reference:.0f}/100</b> '
                 f'({_tier_word(reference)}). Les points ci-dessous méritent une attention en priorité ; '
                 f'cliquez sur un arrêt de la carte pour comprendre d’où vient son problème.')
    groups = grouped_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    prog = load_engagement_progression(c.conn, c.cutoff, c.since_ts, c.end_ts, c.prev_since_ts, c.prev_end_ts,
                                       commune=c.commune)
    _render_watchlist(dg.watchlist(prog, c.visible_ranking, groups, MIN_OBSERVATIONS, prev_label=c.period.prev_label),
                      c.period.prev_label)

    st.markdown('<div class="carte-anchor"></div>', unsafe_allow_html=True)
    scroll_if_requested("carte")
    st.markdown("#### Carte des arrêts")
    st.markdown('<div class="section-note">Couleur : score de fiabilité de l’arrêt, toutes lignes '
                'confondues. Forme : mode de la ligne principale (● tram, ■ bus, ▲ ferry). Taille : '
                'nombre de passages analysés ; les marqueurs grossissent quand on zoome.</div>', unsafe_allow_html=True)
    render_tier_legend("Fiabilité par arrêt", "à surveiller", "bon")
    labels = stop_labels(territorial_network)
    options = sorted(territorial["stop_id"].tolist() if not territorial.empty else [],
                     key=lambda sid: labels.get(sid, sid))
    current = st.session_state.get("stop_id")
    if current and current not in options:
        options = [current] + options
    st.session_state["stop_search"] = current
    st.selectbox("Chercher un arrêt", options, key="stop_search", placeholder="Nom de l'arrêt…",
                 format_func=lambda sid: labels.get(sid, sid), on_change=_on_stop_search)
    _territorial_map(territorial, groups, c.commune, st.session_state.get("stop_id"))
    if st.session_state.get("stop_id"):
        render_stop_panel(c.conn, c.cutoff, c.since_ts, c.end_ts, st.session_state["stop_id"],
                          territorial_network,
                          grouped_territorial(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=None),
                          reference, c.commune, (c.prev_since_ts, c.prev_end_ts, c.period.prev_label))

    if c.commune is None:
        with st.expander("Comparer les communes"):
            communes = load_commune_stats(c.conn, c.cutoff, c.since_ts, c.end_ts)
            if communes.empty:
                st.info("Aucune donnée par commune pour cette période.")
            else:
                worst_c, best_c = communes.iloc[0], communes.iloc[-1]
                st.markdown(
                    f'Sur {len(communes)} communes, la fiabilité s’étend de '
                    f'**{best_c["commune"]}** ({best_c["score_fiabilite"]:.1f}/100) à '
                    f'**{worst_c["commune"]}** ({worst_c["score_fiabilite"]:.1f}/100).'
                )
                left, right = st.columns([1.0, 1.0], gap="large")
                with left:
                    render_tier_legend("Score de fiabilité", "à surveiller", "bon")
                    hc_render(commune_ranking_chart(communes), height=460)
                with right:
                    table = communes[["commune", "score_fiabilite", "pct_a_l_heure", "pct_retard_5min",
                                      "retard_moyen_s", "pct_arrets_sautes", "n_lignes", "observations"]].copy()
                    table.columns = ["Commune", "Score / 100", "Ponctualité ≤ 5 min", "Retards > 5 min",
                                     "Retard moyen", "Arrêts sautés", "Lignes", "Passages"]
                    st.dataframe(
                        table.style.map(_score_tier_style, subset=["Score / 100"]).format({
                            "Score / 100": "{:.1f}", "Ponctualité ≤ 5 min": "{:.1f} %",
                            "Retards > 5 min": "{:.1f} %", "Retard moyen": lambda x: format_seconds(x),
                            "Arrêts sautés": "{:.2f} %", "Lignes": "{:.0f}", "Passages": fmt_int}),
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
                    {"Score / 100": "{:.1f}", "Retards > 5 min": "{:.1f} %", "Passages": fmt_int}),
                width="stretch", hide_index=True, height=320, key="territory_table",
                on_select=lambda: _on_table_select("territory_table", "_territory_stop_ids", show_stop),
                selection_mode="single-row")


def render_page_lines(c: PageContext) -> None:
    st.markdown("### Quelles lignes posent problème, et pourquoi ?")
    worst = c.ranking.iloc[0]
    worst_note = " — ⚠ perturbation signalée sur la période" if worst.route_id in c.disturbed else ""
    _verdict(f'À examiner en premier : <b>ligne {html.escape(str(worst.ligne))}</b>{worst_note}, score '
             f'{worst.score_fiabilite:.0f}/100, avec {worst.pct_retard_5min:.1f} % de passages au-delà de '
             f'5 minutes. Sélectionnez une ligne dans le tableau pour ouvrir sa fiche.')
    left, right = st.columns([1.0, 1.0], gap="large")
    with left:
        st.markdown("#### Les 15 lignes les moins fiables")
        render_tier_legend(invert=False)
        chart_data = c.visible_ranking.head(15).sort_values("score_fiabilite").copy()
        chart_data["ligne_plot"] = [f"⚠ {l}" if rid in c.disturbed else l
                                    for l, rid in zip(chart_data["ligne"], chart_data["route_id"])]
        hc_render(ranking_chart(chart_data), height=390)
    with right:
        st.markdown("#### Toutes les lignes")
        table_src = c.visible_ranking.reset_index(drop=True)
        st.session_state["_lines_table_ids"] = table_src["route_id"].tolist()
        display = pd.DataFrame({
            "Ligne": [f"{mode_glyph(rt)} {'⚠ ' if rid in c.disturbed else ''}{l}"
                      for rt, rid, l in zip(table_src["route_type"], table_src["route_id"], table_src["ligne"])],
            "Score / 100": table_src["score_fiabilite"],
            "Retards > 5 min": table_src["pct_retard_5min"],
            "Arrêts sautés": table_src["pct_arrets_sautes"],
            "Passages": table_src["observations"],
        })
        st.dataframe(
            display.style.map(_score_tier_style, subset=["Score / 100"]).format({
                "Score / 100": "{:.0f}", "Retards > 5 min": "{:.1f} %", "Arrêts sautés": "{:.2f} %",
                "Passages": fmt_int}),
            width="stretch", hide_index=True, height=390, key="lines_table",
            on_select=lambda: _on_table_select("lines_table", "_lines_table_ids", show_line),
            selection_mode="single-row")
    if c.disturbed:
        st.caption(f"⚠ {len(c.disturbed & set(c.ranking['route_id']))} ligne(s) du périmètre font l'objet "
                   "d'une perturbation signalée par TBM sur la période — sans lien de causalité établi avec "
                   "les statistiques présentées.")
    if c.commune is not None:
        st.caption("Classement restreint aux arrêts de la commune ; la fiche porte sur toute la ligne, car "
                   "le retard subi dans une commune se forme souvent ailleurs sur le parcours.")

    options_df = c.ranking_net[c.ranking_net["observations"] >= MIN_OBSERVATIONS]
    if options_df.empty:
        options_df = c.ranking_net
    line_labels = {r.route_id: f"{mode_glyph(r.route_type)} Ligne {r.ligne} · score {r.score_fiabilite:.0f}/100"
                   for r in options_df.itertuples()}
    line_options = list(line_labels)
    if st.session_state.get("line_id") is None and line_options:
        st.session_state["line_id"] = str(c.ranking.iloc[0]["route_id"])
    current_line = st.session_state.get("line_id")
    if current_line and current_line not in line_options:
        line_options = [current_line] + line_options
    st.session_state["line_pick"] = current_line
    st.selectbox("Ligne analysée", line_options, key="line_pick",
                 format_func=lambda rid: line_labels.get(rid, rid), on_change=_on_line_pick)
    if st.session_state.get("line_id"):
        render_line_panel(c.conn, c.cutoff, c.since_ts, c.end_ts, st.session_state["line_id"],
                          c.ranking_net, c.disturbed, c.commune,
                          (c.prev_since_ts, c.prev_end_ts, c.period.prev_label))


def _period_verdict(period: pd.DataFrame) -> str | None:
    table = period.assign(obs=period["observations"],
                          cnt_gt300=period["observations"] * period["pct_retard_5min"] / 100,
                          pct_gt300=period["pct_retard_5min"])
    conc = dg.concentration(table, "période")
    if conc is None:
        return ("Aucun créneau ne se détache nettement : les retards se répartissent sur l'ensemble de la "
                "journée et de la semaine.")
    return (f"Le créneau le plus difficile est <b>{html.escape(conc['label'])}</b> : "
            f"{conc['pct']:.1f} % de passages à plus de 5 min, contre {conc['rest_pct']:.1f} % le reste du temps.")


def render_page_when(c: PageContext) -> None:
    st.markdown("### Quand les problèmes surviennent-ils ?")
    view = st.segmented_control("Vue", ["Selon le créneau", "Dans le temps"], default="Selon le créneau",
                                key="when_view", label_visibility="collapsed") or "Selon le créneau"
    if view == "Selon le créneau":
        period = load_period_stats(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
        if period.empty:
            st.info("Aucune donnée horaire disponible sur ce périmètre pour la période.")
            return
        _verdict(_period_verdict(period))
        st.markdown('<div class="section-note">Matin (06–10), Journée (10–16), Pointe du soir (16–20), '
                    'Soirée & nuit (20–06) du lundi au vendredi, et Week-end. Les arrêts sautés n’y sont '
                    'pas décomptés.</div>', unsafe_allow_html=True)
        left, right = st.columns([1.05, 0.95], gap="large")
        with left:
            st.markdown("#### Ponctualité par créneau")
            render_tier_legend("Ponctualité ≤ 5 min", "à surveiller", "bon")
            hc_render(period_punctuality_chart(period), height=300)
        with right:
            st.markdown("#### Retards > 5 min par mode et créneau")
            render_tier_legend("Retards > 5 min", "bon", "à surveiller", invert=True)
            pm = load_period_mode(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
            if pm.empty:
                st.info("Aucune donnée par mode sur ce périmètre.")
            else:
                hc_render(period_mode_chart(pm), height=300)
        with st.expander("Lignes les moins ponctuelles d'un créneau"):
            selected_period = st.selectbox("Créneau", period["période"].tolist(), key="period_selector")
            lines = load_period_lines(c.conn, c.cutoff, c.since_ts, c.end_ts,
                                      periode=selected_period, commune=c.commune)
            if lines.empty:
                st.info(f"Aucune ligne n'atteint le seuil de {MIN_OBSERVATIONS} passages sur le créneau "
                        f"{selected_period}.")
            else:
                display = lines[["ligne", "mode", "observations", "pct_a_l_heure", "pct_retard_5min",
                                 "retard_moyen_s"]].copy()
                display["ligne"] = [f"{mode_glyph(rt)} {l}" for rt, l in zip(lines["route_type"], lines["ligne"])]
                display.columns = ["Ligne", "Mode", "Passages", "Ponctualité ≤ 5 min", "Retards > 5 min",
                                   "Retard moyen"]
                st.dataframe(display.style.map(_score_tier_style, subset=["Ponctualité ≤ 5 min"]).format({
                    "Passages": fmt_int, "Ponctualité ≤ 5 min": "{:.1f} %", "Retards > 5 min": "{:.1f} %",
                    "Retard moyen": lambda x: format_seconds(x)}), width="stretch", hide_index=True, height=320)
        return

    trend = load_engagement_trend(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if trend.empty:
        st.info("Aucune donnée quotidienne sur ce périmètre pour la période.")
        return
    previous = (load_engagement_trend(c.conn, c.cutoff, c.prev_since_ts, c.prev_end_ts, commune=c.commune)
                if c.prev_since_ts is not None else pd.DataFrame())

    def _weighted_mean(part: pd.DataFrame, col: str) -> float:
        return float((part["observations"] * part[col]).sum()) / max(float(part["observations"].sum()), 1)

    ponct = _weighted_mean(trend, "pct_a_l_heure")
    skip = _weighted_mean(trend, "pct_arrets_sautes")
    if previous.empty:
        missing = (f"pas de données sur {c.period.prev_label}" if c.period.prev_label
                   else "pas de période de comparaison")
        _verdict(f'Sur {html.escape(c.period.label)}, la ponctualité (≤ 5 min) est de <b>{ponct:.1f} %</b> et '
                 f'les arrêts sautés de <b>{skip:.2f} %</b> ({missing}).')
    else:
        p_ponct, p_skip = _weighted_mean(previous, "pct_a_l_heure"), _weighted_mean(previous, "pct_arrets_sautes")
        delta_ponct = ponct - p_ponct
        arrow = "▲" if delta_ponct >= 0 else "▼"
        delta_color = OLIVE_LEAF if delta_ponct >= 0 else COPPERWOOD
        _verdict(f'Sur {html.escape(c.period.label)}, la ponctualité (≤ 5 min) est de <b>{ponct:.1f} %</b>, soit '
                 f'<span style="color:{delta_color}"><b>{arrow}{abs(delta_ponct):.1f} point(s)</b></span> par '
                 f'rapport à {html.escape(c.period.prev_label)}. Les arrêts sautés passent de '
                 f'<b>{p_skip:.2f} %</b> à <b>{skip:.2f} %</b>.')
    st.markdown("#### Ponctualité jour par jour")
    hc_render(engagement_trend_chart(trend, "pct_a_l_heure"), height=340)
    with st.expander("Suivre un autre indicateur"):
        trend_metrics = [("Retards > 5 min", "pct_retard_5min"), ("Arrêts sautés", "pct_arrets_sautes"),
                         ("Retard moyen", "retard_moyen_s")]
        metric_label = st.selectbox("Indicateur", [label for label, _ in trend_metrics], key="trend_metric")
        hc_render(engagement_trend_chart(trend, dict(trend_metrics)[metric_label]), height=340)
    st.markdown("#### Lignes qui se dégradent ou s'améliorent")
    prog = load_engagement_progression(c.conn, c.cutoff, c.since_ts, c.end_ts, c.prev_since_ts, c.prev_end_ts,
                                       commune=c.commune)
    if prog.empty:
        st.info(f"Comparaison impossible : aucune ligne ne cumule au moins {MIN_OBSERVATIONS} passages sur "
                f"{c.period.label} et sur {c.period.prev_label or 'une période précédente'}.")
        return
    worst, best = prog.iloc[0], prog.iloc[-1]
    st.markdown(f"Par rapport à {c.period.prev_label}, la plus forte dégradation concerne la **ligne "
                f"{worst['ligne']}** ({worst['delta_score']:+.1f} points de score), la meilleure progression la "
                f"**ligne {best['ligne']}** ({best['delta_score']:+.1f} points).")
    hc_render(engagement_progression_chart(prog), height=330)
    with st.expander("Détail par ligne"):
        table = prog[["ligne", "mode", "score_fiabilite_prev", "score_fiabilite", "delta_score",
                      "pct_a_l_heure", "pct_arrets_sautes", "observations"]].copy()
        table.columns = ["Ligne", "Mode", "Score avant", "Score", "Évolution", "Ponctualité", "Arrêts sautés",
                         "Passages"]
        st.dataframe(table.style.map(_delta_style, subset=["Évolution"]).format({
            "Score avant": "{:.1f}", "Score": "{:.1f}", "Évolution": lambda x: f"{x:+.1f} pts",
            "Ponctualité": "{:.1f} %", "Arrêts sautés": "{:.2f} %",
            "Passages": fmt_int}), width="stretch", hide_index=True, height=330)
        st.caption(f"« Score avant » : {c.period.prev_label} ; les autres colonnes : {c.period.label}. Score de "
                   f"fiabilité = ponctualité ≤ 5 min − 2 × arrêts sautés (borné 0–100). Seuil : "
                   f"{MIN_OBSERVATIONS} passages sur chacune des deux périodes.")


def render_page_network(c: PageContext) -> None:
    st.markdown("### Le réseau et ses modes, pour situer un problème")
    view = st.segmented_control("Vue", ["Réseau", "Modes de transport"], default="Réseau", key="network_view",
                                label_visibility="collapsed") or "Réseau"
    if view == "Réseau":
        _verdict(f'Score de fiabilité du périmètre : <b>{network_score(c.ranking):.0f}/100</b>. La carte de '
                 f'risque situe chaque ligne selon la durée typique de ses retards (retard médian) et leur '
                 f'fréquence (part des passages à plus de 5 min).')
        st.markdown("#### Carte de risque des lignes")
        hc_render(scatter_chart(c.visible_ranking), height=390)
        st.caption(f"Chaque bulle est une ligne. Couleur : son score de fiabilité ; forme : son mode (● tram, "
                   "■ bus, ▲ ferry) ; taille : son nombre de passages analysés sur la période (échelle sous le "
                   "graphique), c'est-à-dire son poids dans le réseau. Traits pointillés : seuils de "
                   f"{RISK_MEDIAN_S:.0f} s et {RISK_PCT_GT300:.0f} %. En haut à droite, retards fréquents et longs "
                   "(zone critique) ; en bas à droite, retards fréquents mais courts ; en haut à gauche, retards "
                   "rares mais longs.")
        daily = load_network_daily(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("#### Retards > 5 min par jour")
            render_tier_legend("Retards", "bon", "à surveiller", invert=True)
            if daily.empty or len(daily) < 2:
                st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
            else:
                hc_render(network_daily_chart(daily), height=300)
        with right:
            st.markdown("#### Retards > 5 min selon l'heure")
            render_tier_legend("Retards", "bon", "à surveiller", invert=True)
            net_hourly = load_hourly(c.conn, c.cutoff, c.since_ts, end_ts=c.end_ts, commune=c.commune)
            if net_hourly.empty:
                st.info("Cette vue nécessite les heures de départ des observations.")
            else:
                hc_render(network_hourly_chart(net_hourly), height=300)
        with st.expander("Répartition des écarts à l'horaire"):
            distribution = load_distribution(c.conn, c.cutoff, c.since_ts, end_ts=c.end_ts, commune=c.commune)
            if not distribution.empty:
                render_tier_legend("Écart à l'horaire", "proche de l'horaire", "dérive", invert=True)
                hc_render(delay_distribution_chart(distribution), height=280)
        with st.expander("Tableau détaillé des lignes"):
            display = c.visible_ranking[["ligne", "mode", "score_fiabilite", "pct_a_l_heure", "retard_moyen_s",
                                         "retard_median_s", "pct_retard_5min", "pct_arrets_sautes",
                                         "observations"]].copy()
            display["ligne"] = [f"⚠ {l}" if rid in c.disturbed else l
                                for l, rid in zip(display["ligne"], c.visible_ranking["route_id"])]
            display.columns = ["Ligne", "Mode", "Score / 100", "Ponctualité ≤ 5 min", "Retard moyen (s)",
                               "Retard médian (s)", "Retards > 5 min", "Arrêts sautés", "Passages"]
            st.dataframe(display.style.map(_score_tier_style, subset=["Score / 100"]).format({
                "Score / 100": "{:.1f}", "Ponctualité ≤ 5 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}",
                "Retard médian (s)": "{:.0f}", "Retards > 5 min": "{:.1f} %", "Arrêts sautés": "{:.2f} %",
                "Passages": fmt_int}), width="stretch", hide_index=True, height=330)
        return

    mode_stats = load_mode_stats(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if mode_stats.empty:
        st.warning("Aucune donnée exploitable par mode.")
        return
    worst_mode = mode_stats.sort_values("pct_a_l_heure").iloc[0]
    _verdict(f'Le mode le moins ponctuel du périmètre est le <b>{html.escape(str(worst_mode["mode"]).lower())}</b> '
             f'({worst_mode["pct_a_l_heure"]:.1f} % de passages à l’heure). Tram, bus et ferry n’ont pas les '
             f'mêmes contraintes : les comparer aide à distinguer un problème de ligne d’un problème de mode.')
    card_html = '<div style="display:flex;gap:1rem;margin-bottom:.2rem;flex-wrap:wrap">'
    for r in mode_stats.itertuples():
        ponct_color = palette_hex(r.pct_a_l_heure, "score")
        card_html += (
            f'<div style="flex:1 1 0;min-width:220px;background:#ffffff;border:1px solid rgba(221,161,94,.35);'
            f'border-left:4px solid {ponct_color};border-radius:10px;padding:.9rem 1rem;box-shadow:0 1px 3px rgba(40,54,24,.08)">'
            f'<div style="font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;font-weight:600;color:{OLIVE_LEAF_70}">{mode_glyph(r.route_type)} {r.mode}</div>'
            f'<div style="font-size:1.9rem;font-weight:700;color:{BLACK_FOREST};line-height:1.15">{r.pct_a_l_heure:.1f} %</div>'
            f'<div style="font-size:.82rem;color:{OLIVE_LEAF_70}">à l’heure · {fmt_int(r.observations)} passages · '
            f'retard médian {format_seconds(r.retard_median_s, signed=True)} · {r.pct_retard_5min:.1f} % &gt; 5 min</div>'
            f'</div>'
        )
    st.markdown(card_html + '</div>', unsafe_allow_html=True)
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("#### Comparaison des indicateurs")
        hc_render(mode_comparison_chart(mode_stats), height=330)
    with right:
        st.markdown("#### Retards > 5 min selon l'heure")
        mh = load_mode_hourly(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
        if mh.empty:
            st.info("Aucune donnée horaire par mode.")
        else:
            hc_render(mode_hourly_chart(mh), height=330)
    st.markdown("#### Évolution quotidienne par mode")
    md = load_mode_daily(c.conn, c.cutoff, c.since_ts, c.end_ts, commune=c.commune)
    if md.empty or md["date_service"].nunique() < 2:
        st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
    else:
        st.caption("Chaque mode a son trait et sa forme de marqueur (● tram trait plein, ■ bus tirets, "
                   "▲ ferry pointillés) ; la couleur des marqueurs indique le palier.")
        hc_render(mode_daily_chart(md), height=300)
    with st.expander("Tableau par mode"):
        table = mode_stats[["mode", "observations", "pct_a_l_heure", "pct_retard_5min", "pct_avance_1min",
                            "retard_moyen_s", "retard_median_s", "pct_arrets_sautes"]].copy()
        table.columns = ["Mode", "Passages", "Ponctualité ≤ 5 min", "Retards > 5 min", "En avance > 1 min",
                         "Retard moyen (s)", "Retard médian (s)", "Arrêts sautés"]
        st.dataframe(table.style.format({
            "Passages": fmt_int, "Ponctualité ≤ 5 min": "{:.1f} %", "Retards > 5 min": "{:.1f} %",
            "En avance > 1 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}", "Retard médian (s)": "{:.0f}",
            "Arrêts sautés": "{:.2f} %"}), width="stretch", hide_index=True, height=220)


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
                            f"<span style='color:{OLIVE_LEAF_70}'>{x.debut_effectif} → "
                            f"{x.fin_effective} · {x.jours_couverts} j</span>",
                            unsafe_allow_html=True,
                        )
                        st.write(x.description_text or "(description non fournie)")
                        st.markdown("")
                    if len(items) > 6:
                        st.caption(f"+ {len(items) - 6} autre(s) alerte(s) sur cette ligne.")
    st.markdown(f'<div class="section-note">{CAUTION_TEXT}</div>', unsafe_allow_html=True)


def _render_method(c: PageContext) -> None:
    st.markdown("### Ce que mesure ce tableau de bord")
    st.markdown("Les données viennent des flux GTFS-RT **TripUpdates** TBM. Une observation est considérée "
                "stabilisée après 20 minutes hors du flux temps réel ; cela évite d'interpréter comme final un "
                "retard qui peut encore évoluer.")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**Ponctualité**  \nUn passage est classé ponctuel lorsqu'il ne dépasse pas 5 minutes de "
                "retard. Les passages en avance sont conservés pour montrer la distribution réelle.")
    c2.markdown("**Arrêts sautés**  \nLes événements `SKIPPED` sont suivis à part : ils ne gonflent pas "
                "artificiellement le retard moyen, mais pénalisent le score de fiabilité (score = ponctualité "
                "− 2 × taux d'arrêts sautés).")
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
        "- **Points perdus** : le score part de 100 ; les retards de plus de 5 min et les arrêts sautés "
        "(comptés double) en retirent. La fiche ligne dit lequel des deux pèse le plus.\n"
        "- **Évolution** : le score de la période comparé à celui de la période précédente (le mois précédent "
        "pour un mois, la même durée juste avant pour les autres périodes).\n"
        "- **À surveiller** : " + watchlist_rule(c.period.prev_label).split(" : ", 1)[1] + "\n"
        "- **Pistes** : ce sont des indices à confirmer sur le terrain, jamais des conclusions. Les courses "
        "supprimées absentes du flux ne sont pas comptées dans le score ; la fiche ligne les montre à part."
    )
    st.markdown(
        "<div class='section-note'><b>Pourquoi un tram peut-il avoir des arrêts sautés ?</b> "
        "Un événement <code>SKIPPED</code> signifie que le véhicule ne dessert pas un arrêt alors "
        "que le trajet continue. Deux situations courantes l'expliquent :<br/>"
        "• <b>Prise / rendu de service en cours de ligne</b> : le tram ne dessert pas le terminus ou "
        "les premiers arrêts (départ ou fin de service plus loin sur la ligne, retour dépôt, rotation). "
        "Seuls quelques arrêts sont sautés, le reste du trajet circule normalement.<br/>"
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
         "Ponctualité, retards et arrêts sautés par ligne et par jour (avec l'histogramme des écarts)."),
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
                '« passages analysés » sont les passages programmés (SCHEDULED) avec retard connu, hors 20 '
                'dernières minutes. Les arrêts sautés (SKIPPED) sont suivis à part.</div>', unsafe_allow_html=True)
    render_kpis([
        ("Observations (brutes)", f"{stats['total']:,}".replace(",", " "), None, "neutral"),
        ("Passages analysés", f"{stats['analysed']:,}".replace(",", " "), "stabilisés", "neutral"),
        ("Trajets distincts", f"{stats['trajets']:,}".replace(",", " "), None, "neutral"),
        ("Première date", format_date(stats["first_ts"]), None, "neutral"),
        ("Dernière date", format_date(stats["last_ts"]), None, "neutral"),
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
        st.markdown("#### Répartition horaire")
        hourly = stats["hourly"]
        if hourly.empty:
            st.info("Aucune donnée horaire.")
        else:
            hc_render(hourly_distribution_chart(hourly), height=280)


def render_page_data(c: PageContext) -> None:
    views = {"Méthode": _render_method, "Données ouvertes": _render_open_data,
             "Suivi de la collecte": _render_collection}
    view = st.segmented_control("Vue", list(views), default="Méthode", key="data_view",
                                label_visibility="collapsed") or "Méthode"
    views[view](c)


PAGES = {
    "Mon territoire": render_page_territory,
    "Lignes": render_page_lines,
    "Quand ?": render_page_when,
    "Réseau & modes": render_page_network,
    "Perturbations": render_page_perturbations,
    "Données & méthode": render_page_data,
}


def main() -> None:
    st.set_page_config(page_title="Urban Vision | Fiabilité", page_icon="◉", layout="wide")
    inject_style()
    apply_query_params()
    page = render_sidebar()
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
        commune_options = ["Réseau complet (toutes communes)"] + communes
        st.session_state.setdefault("commune_idx", 0)

        # ---- Barre supérieure persistante : identité (sidebar) + commune + plage.
        tb = st.columns([0.62, 0.04, 1.0, 0.04, 0.8])
        with tb[0]:
            st.markdown(
                '<div class="topbar-logo"><span class="dot"></span>Observatoire de la fiabilité</div>',
                unsafe_allow_html=True,
            )
        with tb[2]:
            commune_label = st.selectbox(
                "Territoire", range(len(commune_options)),
                format_func=lambda i: commune_options[i],
                index=st.session_state["commune_idx"],
                key="commune_topbar",
                help="Restreint toutes les pages aux arrêts situés dans la commune.",
            )
        st.session_state["commune_idx"] = int(commune_label)
        selected_commune_label = commune_options[st.session_state["commune_idx"]]
        commune = None if selected_commune_label.startswith("Réseau complet") else selected_commune_label

        with tb[4]:
            period = period_picker(conn, cutoff)
        since_ts, end_ts = _ts(period.start), _ts(period.end)

        scheduled, skipped = load_network_data(conn, cutoff, since_ts, end_ts, commune=commune)
        if scheduled.empty:
            st.warning("Aucun passage exploitable après stabilisation des données.")
            return
        ranking = make_ranking(scheduled, skipped)

        visible_ranking = ranking[ranking["observations"] >= MIN_OBSERVATIONS].copy()
        if visible_ranking.empty:
            visible_ranking = ranking.copy()

        total = int(ranking["observations"].sum())
        on_time = float((ranking["observations"] * ranking["pct_a_l_heure"]).sum() / max(total, 1))
        retard_moyen_network = float((ranking["observations"] * ranking["retard_moyen_s"]).sum() / max(total, 1))
        skipped_total = int(ranking["skipped"].sum())
        eligible_total = int(ranking["eligible"].sum())
        skip_rate = skipped_total / max(eligible_total, 1) * 100
        st.markdown('<div class="eyebrow">Observatoire opérationnel · Bordeaux Métropole</div>', unsafe_allow_html=True)

        if commune is not None:
            st.title(f"La fiabilité de {commune}, en un coup d’œil.")
            st.markdown('<div class="hero-subtitle">Les arrêts situés sur la commune, agrégés par ligne desservie et par mode.</div>', unsafe_allow_html=True)
        else:
            st.title("La fiabilité du réseau, en un coup d’œil.")
            st.markdown('<div class="hero-subtitle">Des indicateurs lisibles pour identifier les lignes, les modes et les créneaux qui demandent une attention.</div>', unsafe_allow_html=True)
        ponct_label = "Ponctualité" if commune is not None else "Ponctualité réseau"
        kpi_ponct = palette_kpi_tier({"ponctualite": on_time}, "ponctualite")
        kpi_retard = palette_kpi_tier({"retard": retard_moyen_network}, "retard")
        kpi_skip = palette_kpi_tier({"skip_rate": skip_rate}, "skip_rate")
        render_kpis([
            ("Passages analysés", f"{total:,}".replace(",", " "), None, "neutral"),
            (ponct_label, f"{on_time:.1f} %", "≤ 5 min de retard", kpi_ponct),
            ("Retard moyen", format_seconds(retard_moyen_network, signed=True), None, kpi_retard),
            ("Lignes suivies", f"{len(ranking)}", None, "neutral"),
            ("Arrêts sautés", f"{skip_rate:.2f} %", f"{skipped_total:,} / {eligible_total:,} attendus".replace(",", " "), kpi_skip),
        ])
        st.caption("**Passage analysé** : un départ programmé (SCHEDULED) avec retard connu, sorti du flux depuis ≥ 20 min. **Observation** : une ligne brute du flux GTFS-RT (sert à mesurer le volume de collecte). **Arrêts sautés** : arrêts annoncés SKIPPED, rapportés aux arrêts attendus (SCHEDULED + SKIPPED).")

        disturbed = load_disturbed_route_ids(conn, since_ts, end_ts, cutoff, commune=commune)
        ranking_net = ranking if commune is None else make_ranking(
            *load_network_data(conn, cutoff, since_ts, end_ts, commune=None))
        ctx = PageContext(conn, cutoff, since_ts, end_ts, commune, ranking, visible_ranking,
                          ranking_net, disturbed, total, period, _ts(period.prev_start), _ts(period.prev_end))
        PAGES[page](ctx)
    finally:
        conn.close()


if __name__ == "__main__":
    main()