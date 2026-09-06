"""Tableau de bord de fiabilite des passages TBM.

Charte graphique « Urban Vision » (fond clair) : Black Forest #283618 (marque,
titres), Olive Leaf #606c38 (texte secondaire), Sunlit Clay #DDA15E (bordures),
Cornsilk #FEFAE0 (fond de page), Copperwood #bc6c25 → Teal #2a6f6f (dégradé de
score mauvais → bon). Les observations les plus recentes
restent dans le flux GTFS-RT : elles sont ecartees afin de ne mesurer que des
passages pour lesquels le retard est stabilise.

Architecture de chargement : les aggregations lourdes sont faites en SQL (seuls
quelques resultats agregees transitent en pandas, pas les 1,3 M de lignes brutes),
les onglets sont rendus paresseusement (seul l'onglet actif calcule ses
graphiques) et les loaders sont mis en cache 60 secondes.
"""

import base64
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

import pydeck as pdk

from matplotlib.colors import LinearSegmentedColormap

from highcharts import (
    render as hc_render,
    ranking_chart,
    scatter_chart,
    network_daily_chart,
    network_hourly_chart,
    mode_comparison_chart,
    mode_daily_chart,
    mode_hourly_chart,
    timeline_chart,
    hourly_risk_chart,
    delay_distribution_chart,
    collection_minutely_chart,
    hourly_distribution_chart,
)

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vigie_tbm.db"
FRESHNESS_BUFFER_SECONDS = 20 * 60
CACHE_TTL_SECONDS = 60
MIN_OBSERVATIONS = 50

BLACK_FOREST = "#283618"
COPPERWOOD = "#bc6c25"
TEAL = "#2a6f6f"
CORNSILK = "#FEFAE0"
WHITE = "#FFFFFF"
SUNLIT_CLAY = "#DDA15E"
OLIVE_LEAF = "#606c38"

SUNLIT_CLAY_40 = "rgba(221, 161, 94, 0.40)"
SUNLIT_CLAY_30 = "rgba(221, 161, 94, 0.30)"
OLIVE_LEAF_70 = "rgba(96, 108, 56, 0.70)"

CAUTION_TEXT = (
    "Cette ligne fait l'objet d'une perturbation signalée par TBM sur tout ou "
    "partie de la période — sans lien de causalité établi arrêt par arrêt avec "
    "les statistiques présentées."
)

# Dégradé continu Copperwood → Teal (0 = mauvais = #bc6c25, 100 = bon = #2a6f6f).
# Utilisé pour colorer les tableaux et afficher les légendes « barre de dégradé ».
_copperwood_rgb = (0xBC, 0x6C, 0x25)
_teal_rgb = (0x2A, 0x6F, 0x6F)
SCORE_CMAP = LinearSegmentedColormap.from_list(
    "score_urban_vision",
    [tuple(c / 255 for c in _copperwood_rgb),
     tuple(c / 255 for c in (0x73, 0x6E, 0x4A)),
     tuple(c / 255 for c in _teal_rgb)],
)


def _color_from_gradient(value: float) -> str:
    """Hex d'une valeur 0-100 sur le dégradé Copperwood→Teal."""
    from highcharts import score_gradient_hex
    return score_gradient_hex(value, higher_is_better=True)


def render_gradient_legend(title: str = "", left_label: str = "à surveiller",
                           right_label: str = "bon", invert: bool = False) -> None:
    """Petite légende « barre de dégradé » graduée 0/50/100.

    La barre va de la valeur 0 (gauche) à la valeur 100 (droite) sur l'axe.
    `left_label`/`right_label` sont affichés aux deux extrémités dans cet ordre.
    `invert=True` pour les métriques où « plus = pire » (retards, arrêts sautés,
    dérive à l'horaire) : valeur 0 = bon = Teal à gauche, valeur 100 = mauvais =
    Copperwood à droite. Sinon (score, ponctualité : plus = mieux) : 0 = Copperwood,
    100 = Teal.
    """
    if invert:
        ramp = "linear-gradient(90deg, #2a6f6f 0%, #2a6f6f 25%, #736E4A 50%, #bc6c25 75%, #bc6c25 100%)"
    else:
        ramp = "linear-gradient(90deg, #bc6c25 0%, #bc6c25 25%, #736E4A 50%, #2a6f6f 75%, #2a6f6f 100%)"
    st.markdown(
        f"""
        <div style="margin:.15rem 0 .6rem;font-size:.78rem;color:{OLIVE_LEAF_70}">
          {('<b>'+title+'</b>&nbsp; ' if title else '')}<span>{left_label}</span>
          <span style="display:inline-block;vertical-align:middle;width:9rem;height:.55rem;border-radius:4px;
            background:{ramp};margin:0 .4rem;"></span>
          <span>{right_label}</span>
          <span style="color:{OLIVE_LEAF_70};margin-left:.4rem">0 — 50 — 100</span>
        </div>
        """,
        unsafe_allow_html=True,
    )





MODE_LABELS = {0: "Tramway", 3: "Bus", 4: "Ferry", 2: "Rail", 5: "Câble", 7: "Funiculaire", 11: "Trolleybus"}
# Chaque mode a une couleur propre et distincte des couleurs Urban Vision
# (jamais vert/magenta qui véhiculeraient une polarité bien/mal). Découverte
# dynamique via load_mode_stats (groupée sur route_type réellement présent).
# Teal tramway · Copperwood bus · Olive Leaf ferry.
MODE_COLORS = {0: "#2a6f6f", 3: "#bc6c25", 4: "#606c38"}
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

# Présélections du time picker, en JOURS de service (les agrégats sont journaliers).
PRESET_RANGES = [
    ("1 jour", 1),
    ("7 jours", 7),
    ("30 jours", 30),
    ("90 jours", 90),
    ("Tout l'historique", None),
]
DEFAULT_PRESET = "7 jours"

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


def _median_from_hists(hists) -> float | None:
    """Médiane exacte (à la seconde) depuis les histogrammes JSON par jour/ligne.

    Comporte comme pandas.median() : pour un effectif pair, moyenne des deux
    valeurs centrales.
    """
    total = 0
    counts: Counter = Counter()
    for h in hists:
        if not h:
            continue
        for k, v in h.items():
            counts[int(k)] += v
            total += v
    if total == 0:
        return None
    if total % 2 == 1:
        target = (total + 1) // 2
        cum = 0
        for sec in sorted(counts):
            cum += counts[sec]
            if cum >= target:
                return sec
    else:
        lower, upper = total // 2, total // 2 + 1
        cum, found = 0, []
        for sec in sorted(counts):
            cum += counts[sec]
            if len(found) == 0 and cum >= lower:
                found.append(sec)
            if cum >= upper:
                found.append(sec)
                break
        return (found[0] + found[1]) / 2.0


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


def format_seconds(value: float | int | None, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = int(round(float(value)))
    sign = "+" if signed and value > 0 else "−" if value < 0 else ""
    absolute = abs(value)
    minutes, seconds = divmod(absolute, 60)
    return f"{sign}{minutes} min {seconds:02d} s" if minutes else f"{sign}{seconds} s"


def format_date(ts: int | None) -> str:
    if not ts:
        return "inconnue"
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y à %H:%M")


def _day_midnight(d: datetime.date) -> datetime:
    return datetime.combine(d, dtime(0, 0))


def time_range_picker(cutoff_ts: int) -> tuple[int | None, int | None, str]:
    """Sélecteur de période dans l'esprit du time picker Grafana (popover).

    Bouton dans la barre supérieure affichant la plage active. Le popover
    propose des plages rapides (« Aujourd'hui », « 7 derniers jours », …) et des
    plages personnalisées, relatives (quantité + unité) ou absolues (Du → Au).
    La période est exprimée en journées de service complètes. `end_ts` est la
    borne exclusive (minuit du jour suivant le dernier jour inclus).
    """
    today = datetime.fromtimestamp(cutoff_ts).date()

    # Bouton d'ouverture : affiche la plage active courante.
    # Première visite : applique réellement la présélection par défaut ("7 jours"),
    # sinon range_since/range_end resteraient None et la période serait illimitée.
    if "range_key" not in st.session_state:
        st.session_state["range_key"] = DEFAULT_PRESET
        st.session_state["range_label"] = DEFAULT_PRESET
        default_days = dict(PRESET_RANGES)[DEFAULT_PRESET]
        st.session_state["range_since"] = int(
            (_day_midnight(today) - timedelta(days=default_days - 1)).timestamp())
        st.session_state["range_end"] = int(
            (_day_midnight(today) + timedelta(days=1)).timestamp())
    with st.popover(
        f"🗓 Période : {st.session_state['range_label']}",
        use_container_width=False,
    ):
        mode = st.radio("Type de plage", ("Plage relative", "Plage absolue"),
                        horizontal=True, label_visibility="collapsed")
        st.markdown("**Plages rapides**")
        presets = st.columns(5)
        preset_map = dict(PRESET_RANGES)
        for col, label in zip(presets, [l for l, _ in PRESET_RANGES]):
            if col.button(label, key=f"preset_{label}", use_container_width=True):
                days = preset_map[label]
                if days is None:
                    st.session_state["range_key"] = label
                    st.session_state["range_label"] = label
                    st.session_state["range_since"] = None
                    st.session_state["range_end"] = None
                else:
                    start = _day_midnight(today) - timedelta(days=days - 1)
                    st.session_state["range_key"] = label
                    st.session_state["range_label"] = label
                    st.session_state["range_since"] = int(start.timestamp())
                    st.session_state["range_end"] = int(
                        (_day_midnight(today) + timedelta(days=1)).timestamp())
                st.rerun()
        st.markdown("---")
        if mode == "Plage relative":
            st.markdown("**Plage personnalisée (relative)**")
            rc1, rc2, rc3 = st.columns([2, 3, 1])
            qty = rc1.number_input("Quantité", min_value=1, max_value=3650, value=7, key="rel_qty")
            unit = rc2.selectbox("Unité", ("Jours", "Semaines", "Mois"), key="rel_unit")
            if rc3.button("Appliquer", key="rel_apply"):
                days = qty * {"Jours": 1, "Semaines": 7, "Mois": 30}[unit]
                start = _day_midnight(today) - timedelta(days=days - 1)
                st.session_state["range_key"] = "custom"
                st.session_state["range_label"] = f"Étendue ({qty} {unit.lower()})"
                st.session_state["range_since"] = int(start.timestamp())
                st.session_state["range_end"] = int(
                    (_day_midnight(today) + timedelta(days=1)).timestamp())
                st.rerun()
        else:
            st.markdown("**Plage personnalisée (absolue)**")
            default_start = today - timedelta(days=6)
            ac1, ac2 = st.columns(2)
            start_date = ac1.date_input("Du", value=default_start, max_value=today, key="abs_start")
            end_date = ac2.date_input("Au", value=today, max_value=today, key="abs_end")
            if start_date > end_date:
                st.error("La date de début doit précéder la fin.")
            if st.button("Appliquer", key="abs_apply"):
                end_day = end_date if start_date <= end_date else start_date
                st.session_state["range_key"] = "custom"
                st.session_state["range_label"] = f"{start_date:%d/%m} → {end_day:%d/%m}"
                st.session_state["range_since"] = int(_day_midnight(start_date).timestamp())
                st.session_state["range_end"] = int(
                    (_day_midnight(end_day) + timedelta(days=1)).timestamp())
                st.rerun()

    return st.session_state.get("range_since"), st.session_state.get("range_end"), st.session_state["range_label"]


def inject_style() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{ background: #FEFAE0; color: #283618; }}
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
    ranking["mode_color"] = ranking["route_type"].map(MODE_COLORS).fillna(OLIVE_LEAF_70)
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
    g["mode_color"] = g["route_type"].map(MODE_COLORS).fillna(OLIVE_LEAF_70)
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
def load_line_timeline(_conn, cutoff_ts: int, since_ts: int | None, route_id: str, end_ts: int | None = None,
                       commune: str | None = None) -> pd.DataFrame:
    """Timeline quotidienne d'une ligne, depuis les agrégats."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, route_id=route_id, commune=commune)
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


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_distribution(_conn, cutoff_ts: int, since_ts: int | None, route_id: str | None = None,
                      end_ts: int | None = None, commune: str | None = None) -> pd.DataFrame:
    """Distribution du retard par classes, depuis les histogrammes journaliers."""
    core = _load_daily_core(_conn, cutoff_ts, since_ts, end_ts, route_id=route_id, commune=commune)
    if core.empty:
        return pd.DataFrame(columns=["observations", "plage"])
    bucket_counts = Counter()
    for h in core["hist"]:
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
    counts = pd.Series(bucket_counts).reindex(DIST_BUCKETS, fill_value=0).reset_index()
    counts.columns = ["bucket", "observations"]
    return counts.assign(plage=DIST_LABELS).drop(columns="bucket")[["observations", "plage"]]


def _attach_mode(df: pd.DataFrame) -> pd.DataFrame:
    if not df.empty:
        df["mode"] = df["route_type"].map(MODE_LABELS).fillna("Autre")
        df["mode_color"] = df["route_type"].map(MODE_COLORS).fillna(OLIVE_LEAF_70)
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


def _score_gradient(value: float) -> tuple[int, int, int]:
    """Dégradé Copperwood (#bc6c25) → Teal (#2a6f6f), lu de gauche à droite.

    calibrated_r = lerp(188 → 42), calibrated_g = lerp(108 → 111),
    calibrated_b = lerp(37 → 111). Valeur 0 (mauvais) = Copperwood, 100 (bon) = Teal.
    """
    ratio = max(0.0, min(1.0, value / 100.0))
    r = round(188 + (42 - 188) * ratio)
    g = round(108 + (111 - 108) * ratio)
    b = round(37 + (111 - 37) * ratio)
    return (r, g, b)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def load_territorial(_conn, cutoff_ts: int, since_ts: int | None, end_ts: int | None = None,
                     commune: str | None = None) -> pd.DataFrame:
    """Arrêts du périmètre avec leur ligne principale et le score de fiabilité.

    Agriège depuis agg_daily_stop (jamais la table brute) : pour chaque arrêt,
    la ligne la plus fréquentée devient la ligne « principale » affichée.
    """
    since_day, end_day = _day_bounds(since_ts, end_ts, cutoff_ts)
    if commune is None:
        rows = _conn.execute(
            """
            SELECT d.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                   d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne,
                   d.sum_delay, d.cnt_le300, d.cnt_gt300, d.obs
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
                   d.route_id, COALESCE(r.route_short_name, d.route_id) AS ligne,
                   d.sum_delay, d.cnt_le300, d.cnt_gt300, d.obs
            FROM agg_daily_stop d
            JOIN stops s ON s.stop_id = d.stop_id
            JOIN routes r ON r.route_id = d.route_id
            JOIN stop_municipalities sm ON sm.stop_id = d.stop_id
            WHERE sm.commune_name = ? AND d.date_service >= ? AND d.date_service < ?
            """, (commune, since_day, end_day),
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "stop_id", "stop_name", "direction", "lat", "lon", "route_id", "ligne",
            "retard_median_s", "pct_retard_5min", "pct_a_l_heure", "observations",
            "score_fiabilite", "lignes",
        ])
    df = pd.DataFrame(rows, columns=[
        "stop_id", "stop_name", "stop_lat", "stop_lon", "route_id", "ligne",
        "sum_delay", "cnt_le300", "cnt_gt300", "obs",
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
        pct_lt60 = 0.0  # non fourni dans cette vue ; le score passe par la ligne principale
        score = pct_le300  # ponctualité ≤ 5 min ≈ score territorial
        lignes = ", ".join(sorted(set(sub["ligne"].astype(str))))
        out.append({
            "stop_id": top["stop_id"],
            "stop_name": top["stop_name"],
            "direction": directions.get((top["route_id"], top["stop_id"]), ""),
            "lat": float(top["stop_lat"]),
            "lon": float(top["stop_lon"]),
            "route_id": top["route_id"],
            "ligne": top["ligne"],
            "pct_a_l_heure": round(pct_le300, 1),
            "pct_retard_5min": round(pct_gt300, 1),
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
    for route_id, ligne, header, desc, p_start, p_end in rows:
        p_end = p_end if p_end is not None else end_ts_int
        eff_start = max(p_start, start_ts)
        eff_end = min(p_end, end_ts_int)
        if eff_end <= eff_start:
            continue
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


def _territorial_score(df: pd.DataFrame) -> float:
    """Score de fiabilité moyen (pondéré par les passages) du périmètre territorial.

    Utilisé pour la phrase d'accroche de la Vue territoriale (« La fiabilité de la
    commune est de X/100, contre Y/100 pour l'ensemble du réseau »).
    """
    if df is None or df.empty:
        return 0.0
    total = float(df["observations"].sum())
    if total <= 0:
        return round(float(df["score_fiabilite"].mean()), 1)
    return round(float((df["score_fiabilite"] * df["observations"]).sum() / total), 1)


def _territorial_map(df: pd.DataFrame, commune: str | None = None) -> None:
    """Carte géographique interactive (pydeck) des arrêts colorés par score.

    Le dégradé Copperwood (#bc6c25) → Teal (#2a6f6f) suit le score de fiabilité de
    la ligne principale : orange = à surveiller, teal = bon.
    """
    if df.empty:
        st.info("Aucun arrêt exploitable sur ce périmètre pour la période.")
        return
    df = df.copy()
    df["color"] = df["score_fiabilite"].apply(lambda v: list(_score_gradient(v)))
    df["radius"] = df["observations"].clip(50, 400)
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position=["lon", "lat"],
        get_fill_color="color",
        get_radius="radius",
        pickable=True,
        opacity=0.7,
        stroked=True,
        get_line_color=[255, 255, 255],
        line_width_min_pixels=1,
    )
    tooltip = {
        "html": "<b>{stop_name}</b><br/>Direction : {direction}<br/>"
                "Ligne(s) : {lignes}<br/>"
                "Score de fiabilité : {score_fiabilite}/100<br/>"
                "Retards &gt; 5 min : {pct_retard_5min} %<br/>"
                "Passages : {observations}",
        "style": {"backgroundColor": "#FFFFFF", "color": BLACK_FOREST},
    }
    if commune is not None:
        view = pdk.ViewState(longitude=df["lon"].mean(), latitude=df["lat"].mean(),
                             zoom=11.5, min_zoom=9)
    else:
        view = pdk.ViewState(longitude=-0.579, latitude=44.838, zoom=10, min_zoom=8)
    st.pydeck_chart(
        pdk.Deck(layers=[layer], tooltip=tooltip, initial_view_state=view,
                 map_style="light", height=420),
        use_container_width=True,
    )


NAV_ITEMS = [
    "Vue territoriale", "Vue réseau", "Modes de transport", "Analyse d'une ligne",
    "Perturbations", "Collecte des données", "Méthode & données",
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
    if polarity == "good":
        return TEAL
    if polarity == "bad":
        return COPPERWOOD
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


def main() -> None:
    st.set_page_config(page_title="Vigie TBM | Fiabilité", page_icon="◉", layout="wide")
    inject_style()
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
        _cidx = st.session_state["commune_idx"]

        # ---- Barre supérieure persistante : identité (sidebar) + commune + plage.
        tb = st.columns([0.42, 0.02, 1.0, 0.02, 0.8])
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
                help="Restreint tous les onglets aux arrêts situés dans la commune.",
            )
        st.session_state["commune_idx"] = int(commune_label)
        selected_commune_label = commune_options[st.session_state["commune_idx"]]
        commune = None if selected_commune_label.startswith("Réseau complet") else selected_commune_label

        with tb[4]:
            since_ts, end_ts, range_label = time_range_picker(cutoff)

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
        delayed = float((ranking["observations"] * ranking["pct_retard_5min"]).sum() / max(total, 1))
        retard_moyen_network = float((ranking["observations"] * ranking["retard_moyen_s"]).sum() / max(total, 1))
        skipped_total = int(ranking["skipped"].sum())
        eligible_total = int(ranking["eligible"].sum())
        skip_rate = skipped_total / max(eligible_total, 1) * 100
        st.markdown('<div class="eyebrow">Observatoire opérationnel · Bordeaux Métropole</div>', unsafe_allow_html=True)

        if commune is not None:
            st.title(f"La fiabilité de {commune}, en un coup d’œil.")
            st.markdown('<div class="hero-subtitle">Les arrêts situés sur la commune, agréés par ligne desservie et par mode.</div>', unsafe_allow_html=True)
        else:
            st.title("La fiabilité du réseau, en un coup d’œil.")
            st.markdown('<div class="hero-subtitle">Des indicateurs lisibles pour identifier les lignes, les modes et les créneaux qui demandent une attention.</div>', unsafe_allow_html=True)
        ponct_label = "Ponctualité" if commune is not None else "Ponctualité réseau"
        render_kpis([
            ("Passages analysés", f"{total:,}".replace(",", " "), None, "neutral"),
            (ponct_label, f"{on_time:.1f} %", "≤ 5 min de retard", "good" if on_time >= 80 else "bad"),
            ("Retard moyen", format_seconds(retard_moyen_network, signed=True), None, "good" if retard_moyen_network <= 60 else "bad"),
            ("Lignes suivies", f"{len(ranking)}", None, "neutral"),
            ("Arrêts sautés", f"{skip_rate:.2f} %", f"{skipped_total:,} / {eligible_total:,} attendus".replace(",", " "), "good" if skip_rate <= 5 else "bad"),
        ])
        st.caption("**Passage analysé** : un départ programmé (SCHEDULED) avec retard connu, sorti du flux depuis ≥ 20 min. **Observation** : une ligne brute du flux GTFS-RT (sert à mesurer le volume de collecte). **Arrêts sautés** : arrêts annoncés SKIPPED, rapportés aux arrêts attendus (SCHEDULED + SKIPPED).")

        disturbed = load_disturbed_route_ids(conn, since_ts, end_ts, cutoff, commune=commune)

        # navigation pilotée par la sidebar (voir render_sidebar).

        if page == "Vue territoriale":
            st.markdown('<div class="section-note">La carte est le point de départ : '
                        'chaque point est un arrêt, coloré selon le score de fiabilité de la ligne '
                        'principale qui le dessert (dégradé magenta→vert, gradué 0/50/100). '
                        'La taille du point correspond au nombre de passages analysés de l’arrêt '
                        '(de 50 à 400) : plus un arrêt est fréquenté, plus le point est grand. '
                        'Survolez un arrêt pour le détail.</div>', unsafe_allow_html=True)
            territorial = load_territorial(conn, cutoff, since_ts, end_ts, commune=commune)
            territorial_network = load_territorial(conn, cutoff, since_ts, end_ts, commune=None)
            if commune is not None and not territorial.empty:
                st.markdown(
                    f'<div class="insight">La fiabilité de <b>{commune}</b> est de '
                    f'<b>{_territorial_score(territorial)}/100</b>, contre '
                    f'{_territorial_score(territorial_network)}/100 pour l’ensemble du réseau.</div>',
                    unsafe_allow_html=True,
                )
            elif territorial.empty:
                st.info("Aucun arrêt exploitable sur ce périmètre pour la période.")
            render_gradient_legend("Fiabilité par arrêt", "à surveiller", "bon")
            _territorial_map(territorial, commune)
            if not territorial.empty:
                st.markdown("#### Arrêts du périmètre")
                tdisp = territorial[["stop_name", "direction", "ligne", "lignes", "score_fiabilite", "pct_retard_5min", "observations"]].copy()
                tdisp.columns = ["Arrêt", "Direction", "Ligne principale", "Lignes desservies", "Score / 100", "Retards > 5 min", "Passages"]
                tstyled = (
                    tdisp.style
                    .background_gradient(cmap=SCORE_CMAP, subset=["Score / 100"], vmin=0, vmax=100)
                    .format({"Score / 100": "{:.1f}", "Retards > 5 min": "{:.1f} %", "Passages": "{:,}"})
                )
                st.dataframe(tstyled, use_container_width=True, hide_index=True, height=320)
                if commune is not None:
                    st.caption("Carte et tableau restreints aux arrêts de la commune sélectionnée, centrés automatiquement sur son périmètre.")

        if page == "Vue réseau":
            st.markdown("### Priorités de fiabilité")
            st.markdown("<div class=\"section-note\">Le score combine ponctualité (≤ 5 min) et passages signalés comme sautés. Plus il est bas, plus la ligne mérite une attention. Le marqueur ⚠ signale une ligne faisant l'objet d'une perturbation annoncée par TBM sur la période.</div>", unsafe_allow_html=True)
            left, right = st.columns([1.05, .95], gap="large")
            with left:
                st.markdown("#### Score de fiabilité")
                render_gradient_legend(invert=False)
                chart_data = visible_ranking.head(15).sort_values("score_fiabilite").copy()
                chart_data["perturbed"] = chart_data["route_id"].isin(disturbed)
                chart_data["ligne_plot"] = [
                    f"⚠ {l}" if p else l for l, p in zip(chart_data["ligne"], chart_data["perturbed"])
                ]
                hc_render(ranking_chart(chart_data), height=390)
            with right:
                st.markdown("#### Carte de risque (retard médian par mode)")
                hc_render(scatter_chart(visible_ranking), height=390)
            worst = ranking.iloc[0]
            worst_note = ""
            if worst.route_id in disturbed:
                worst_note = " — ⚠ ligne signalée en perturbation sur la période"
            st.markdown(f'<div class="insight">À surveiller en premier : <b>ligne {worst.ligne}</b>{worst_note} — score de fiabilité {worst.score_fiabilite:.1f}/100, avec {worst.pct_retard_5min:.1f} % de passages au-delà de 5 minutes.</div>', unsafe_allow_html=True)

            st.markdown("### Évolution du réseau")
            daily = load_network_daily(conn, cutoff, since_ts, end_ts, commune=commune)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Retards > 5 min par jour")
                render_gradient_legend("Retards", "bon", "à surveiller", invert=True)
                if daily.empty or len(daily) < 2:
                    st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
                else:
                    hc_render(network_daily_chart(daily), height=300)
            with right:
                st.markdown("#### Risque selon l'heure")
                render_gradient_legend("Retards", "bon", "à surveiller", invert=True)
                net_hourly = load_hourly(conn, cutoff, since_ts, end_ts=end_ts, commune=commune)
                if net_hourly.empty:
                    st.info("Cette vue nécessite les heures de départ des observations.")
                else:
                    hc_render(network_hourly_chart(net_hourly), height=300)

            st.markdown("#### Profil des retards du réseau")
            distribution = load_distribution(conn, cutoff, since_ts, end_ts=end_ts, commune=commune)
            if not distribution.empty:
                render_gradient_legend("Écart à l'horaire", "proche de l'horaire", "dérive", invert=True)
                hc_render(delay_distribution_chart(distribution), height=280)

            st.markdown("#### Détail des lignes")
            render_gradient_legend("Score de fiabilité", "à surveiller", "bon")
            display = visible_ranking[["ligne", "mode", "score_fiabilite", "pct_a_l_heure", "retard_moyen_s", "retard_median_s", "pct_retard_5min", "pct_arrets_sautes", "observations"]].copy()
            display["ligne"] = [
                f"⚠ {l}" if rid in disturbed else l
                for l, rid in zip(display["ligne"], visible_ranking["route_id"])
            ]
            display.columns = ["Ligne", "Mode", "Score / 100", "Ponctualité ≤ 5 min", "Retard moyen (s)", "Retard médian (s)", "Retards > 5 min", "Arrêts sautés", "Passages"]
            styled = (
                display.style
                .background_gradient(cmap=SCORE_CMAP, subset=["Score / 100"], vmin=0, vmax=100)
                .format({
                    "Score / 100": "{:.1f}", "Ponctualité ≤ 5 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}",
                    "Retard médian (s)": "{:.0f}", "Retards > 5 min": "{:.1f} %", "Arrêts sautés": "{:.2f} %", "Passages": "{:,}",
                })
            )
            st.dataframe(styled, use_container_width=True, hide_index=True, height=330)
            if disturbed:
                st.caption(f"⚠ {len(disturbed & set(ranking['route_id']))} ligne(s) du périmètre font l'objet d'une perturbation signalée par TBM sur la période — sans lien de causalité établi avec les statistiques présentées.")
            if commune is not None:
                st.info("Données restreintes aux arrêts situés dans la commune sélectionnée.")

        if page == "Modes de transport":
            mode_stats = load_mode_stats(conn, cutoff, since_ts, end_ts, commune=commune)
            if mode_stats.empty:
                st.warning("Aucune donnée exploitable par mode.")
                return
            st.markdown("### Comparaison par mode de transport")
            st.markdown('<div class="section-note">Tramway, bus et ferry n’ont pas les mêmes contraintes : comparer leurs profils permet d’isoler des problèmes structurels. Chaque mode a sa couleur propre (violet tram, orange bus, turquoise ferry) ; sur les cartes, la grande valeur garde le dégradé de ponctualité (magenta→vert).</div>', unsafe_allow_html=True)
            card_html = '<div style="display:flex;gap:1rem;margin-bottom:.2rem;flex-wrap:wrap">'
            for r in mode_stats.itertuples():
                ponct_color = _color_from_gradient(r.pct_a_l_heure)
                card_html += (
                    f'<div style="flex:1 1 0;min-width:220px;background:#ffffff;border:1px solid rgba(221,161,94,.35);'
                    f'border-left:4px solid {r.mode_color};border-radius:10px;padding:.9rem 1rem;box-shadow:0 1px 3px rgba(40,54,24,.08)">'
                    f'<div style="font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;font-weight:600;color:{OLIVE_LEAF_70}">{r.mode}</div>'
                    f'<div style="font-size:1.9rem;font-weight:700;color:{ponct_color};line-height:1.15">{r.pct_a_l_heure:.1f} %</div>'
                    f'<div style="font-size:.82rem;color:{OLIVE_LEAF_70}">{int(r.observations):,} passages · '
                    f'retard médian {format_seconds(r.retard_median_s, signed=True)} · {r.pct_retard_5min:.1f} % &gt; 5 min</div>'
                    f'</div>'
                )
            card_html += '</div>'
            st.markdown(card_html, unsafe_allow_html=True)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Comparaison des indicateurs")
                hc_render(mode_comparison_chart(mode_stats), height=330)
            with right:
                st.markdown("#### Profil horaire par mode")
                st.caption("Séries de comparaison : chaque mode a sa couleur propre (violet tram, orange bus, turquoise ferry), pas de jugement de valeur.")
                mh = load_mode_hourly(conn, cutoff, since_ts, end_ts, commune=commune)
                if mh.empty:
                    st.info("Aucune donnée horaire par mode.")
                else:
                    hc_render(mode_hourly_chart(mh), height=330)
            st.markdown("#### Évolution quotidienne par mode")
            md = load_mode_daily(conn, cutoff, since_ts, end_ts, commune=commune)
            if md.empty or md["date_service"].nunique() < 2:
                st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
            else:
                st.caption("Séries de comparaison : chaque mode a sa couleur propre (violet tram, orange bus, turquoise ferry) ; le trait pointillé distingue le ferry.")
                hc_render(mode_daily_chart(md), height=300)

            table = mode_stats[["mode", "observations", "pct_a_l_heure", "pct_retard_5min", "pct_avance_1min", "retard_moyen_s", "retard_median_s", "pct_arrets_sautes"]].copy()
            table.columns = ["Mode", "Passages", "Ponctualité ≤ 5 min", "Retards > 5 min", "En avance > 1 min", "Retard moyen (s)", "Retard médian (s)", "Arrêts sautés"]
            st.dataframe(table.style.format({
                "Passages": "{:,}", "Ponctualité ≤ 5 min": "{:.1f} %", "Retards > 5 min": "{:.1f} %",
                "En avance > 1 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}", "Retard médian (s)": "{:.0f}", "Arrêts sautés": "{:.2f} %",
            }), use_container_width=True, hide_index=True, height=220)

        if page == "Analyse d'une ligne":
            options = visible_ranking if not visible_ranking.empty else ranking
            route_labels = {
                f"Ligne {r.ligne} · {int(r.observations):,} passages": r.route_id
                for r in options.itertuples()
            }
            default_line = list(route_labels.values())[0] if route_labels else None
            st.selectbox(
                "Ligne analysée", list(route_labels), index=0,
                key="line_selector",
            )
            selected_label = st.session_state.get("line_selector")
            selected_route_id = route_labels[selected_label] if selected_label in route_labels else default_line
            line = ranking[ranking.route_id == selected_route_id].iloc[0]
            marker = " ⚠" if selected_route_id in disturbed else ""
            st.markdown(f"### Ligne {line['ligne']}{marker} · <span style='color:{line['mode_color']}'>{line['mode']}</span>", unsafe_allow_html=True)
            if selected_route_id in disturbed:
                st.warning(CAUTION_TEXT)
            render_kpis([
                ("Score de fiabilité", f"{line.score_fiabilite:.1f} / 100", None, "good" if line.score_fiabilite >= 80 else "bad"),
                ("Retard médian", format_seconds(line.retard_median_s, signed=True), None, "good" if line.retard_median_s <= 60 else "bad"),
                ("Passages > 5 min", f"{line.pct_retard_5min:.1f} %", None, "good" if line.pct_retard_5min <= 40 else "bad"),
                ("En avance > 1 min", f"{line.pct_avance_1min:.1f} %", None, "neutral"),
            ])
            timeline = load_line_timeline(conn, cutoff, since_ts, selected_route_id, end_ts, commune=commune)
            hourly = load_hourly(conn, cutoff, since_ts, selected_route_id, end_ts, commune=commune)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Évolution quotidienne")
                if timeline.empty or len(timeline) < 2:
                    st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
                else:
                    hc_render(timeline_chart(timeline), height=285)
            with right:
                st.markdown("#### Risque selon l'heure")
                render_gradient_legend("Retards", "bon", "à surveiller", invert=True)
                if hourly.empty:
                    st.info("Cette vue nécessite les heures de départ des observations.")
                else:
                    hc_render(hourly_risk_chart(hourly, delayed), height=285)
            st.markdown("#### Profil des retards")
            distribution = load_distribution(conn, cutoff, since_ts, selected_route_id, end_ts, commune=commune)
            if not distribution.empty:
                render_gradient_legend("Écart à l'horaire", "proche de l'horaire", "dérive", invert=True)
                hc_render(delay_distribution_chart(distribution), height=280)

        if page == "Perturbations":
            st.markdown("### Perturbations sur la période")
            st.markdown('<div class="section-note">Perturbations diffusées par TBM dans le flux GTFS-RT Service Alerts. La cause indiquée par TBM (quasi toujours « inconnue ») n’est pas fiable et n’est pas affichée ; le titre, la description, les lignes concernées et la période restent en revanche exploitables.', unsafe_allow_html=True)
            now_ts = int(datetime.now().timestamp())
            alerts_now = load_active_alerts(conn, now_ts)
            if commune is not None and not alerts_now.empty:
                commune_routes = load_commune_routes(conn, commune)
                alerts_now = alerts_now[(alerts_now["route_id"] == "") | (alerts_now["route_id"].isin(commune_routes))]
            history = load_perturbation_history(conn, since_ts, end_ts, cutoff, commune=commune)
            lignes_actives = int(alerts_now["route_id"].nunique()) if not alerts_now.empty else 0
            jours_lignes = int(history["jours_couverts"].sum()) if not history.empty else 0
            render_kpis([
                ("Lignes actuellement perturbées", str(lignes_actives), None, "bad" if lignes_actives > 0 else "good"),
                ("Jours-lignes cumulés perturbés", f"{jours_lignes:,}", None, "neutral"),
                ("Perturbations sur la période", str(len(history)), None, "neutral"),
            ])
            if commune is not None:
                st.caption("Périmètre restreint : lignes desservant des arrêts de la commune sélectionnée.")
            st.markdown("#### Historique des perturbations (période sélectionnée)")
            if history.empty:
                st.info("Aucune perturbation active sur tout ou partie de la période sélectionnée.")
            else:
                st.caption("Période effective tronquée à l'intersection avec la période sélectionnée ; lignes classées par nombre total de jours perturbés décroissant.")
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
                    order = sorted(
                        by_line.items(),
                        key=lambda kv: sum(x.jours_couverts for x in kv[1]),
                        reverse=True,
                    )
                    for ligne, items in order:
                        n = len(items)
                        jours = int(sum(x.jours_couverts for x in items))
                        acc = (
                            f"**Ligne {ligne}** — {n} alerte{'s' if n > 1 else ''} · "
                            f"{jours} jour{'s' if jours > 1 else ''} cumulé{'s' if jours > 1 else ''}"
                        )
                        inner = items if n <= 6 else sorted(
                            items, key=lambda x: x.jours_couverts, reverse=True)[:6]
                        with st.expander(acc):
                            for x in inner:
                                st.markdown(
                                    f"**{x.header_text}**  \n"
                                    f"<span style='color:{OLIVE_LEAF_70}'>{x.debut_effectif} → "
                                    f"{x.fin_effective} · {x.jours_couverts} j</span>",
                                    unsafe_allow_html=True,
                                )
                                st.write(x.description_text or "(description non fournie)")
                                st.markdown("")
                            if len(items) > 6:
                                st.caption(f"+ {len(items) - 6} autre(s) alerte(s) sur cette ligne.")
            st.markdown(f'<div class="section-note">{CAUTION_TEXT}</div>', unsafe_allow_html=True)

        if page == "Collecte des données":
            stats = load_collection_stats(conn)
            st.markdown("### Suivi de la collecte")
            st.markdown('<div class="section-note">Volume et continuité des données collectées via les flux GTFS-RT TripUpdates.</div>', unsafe_allow_html=True)
            st.caption("**Observation (brute)** : toute ligne reçue du flux, quelle que soit sa nature. **Passage analysé** : observation SCHEDULED avec retard connu, hors 20 dernières minutes — c'est la définition utilisée partout dans la Vue réseau. Les arrêts SKIPPED ne comptent pas comme passages analysés mais sont suivis à part.")
            render_kpis([
                ("Observations (brutes)", f"{stats['total']:,}".replace(",", " "), None, "neutral"),
                ("Passages analysés", f"{stats['analysed']:,}".replace(",", " "), "stabilisés, horaires < 5 min", "neutral"),
                ("Trajets distincts", f"{stats['trajets']:,}".replace(",", " "), None, "neutral"),
                ("Première date", format_date(stats["first_ts"]), None, "neutral"),
                ("Dernière date", format_date(stats["last_ts"]), None, "neutral"),
            ])
            st.markdown('<div class="section-note">Les « Observations brutes » comptent toutes les lignes reçues du flux. Les « Passages analysés » reprennent la définition de la Vue réseau : passages programmés (SCHEDULED) avec retard connu, hors 20 dernières minutes. Les arrêts sautés (SKIPPED) ne sont pas comptés comme passages mais restent suivis séparément.</div>', unsafe_allow_html=True)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Observations par minute")
                last_ts = stats["last_ts"]
                if not last_ts:
                    st.info("Aucune donnée disponible.")
                else:
                    ref_ts = int(last_ts)
                    c_end_ts = (ref_ts // 60) * 60
                    c_start_ts = c_end_ts - 7 * 24 * 3600
                    minutely = load_collection_minutely(conn, c_start_ts, c_end_ts)
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

        if page == "Méthode & données":
            st.markdown("### Ce que mesure ce tableau de bord")
            st.markdown("Les données viennent des flux GTFS-RT **TripUpdates** TBM. Une observation est considérée stabilisée après 20 minutes hors du flux temps réel ; cela évite d'interpréter comme final un retard qui peut encore évoluer.")
            c1, c2, c3 = st.columns(3)
            c1.markdown("**Ponctualité**  \n+Un passage est classé ponctuel lorsqu'il ne dépasse pas 5 minutes de retard. Les passages en avance sont conservés pour montrer la distribution réelle.")
            c2.markdown("**Arrêts sautés**  \n+Les événements `SKIPPED` sont suivis à part : ils ne gonflent pas artificiellement le retard moyen, mais pénalisent le score de fiabilité.")
            c3.markdown(f"**Seuil d'échantillon**  \n+Une ligne n'apparaît dans les classements et graphiques que si elle totalise au moins **{MIN_OBSERVATIONS} passages** sur la période. Cela écarte les lignes trop peu observées, dont les chiffres ne seraient pas statistiquement fiables.")
            st.markdown(
                f"<div class='section-note'><b>Pourquoi un tram peut-il avoir des arrêts sautés ?</b> "
                f"Un événement <code>SKIPPED</code> signifie que le véhicule ne dessert pas un arrêt alors "
                f"que le trajet continue. Deux situations courantes l'expliquent :<br/>"
                f"• <b>Prise / rendu de service en cours de ligne</b> : le tram ne dessert pas le terminus ou "
                f"les premiers arrêts (départ ou fin de service plus loin sur la ligne, retour dépôt, rotation). "
                f"Seuls quelques arrêts sont sautés, le reste du trajet circule normalement.<br/>"
                f"• <b>Trajet entièrement annulé</b> : tous les arrêts de la course sont marqués "
                f"<code>SKIPPED</code>. Il s'agit d'une annulation de course, pas d'un saut d'arrêt au sens "
                f"strict : le véhicule ne circule pas du tout.</div>",
                unsafe_allow_html=True,
            )
            st.caption(f"Fenêtre analysée : {total:,} passages programmés stabilisés ; dernier point retenu le {format_date(cutoff)}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()