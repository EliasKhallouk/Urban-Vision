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
- Teal          #2A6F6F : couleur mode transport (bus).

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