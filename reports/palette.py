"""Palette Urban Vision et logique de couleur par seuil (partagée).

Les consommateurs sont les scripts de rapports LaTeX (generate_monthly_report.py),
les graphiques matplotlib des rapports et le dashboard (dashboard/app.py,
dashboard/highcharts.py). Les seuils sont centralisés ici pour garantir que la
couleur d'un KPI évaluatif est identique partout.

Palette (5 couleurs) :
- Black Forest  #283618 : marque (titres, Passages analysés, éléments neutres).
- Olive Leaf    #606c38 : positif (bonne performance).
- Sunlit Clay   #DDA15E : moyen (performance intermédiaire).
- Copperwood    #bc6c25 : négatif (performance dégradée).
- Teal          #2A6F6F : couleur d'appoint des graphiques matplotlib des rapports.

Modes de transport : la forme du marqueur dit le mode (tram ● cercle, bus ■
carré, ferry ▲ triangle), la couleur reste celle du palier de score.

Seuils (cohérents avec les légendes des graphiques) :
- "score"    : positif >= 80/100, moyen 50-80, négatif < 50.
- "retard"   : positif <= 60 s, moyen 60-180 s, négatif > 180 s (valeur absolue).
- "pourcent" : positif <= 5 %, moyen 5-15 %, négatif > 15 %.
"""

from __future__ import annotations

# --- Couleurs de la charte ---------------------------------------------------
BLACK_FOREST = "#283618"
OLIVE_LEAF = "#606c38"
SUNLIT_CLAY = "#DDA15E"
COPPERWOOD = "#bc6c25"
CORNSILK = "#FEFAE0"
WHITE = "#FFFFFF"
TEAL = "#2A6F6F"

# --- Paliers -----------------------------------------------------------------
POSITIVE = "positif"
MEDIUM = "moyen"
NEGATIVE = "negatif"

# Nom des paliers >= la couleur.
HEX = {
    POSITIVE: OLIVE_LEAF,
    MEDIUM: SUNLIT_CLAY,
    NEGATIVE: COPPERWOOD,
}

# Mot affiché au lecteur pour chaque palier (dashboard) : du langage courant,
# jamais le nom interne du palier ni celui de sa couleur.
TIER_LABELS = {
    POSITIVE: "fiable",
    MEDIUM: "à surveiller",
    NEGATIVE: "problématique",
}

# Nom LaTeX associé à chaque palier (définis dans le préambule des rapports).
LATEX = {
    POSITIVE: "olive",
    MEDIUM: "sunlitclay",
    NEGATIVE: "alert",
}

_INF = float("inf")

# Bornes des paliers : (borne_inf, borne_sup, palier), premier intervalle gagnant.
SEUILS = {
    "score": [
        (0.0, 50.0, NEGATIVE),
        (50.0, 80.0, MEDIUM),
        (80.0, 101.0, POSITIVE),
    ],
    "retard": [
        (0.0, 60.0, POSITIVE),
        (60.0, 180.0, MEDIUM),
        (180.0, _INF, NEGATIVE),
    ],
    "pourcent": [
        (0.0, 5.0, POSITIVE),
        (5.0, 15.0, MEDIUM),
        (15.0, 101.0, NEGATIVE),
    ],
}

# Association des clés du dict `metrics` des rapports/dashboard à un jeu de
# seuils. Les clés "retard" sont évaluées en valeur absolue.
_KPI_KIND = {
    "fiability": "score",
    "ponctualite": "score",
    "retard": "retard",
    "retard_median": "retard",
    "skip_rate": "pourcent",
    "retard_5min": "pourcent",
}
_ABS_KEYS = {"retard", "retard_median"}


def tier(value: float, kind: str) -> str:
    """Renvoie le palier (positif/moyen/negatif) d'une valeur numérique."""
    for low, high, label in SEUILS[kind]:
        if low <= value < high:
            return label
    return MEDIUM


def hex(value: float, kind: str) -> str:
    """Couleur hexadécimale associée à une valeur et un jeu de seuils."""
    return HEX[tier(value, kind)]


def tier_label(value: float, kind: str = "score") -> str:
    """Mot courant (« fiable », « à surveiller », « problématique ») d'une valeur."""
    return TIER_LABELS[tier(value, kind)]


def latex(value: float, kind: str) -> str:
    """Nom LaTeX associé à une valeur et un jeu de seuils."""
    return LATEX[tier(value, kind)]


def _metric_value(metrics: dict, key: str) -> float:
    value = metrics[key]
    return abs(value) if key in _ABS_KEYS else abs(float(value))


def kpi_tier(metrics: dict, key: str) -> str:
    """Palier d'un KPI évaluatif clé du dict `metrics` (fiabilité, etc.)."""
    return tier(_metric_value(metrics, key), _KPI_KIND[key])


def kpi_hex(metrics: dict, key: str) -> str:
    """Couleur hexadécimale d'un KPI évaluatif (graphiques/dashboard)."""
    return HEX[kpi_tier(metrics, key)]


def kpi_latex(metrics: dict, key: str) -> str:
    """Nom LaTeX d'un KPI évaluatif (préambule des rapports)."""
    return LATEX[kpi_tier(metrics, key)]


MODE_MARKERS = {0: "circle", 3: "square", 4: "triangle"}
DEFAULT_MARKER = "diamond"
MARKER_GLYPHS = {"circle": "●", "square": "■", "triangle": "▲", "diamond": "◆"}


def mode_marker(route_type) -> str:
    """Forme de marqueur d'un mode GTFS (la couleur reste celle du palier de score)."""
    try:
        return MODE_MARKERS.get(int(route_type), DEFAULT_MARKER)
    except (TypeError, ValueError):
        return DEFAULT_MARKER


def mode_glyph(route_type) -> str:
    """Glyphe texte de la forme du mode, pour les libellés et légendes."""
    return MARKER_GLYPHS[mode_marker(route_type)]


RISK_MEDIAN_S = 60.0
RISK_PCT_GT300 = 15.0

CRITICAL = "critique"
FREQUENT_SHORT = "frequents_courts"
RARE_LONG = "rares_longs"
LOW = "faible"

RISK_ZONE_LABELS = {
    CRITICAL: "Zone critique",
    FREQUENT_SHORT: "Retards fréquents mais courts",
    RARE_LONG: "Retards rares mais longs",
    LOW: "Risque faible",
}


def risk_zone(median_s: float, pct_gt300: float) -> str:
    """Zone de la carte de risque (retard médian × part des passages > 5 min).

    Seuils : retard médian au-delà du palier positif « retard » (60 s) et part
    des passages > 5 min dans le palier négatif « pourcent » (15 %).
    """
    installed = abs(float(median_s)) >= RISK_MEDIAN_S
    heavy = float(pct_gt300) >= RISK_PCT_GT300
    if installed and heavy:
        return CRITICAL
    if installed:
        return FREQUENT_SHORT
    if heavy:
        return RARE_LONG
    return LOW

TRONCON_SECONDS = (15.0, 40.0)
TRONCON_PCT = (25.0, 50.0)
NO_DATA = "#C4C4BC"
TRONCON_LABELS = {
    POSITIVE: "Les bus n'y perdent pas de temps",
    MEDIUM: "Les bus y perdent un peu de temps",
    NEGATIVE: "Les bus y perdent beaucoup de temps",
}


def troncon_tier(median_s: float, median_pct: float) -> str:
    """Palier d'un tronçon : il faut perdre assez de secondes ET une part suffisante du temps prévu.

    Négatif : au moins 40 s et la moitié du temps prévu ; moyen : au moins 15 s et un quart ;
    sinon positif. Les deux conditions évitent de colorer à tort les tronçons longs (secondes
    seules) ou très courts (pourcentage seul).
    """
    s, pct = float(median_s), float(median_pct)
    if s >= TRONCON_SECONDS[1] and pct >= TRONCON_PCT[1]:
        return NEGATIVE
    if s >= TRONCON_SECONDS[0] and pct >= TRONCON_PCT[0]:
        return MEDIUM
    return POSITIVE


# --- Charte écran « Pierre et vin » (dashboard uniquement) ---------------------

INK = "#2B2523"
INK_MUTED = "#6A5F58"
SURFACE = "#F6F1EA"
LINE = "#E5DCD0"
ACTION = "#351E8F"
GARONNE = "#7FAAA6"
PIERRE = "#E1C17E"
VIN = "#B1437A"
LIE = "#602D24"
SCREEN_NO_DATA = "#D6D6D2"
SCALE = (GARONNE, PIERRE, VIN, LIE)
SCREEN_HEX = {POSITIVE: GARONNE, MEDIUM: PIERRE, NEGATIVE: VIN}

SCORE_ANCHORS = (80.0, 60.0, 50.0, 35.0)
TRONCON_ANCHORS = (0.0, TRONCON_SECONDS[0], TRONCON_SECONDS[1], 60.0)
ANCHORS_REFERENCE = "rentrée 2026 (01/09 au 11/10/2026)"


def screen_hex(value: float, kind: str) -> str:
    """Couleur écran du palier d'une valeur (graphiques à paliers du dashboard)."""
    return SCREEN_HEX[tier(value, kind)]


def retained_seconds(median_s: float, median_pct: float) -> float:
    """Secondes perdues plafonnées par l'équivalent en secondes de la part du temps prévu.

    Les seuils de `troncon_tier` font correspondre 25 % à 15 s et 50 % à 40 s (proportionnel
    entre les deux, et au-delà avec la même pente) : la valeur atteint 15 s exactement quand le
    palier est moyen ou négatif, et 40 s exactement quand il est négatif.
    """
    (p0, p1), (s0, s1) = TRONCON_PCT, TRONCON_SECONDS
    pct = float(median_pct)
    cap = pct * s0 / p0 if pct <= p0 else s0 + (pct - p0) * (s1 - s0) / (p1 - p0)
    return min(float(median_s), cap)


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def scale_color(value: float, anchors: tuple = TRONCON_ANCHORS, colors: tuple = SCALE) -> str:
    """Couleur continue entre quatre repères fixes, interpolée en sRGB.

    `anchors` peut être croissant (plus = pire, secondes perdues) ou décroissant (moins = pire,
    score) ; avant le premier repère la couleur calme, après le dernier la couleur extrême.
    """
    sign = 1.0 if anchors[-1] >= anchors[0] else -1.0
    v, a = sign * float(value), [sign * float(x) for x in anchors]
    if v <= a[0]:
        return colors[0]
    for i in range(1, len(a)):
        if v <= a[i]:
            t = (v - a[i - 1]) / (a[i] - a[i - 1])
            lo, hi = _rgb(colors[i - 1]), _rgb(colors[i])
            return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(lo, hi))
    return colors[-1]


def ink_on(color: str) -> str:
    """Texte lisible sur un fond donné : encre sur les fonds clairs, blanc sur les fonds foncés."""
    def lum(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lum(x) for x in _rgb(color))
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    ink = _rgb(INK)
    y_ink = 0.2126 * lum(ink[0]) + 0.7152 * lum(ink[1]) + 0.0722 * lum(ink[2])
    return INK if (y + 0.05) / (y_ink + 0.05) >= 1.05 / (y + 0.05) else "#FFFFFF"


def score_color(score: float) -> str:
    """Couleur écran d'un score de fiabilité sur 100 (repères `SCORE_ANCHORS`)."""
    return scale_color(score, SCORE_ANCHORS)


def troncon_color(median_s: float, median_pct: float) -> str:
    """Couleur écran d'un tronçon mesuré (repères `TRONCON_ANCHORS` en secondes retenues)."""
    return scale_color(retained_seconds(median_s, median_pct), TRONCON_ANCHORS)
