"""Configurations Highcharts pour le dashboard Urban Vision.

Charte graphique alignée sur les rapports mensuels. Palette et seuils partagés
via reports/palette.py (source unique de vérité) :
- Black Forest (#283618) : marque, titres, éléments neutres (axes, séries sans polarité).
- Olive Leaf (#606c38) : palier positif (bonne performance).
- Sunlit Clay (#DDA15E) : palier moyen (performance intermédiaire).
- Copperwood (#bc6c25) : palier négatif (performance dégradée).
- Blanc : fond des graphiques et de la page.
- Olive Leaf (#606c38) : texte secondaire (opacité 70 %).

Modes de transport : la forme du marqueur (tram ● cercle, bus ■ carré, ferry ▲
triangle) et le style de trait les distinguent ; la couleur reste réservée aux
paliers de performance.
"""

import json
import sys
from pathlib import Path

import streamlit as st
import pandas as pd

# Import du module partagé reports/palette.py (palette et seuils identiques aux
# rapports) : on l'ajoute au sys.path s'il est absent.
_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
from palette import (  # noqa: E402
    BLACK_FOREST,
    CORNSILK,
    COPPERWOOD,
    OLIVE_LEAF,
    RISK_MEDIAN_S,
    RISK_PCT_GT300,
    RISK_ZONE_LABELS,
    SUNLIT_CLAY,
    WHITE,
    hex as palette_hex,
    mode_glyph,
    mode_marker,
    risk_zone,
)

# Variantes dérivées (opacités de la charte) pour les bordures et textes secondaires.
SUNLIT_CLAY_40 = "rgba(221, 161, 94, 0.40)"
SUNLIT_CLAY_30 = "rgba(221, 161, 94, 0.30)"
OLIVE_LEAF_70 = "rgba(96, 108, 56, 0.70)"
BLACK_FOREST_35 = "rgba(40, 54, 24, 0.35)"

MODE_DASH = {0: "Solid", 3: "ShortDash", 4: "Dot"}


def _route_type(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def mode_series_name(route_type, mode: str) -> str:
    return f"{mode_glyph(route_type)} {mode}"


def _glyph_labels(route_type) -> dict:
    return {
        "enabled": True,
        "format": mode_glyph(route_type),
        "style": {"color": BLACK_FOREST, "fontSize": "11px", "textOutline": "none", "fontWeight": "400"},
    }


GLYPH_LEGEND = {"symbolWidth": 0, "symbolHeight": 0, "symbolPadding": 0, "squareSymbol": False}


def _mode_line_series(route_type, mode: str, data: list) -> dict:
    return {
        "name": mode,
        "data": data,
        "color": BLACK_FOREST,
        "dashStyle": MODE_DASH.get(_route_type(route_type), "LongDash"),
        "lineWidth": 2,
        "marker": {"enabled": True, "symbol": mode_marker(route_type), "radius": 4,
                   "lineColor": BLACK_FOREST, "lineWidth": 1},
    }

LIGHT_THEME = {
    "colors": [BLACK_FOREST, BLACK_FOREST_35, OLIVE_LEAF, SUNLIT_CLAY, COPPERWOOD],
    "chart": {
        "backgroundColor": "#FFFFFF",
        "style": {"color": OLIVE_LEAF_70, "fontFamily": "Inter, 'Segoe UI', sans-serif"},
        "borderRadius": 8,
        "spacing": [12, 12, 12, 12],
    },
    "title": {"style": {"color": BLACK_FOREST, "fontSize": "14px", "fontWeight": "700"}},
    "xAxis": {
        "labels": {"style": {"color": OLIVE_LEAF_70, "fontSize": "11px"}},
        "lineColor": SUNLIT_CLAY_40,
        "tickColor": SUNLIT_CLAY_40,
        "gridLineColor": SUNLIT_CLAY_30,
        "title": {"style": {"color": OLIVE_LEAF_70, "fontSize": "12px"}},
    },
    "yAxis": {
        "labels": {"style": {"color": OLIVE_LEAF_70, "fontSize": "11px"}},
        "lineColor": SUNLIT_CLAY_40,
        "tickColor": SUNLIT_CLAY_40,
        "gridLineColor": SUNLIT_CLAY_30,
        "title": {"style": {"color": OLIVE_LEAF_70, "fontSize": "12px"}},
    },
    "legend": {
        "itemStyle": {"color": OLIVE_LEAF_70, "fontSize": "11px"},
        "itemHoverStyle": {"color": BLACK_FOREST},
    },
    "tooltip": {
        "backgroundColor": "#FFFFFF",
        "borderColor": SUNLIT_CLAY_40,
        "style": {"color": BLACK_FOREST, "fontSize": "12px"},
        "shadow": True,
    },
    "credits": {"enabled": False},
}


def _accessibility_description(chart_config: dict) -> str:
    ctype = (chart_config.get("chart") or {}).get("type", "")
    base = {
        "bar": "classement", "column": "comparaison en colonnes",
        "line": "série temporelle", "area": "série temporelle",
        "scatter": "nuage de points",
    }.get(ctype, "graphique")
    names = [s.get("name") for s in (chart_config.get("series") or []) if s.get("name")]
    if not names:
        return f"Graphique en {base}"
    return f"Graphique en {base} : {', '.join(str(n) for n in names)}"


def _html(chart_config: dict, height: int, use_stock: bool = False) -> str:
    config = dict(chart_config)
    acc = dict(config.get("accessibility") or {})
    acc["enabled"] = True
    acc.setdefault("description", _accessibility_description(config))
    config["accessibility"] = acc
    chart_id = "hc_" + str(abs(hash(json.dumps(config, sort_keys=True, default=str, ensure_ascii=False))))[:10]
    constructor = "stockChart" if use_stock else "chart"
    scripts = '<script src="https://code.highcharts.com/stock/highstock.js"></script>'
    if not use_stock:
        scripts += '\n<script src="https://code.highcharts.com/highcharts-more.js"></script>'
        scripts += '\n<script src="https://code.highcharts.com/modules/accessibility.js"></script>'
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
{scripts}
</head><body>
<div id="{chart_id}" style="width:100%;height:{height}px;"></div>
<script>
Highcharts.setOptions({json.dumps(LIGHT_THEME, ensure_ascii=False)});
Highcharts.{constructor}('{chart_id}', {json.dumps(config, ensure_ascii=False)});
</script>
</body></html>"""


def render(config: dict, height: int = 300, use_stock: bool = False) -> None:
    st.components.v1.html(_html(config, height, use_stock), height=height + 60)


def ranking_chart(df: pd.DataFrame) -> dict:
    df = df.sort_values("score_fiabilite")
    data = [{"y": round(r["score_fiabilite"], 1), "color": palette_hex(r["score_fiabilite"], "score")}
            for _, r in df.iterrows()]
    labels = df["ligne_plot"].tolist() if "ligne_plot" in df.columns else df["ligne"].tolist()
    return {
        "legend": {"enabled": False},
        "chart": {"type": "bar", "height": 390},
        "title": {"text": None},
        "xAxis": {"categories": labels, "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "max": 100, "min": 0},
        "series": [{"name": "Score", "data": data}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
        "tooltip": {"pointFormat": "<b>{point.y}</b> / 100"},
    }


def commune_ranking_chart(df: pd.DataFrame) -> dict:
    df = df.sort_values("score_fiabilite")
    data = [{"y": round(r["score_fiabilite"], 1), "color": palette_hex(r["score_fiabilite"], "score"),
             "pct": round(r["pct_a_l_heure"], 1), "passages": int(r["observations"])}
            for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "bar", "height": 430},
        "title": {"text": None},
        "xAxis": {"categories": df["commune"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "max": 100, "min": 0},
        "series": [{"name": "Score", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f}</b> / 100<br/>Ponctualité ≤ 5 min : {point.pct:.1f} %<br/>Passages : {point.passages:,.0f}"}}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
    }


def scatter_chart(df: pd.DataFrame) -> dict:
    """Carte de risque : retard médian (x) × retards > 5 min (y).

    Couleur = palier du score de fiabilité, forme = mode. Les deux seuils de
    `palette.risk_zone` sont tracés et chaque point porte sa zone.
    """
    series = []
    work = df.assign(_rt=[_route_type(v) for v in df["route_type"]]) if "route_type" in df.columns \
        else df.assign(_rt=-1)
    for rt, sub in work.groupby("_rt", sort=False):
        data = []
        for _, r in sub.iterrows():
            score = round(float(r["score_fiabilite"]), 1)
            data.append({
                "x": round(r["retard_median_s"], 1),
                "y": round(r["pct_retard_5min"], 1),
                "z": max(r["observations"], 1),
                "name": r["ligne"],
                "score": score,
                "zone": RISK_ZONE_LABELS[risk_zone(r["retard_median_s"], r["pct_retard_5min"])],
                "color": palette_hex(score, "score"),
            })
        series.append({
            "type": "bubble",
            "name": sub["mode"].iloc[0] if "mode" in sub.columns else "Autre",
            "data": data,
            "color": BLACK_FOREST,
            "marker": {"symbol": mode_marker(rt), "lineColor": WHITE, "lineWidth": 1},
            "tooltip": {"headerFormat": "",
                        "pointFormat": "<b>{point.name}</b> ({point.series.name})<br/>{point.zone}<br/>"
                                       "Retard médian : {point.x:.0f} s<br/>&gt; 5 min : {point.y:.1f} %<br/>"
                                       "Score : {point.score}/100<br/>Passages : {point.z:,.0f}"},
        })
    threshold_line = {"color": SUNLIT_CLAY, "dashStyle": "Dash", "width": 1, "zIndex": 3}
    return {
        "chart": {"type": "bubble", "height": 390},
        "title": {"text": None},
        "xAxis": {"title": {"text": "Retard médian (secondes)"},
                  "plotLines": [dict(threshold_line, value=RISK_MEDIAN_S,
                                     label={"text": f"{RISK_MEDIAN_S:.0f} s", "style": {"color": OLIVE_LEAF_70}})]},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0,
                  "plotLines": [dict(threshold_line, value=RISK_PCT_GT300,
                                     label={"text": f"{RISK_PCT_GT300:.0f} %", "align": "right",
                                            "style": {"color": OLIVE_LEAF_70}})]},
        "series": series,
        "plotOptions": {"bubble": {"minSize": 10, "maxSize": 60, "opacity": 0.85}},
        "legend": {"enabled": True, "verticalAlign": "bottom", "align": "center",
                   "bubbleLegend": {"enabled": True, "color": BLACK_FOREST_35, "borderColor": BLACK_FOREST,
                                    "connectorColor": BLACK_FOREST, "ranges": [{}, {}, {}],
                                    "labels": {"format": "{value:,.0f} passages",
                                               "style": {"color": OLIVE_LEAF_70, "fontSize": "10px"}}}},
    }


def network_daily_chart(df: pd.DataFrame) -> dict:
    data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": round(r, 1),
             "passages": int(obs), "color": palette_hex(r, "pourcent")}
            for ts, r, obs in zip(df["date_service"], df["pct_retard_5min"], df["observations"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "line", "height": 300},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "Retards > 5 min", "data": data, "color": BLACK_FOREST, "lineWidth": 2,
                    "marker": {"enabled": True, "radius": 5},
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,.0f}"}}],
        "plotOptions": {"series": {"dataLabels": {"enabled": False}}},
    }


def network_hourly_chart(df: pd.DataFrame) -> dict:
    hm = {int(r["heure"]): r for _, r in df.iterrows()}
    data = []
    for h in range(24):
        if h in hm:
            v = round(hm[h]["pct_retard_5min"], 1)
            data.append({"y": v, "color": palette_hex(v, "pourcent")})
        else:
            data.append({"y": 0, "color": SUNLIT_CLAY})
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": [str(h) for h in range(24)], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "> 5 min", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def mode_comparison_chart(mode_stats: pd.DataFrame) -> dict:
    metrics = [("pct_a_l_heure", "Ponctualité ≤ 5 min", True),
               ("pct_retard_5min", "Retards > 5 min", False),
               ("pct_avance_1min", "En avance > 1 min", False),
               ("pct_arrets_sautes", "Arrêts sautés", False)]
    kinds = {"pct_a_l_heure": "score", "pct_retard_5min": "pourcent",
             "pct_avance_1min": "pourcent", "pct_arrets_sautes": "pourcent"}
    series = []
    for _, r in mode_stats.iterrows():
        data = [{"y": round(r[m], 1), "color": palette_hex(round(r[m], 1), kinds[m])} for m, _, _ in metrics]
        series.append({"name": mode_series_name(r["route_type"], r["mode"]), "color": BLACK_FOREST,
                       "data": data, "dataLabels": _glyph_labels(r["route_type"])})
    return {
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [label for _, label, _ in metrics], "title": {"text": None}},
        "yAxis": {"title": {"text": "%"}, "min": 0, "max": 100},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.1, "pointPadding": 0.05}},
        "legend": GLYPH_LEGEND,
    }


def mode_daily_chart(df: pd.DataFrame) -> dict:
    series = []
    for mode, sub in df.groupby("mode"):
        sub = sub.sort_values("date_service")
        data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": round(v, 1),
                 "color": palette_hex(round(v, 1), "pourcent")}
                for ts, v in zip(sub["date_service"], sub["pct_retard_5min"])]
        series.append(_mode_line_series(sub["route_type"].iloc[0], mode, data))
    return {
        "chart": {"type": "line", "height": 300},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
    }


def mode_hourly_chart(df: pd.DataFrame) -> dict:
    series = []
    for mode, sub in df.groupby("mode"):
        hm = {int(r["heure"]): round(r["pct_retard_5min"], 1) for _, r in sub.iterrows()}
        data = [{"y": hm[h], "color": palette_hex(hm[h], "pourcent")} if h in hm else {"y": None}
                for h in range(24)]
        series.append(_mode_line_series(sub["route_type"].iloc[0], mode, data))
    return {
        "chart": {"type": "line", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [str(h) for h in range(24)], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
    }


def period_punctuality_chart(df: pd.DataFrame) -> dict:
    data = [{"y": round(r["pct_a_l_heure"], 1), "color": palette_hex(r["pct_a_l_heure"], "score"),
             "passages": int(r["observations"]), "moy": float(r["retard_moyen_s"])}
            for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": df["période"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Ponctualité ≤ 5 min (%)"}, "min": 0, "max": 100},
        "series": [{"name": "Ponctualité", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,.0f}<br/>Retard moyen : {point.moy:+.0f} s"}}],
        "plotOptions": {"column": {"borderRadius": 4, "groupPadding": 0.05, "pointPadding": 0.08}},
    }


def period_mode_chart(df: pd.DataFrame) -> dict:
    categories = list(dict.fromkeys(df["période"]))
    series = []
    for mode, sub in df.groupby("mode", sort=False):
        pa = sub.set_index("période").reindex(categories)
        route_type = sub["route_type"].iloc[0]
        data = [{"y": round(float(r["pct_retard_5min"]), 1),
                 "color": palette_hex(round(float(r["pct_retard_5min"]), 1), "pourcent")}
                if pd.notna(r["pct_retard_5min"]) else {"y": None}
                for _, r in pa.iterrows()]
        series.append({"name": mode_series_name(route_type, mode), "data": data, "color": BLACK_FOREST,
                       "dataLabels": _glyph_labels(route_type)})
    return {
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": categories, "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.06, "pointPadding": 0.05}},
        "legend": GLYPH_LEGEND,
    }


def engagement_trend_chart(trend: pd.DataFrame, metric: str) -> dict:
    """Série quotidienne d'une métrique + moyenne glissante sur 7 jours."""
    df = trend.sort_values("date_service").reset_index(drop=True)
    dates_ms = [int(pd.Timestamp(ts).timestamp() * 1000) for ts in df["date_service"]]
    values = df[metric].astype(float)
    raw = [[ms, round(float(v), 1)] for ms, v in zip(dates_ms, values)]
    ma = [round(float(values.iloc[max(0, i - 6):i + 1].mean()), 1) for i in range(len(df))]
    titles = {
        "pct_a_l_heure": "Ponctualité ≤ 5 min (%)",
        "pct_retard_5min": "Retards > 5 min (%)",
        "pct_arrets_sautes": "Arrêts sautés (%)",
        "retard_moyen_s": "Retard moyen (s)",
    }
    y_axis = {"title": {"text": titles[metric]}, "min": 0}
    if metric != "retard_moyen_s":
        y_axis["max"] = 100
    return {
        "chart": {"type": "line", "height": 320},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": y_axis,
        "series": [
            {"name": "Quotidien", "data": raw, "color": BLACK_FOREST, "lineWidth": 2,
             "marker": {"enabled": True, "radius": 3},
             "zIndex": 2},
            {"name": "Moyenne 7 jours", "data": [[ms, v] for ms, v in zip(dates_ms, ma)],
             "color": OLIVE_LEAF, "dashStyle": "ShortDash", "lineWidth": 2,
             "marker": {"enabled": False}},
        ],
        "tooltip": {"shared": True},
    }


def engagement_progression_chart(prog: pd.DataFrame) -> dict:
    """Évolution du score de fiabilité par ligne (moitié récente − moitié précédente)."""
    df = prog.sort_values("delta_score")
    data = [{"y": round(r["delta_score"], 1),
             "color": OLIVE_LEAF if r["delta_score"] >= 2
                      else COPPERWOOD if r["delta_score"] <= -2 else SUNLIT_CLAY,
             "score": round(r["score_fiabilite"], 1),
             "prec": round(r["score_fiabilite_prev"], 1)}
            for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "bar", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": df["ligne"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Évolution du score (/100)"}},
        "series": [{"name": "Δ score", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:+.1f} pts</b><br/>"
                                               "Récent : {point.score} · Précédent : {point.prec}"}}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
    }


def delay_distribution_chart(df: pd.DataFrame) -> dict:
    # Palette par seuil (identique aux rapports) : chaque classe d'écart à
    # l'horaire est rattachée aux seuils « retard » via l'écart absolu médian
    # de la classe (positif ≤ 60 s, moyen ≤ 180 s, négatif au-delà). Aucun
    # dégradé continu : seules les 3 couleurs de palier sont utilisées.
    drift_seconds = {
        "< −10 min": 900, "−10 à −5": 450, "−5 à −2": 210,
        "−2 à −1": 90, "−1 à 0": 30, "0 à +1": 30, "+1 à +2": 90,
        "+2 à +5": 210, "+5 à +10": 450, "+10 à +20": 900, "> +20 min": 1500,
    }
    data = [{"y": int(r["observations"]),
             "color": palette_hex(drift_seconds.get(r["plage"], 1500), "retard")}
            for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": df["plage"].tolist(), "title": {"text": "Écart à l'horaire théorique"}, "labels": {"rotation": -35}},
        "yAxis": {"title": {"text": "Nombre de passages"}},
        "series": [{"name": "Passages", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
    }


def collection_minutely_chart(df: pd.DataFrame) -> dict:
    data = [[int(pd.Timestamp(r["minute"]).timestamp() * 1000), int(r["observations"])] for _, r in df.iterrows()]
    return {
        "chart": {"zoomType": "x"},
        "rangeSelector": {
            "buttons": [
                {"type": "hour", "count": 1, "text": "1h"},
                {"type": "hour", "count": 6, "text": "6h"},
                {"type": "day", "count": 1, "text": "24h"},
                {"type": "day", "count": 7, "text": "1 sem."},
                {"type": "all", "text": "Tout"},
            ],
            "selected": 2,
            "inputEnabled": False,
            "buttonTheme": {"fill": "#FFFFFF", "stroke": SUNLIT_CLAY_40, "style": {"color": OLIVE_LEAF_70, "fontSize": "11px"}},
        },
        "navigator": {
            "enabled": True,
            "series": {"color": BLACK_FOREST, "lineWidth": 1},
            "xAxis": {"labels": {"style": {"color": OLIVE_LEAF_70}}},
        },
        "scrollbar": {
            "enabled": True,
            "barBackgroundColor": "#FFFFFF",
            "barBorderColor": SUNLIT_CLAY_40,
            "buttonBackgroundColor": SUNLIT_CLAY_30,
            "buttonBorderColor": SUNLIT_CLAY_40,
            "trackBackgroundColor": SUNLIT_CLAY_30,
            "trackBorderColor": SUNLIT_CLAY_40,
        },
        "title": {"text": None},
        "series": [{"type": "line", "name": "Observations", "data": data, "color": BLACK_FOREST, "marker": {"enabled": False}}],
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
    }


def hourly_distribution_chart(df: pd.DataFrame) -> dict:
    data = [int(r["observations"]) for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": [str(int(r["heure"])) for _, r in df.iterrows()], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
        "series": [{"name": "Passages", "data": data, "color": BLACK_FOREST}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.05, "pointPadding": 0.05}},
    }



def commune_bands(stop_ids: list, commune_stop_ids: set | None, commune_label: str | None) -> list:
    """Bandes de fond (Cornsilk bordé) sur les suites d'arrêts situés dans la commune sélectionnée."""
    if not commune_stop_ids:
        return []
    bands, start = [], None
    flags = [sid in commune_stop_ids for sid in stop_ids] + [False]
    for i, inside in enumerate(flags):
        if inside and start is None:
            start = i
        elif not inside and start is not None:
            band = {"from": start - 0.5, "to": i - 0.5, "color": CORNSILK, "borderColor": SUNLIT_CLAY_40,
                    "borderWidth": 1, "zIndex": 0}
            if not bands and commune_label:
                band["label"] = {"text": commune_label, "align": "left", "x": 4, "y": 12,
                                 "style": {"color": OLIVE_LEAF_70, "fontSize": "10px", "fontWeight": "600"}}
            bands.append(band)
            start = None
    return bands


def _stop_line(stop_ids: list, stop_id: str | None) -> list:
    if stop_id is None or stop_id not in stop_ids:
        return []
    return [{"value": stop_ids.index(stop_id), "color": BLACK_FOREST, "dashStyle": "Dash", "width": 2,
             "zIndex": 4, "label": {"text": "Cet arrêt", "rotation": 0, "y": 12, "x": 4,
                                    "style": {"color": BLACK_FOREST, "fontWeight": "600", "fontSize": "11px"}}}]


def _profile_axis(p: pd.DataFrame, highlight_stop_id, commune_stop_ids, commune_label) -> dict:
    ids = p["stop_id"].tolist()
    return {"categories": p["stop_name"].tolist(), "labels": {"rotation": -50, "style": {"fontSize": "10px"}},
            "plotBands": commune_bands(ids, commune_stop_ids, commune_label),
            "plotLines": _stop_line(ids, highlight_stop_id)}


def line_profile_chart(profile: pd.DataFrame, highlight_stop_id: str | None = None,
                       hotspot_stop_ids: set | None = None, commune_stop_ids: set | None = None,
                       commune_label: str | None = None) -> dict:
    """Profil d'une direction : retard pris par tronçon (colonnes) et retard à l'arrêt (courbe).

    Les tronçons où le retard se forme le plus (`hotspot_stop_ids`) ressortent en
    Copperwood ; les arrêts de la commune sélectionnée sont sur fond Cornsilk ;
    l'arrêt consulté est marqué d'un trait vertical.
    """
    p = profile.sort_values("order").reset_index(drop=True)
    hot = hotspot_stop_ids or set()
    gains = [{"y": round(float(g), 1), "color": COPPERWOOD if sid in hot else BLACK_FOREST_35,
              "prev": prev if isinstance(prev, str) else "—"}
             for sid, g, prev in zip(p["stop_id"], p["gain_s"], p["prev_stop_name"])]
    delays = [round(float(v), 1) if pd.notna(v) else None for v in p["delay_s"]]
    return {
        "chart": {"height": 360},
        "title": {"text": None},
        "xAxis": _profile_axis(p, highlight_stop_id, commune_stop_ids, commune_label),
        "yAxis": [{"title": {"text": "Retard (secondes)"}}],
        "series": [
            {"type": "column", "name": "Retard pris sur le tronçon", "data": gains, "color": BLACK_FOREST_35,
             "tooltip": {"pointFormat": "Depuis {point.prev} : <b>{point.y:+.0f} s</b><br/>"}},
            {"type": "line", "name": "Retard moyen à l'arrêt", "data": delays, "color": BLACK_FOREST,
             "lineWidth": 2, "marker": {"enabled": False}, "tooltip": {"valueSuffix": " s", "valueDecimals": 0}},
        ],
        "tooltip": {"shared": True},
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
        "legend": {"enabled": True},
    }


def slot_profile_chart(sp: pd.DataFrame, slot_label: str, highlight_stop_id: str | None = None,
                       commune_stop_ids: set | None = None, commune_label: str | None = None) -> dict:
    """Retard moyen arrêt par arrêt sur un créneau (jour × heure) comparé aux autres créneaux."""
    p = sp.sort_values("order").reset_index(drop=True)

    def _values(col):
        return [round(float(v)) if pd.notna(v) else None for v in p[col]]

    return {
        "chart": {"type": "line", "height": 340},
        "title": {"text": None},
        "xAxis": _profile_axis(p, highlight_stop_id, commune_stop_ids, commune_label),
        "yAxis": {"title": {"text": "Retard moyen (secondes)"}},
        "series": [
            {"name": slot_label, "data": _values("slot_delay"), "color": BLACK_FOREST, "lineWidth": 3,
             "marker": {"enabled": True, "radius": 3, "symbol": "circle"}, "connectNulls": True},
            {"name": "D'habitude (autres jours et heures)", "data": _values("usual_delay"),
             "color": BLACK_FOREST_35, "dashStyle": "ShortDash", "lineWidth": 2,
             "marker": {"enabled": False}, "connectNulls": True},
        ],
        "tooltip": {"shared": True, "valueSuffix": " s"},
        "legend": {"enabled": True},
    }


def skip_profile_chart(profile: pd.DataFrame, commune_stop_ids: set | None = None,
                       commune_label: str | None = None) -> dict:
    """Taux d'arrêts sautés arrêt par arrêt le long d'une direction (palier « pourcent »)."""
    p = profile.sort_values("order").reset_index(drop=True)
    rates = (p["skipped"] / p["eligible"].where(p["eligible"] > 0) * 100).fillna(0.0)
    data = [{"y": round(float(r), 1), "color": palette_hex(round(float(r), 1), "pourcent"),
             "skipped": int(k)} for r, k in zip(rates, p["skipped"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": _profile_axis(p, None, commune_stop_ids, commune_label),
        "yAxis": {"title": {"text": "Arrêts sautés (%)"}, "min": 0},
        "series": [{"name": "Arrêts sautés", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b> ({point.skipped} passages non desservis)"}}],
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
    }


def stop_lines_chart(lines: pd.DataFrame) -> dict:
    """Passages > 5 min et arrêts sautés par ligne à un arrêt (nombres absolus)."""
    df = lines.sort_values("cnt_gt300", ascending=False)
    categories = [f"{mode_glyph(rt)} {l}" for rt, l in zip(df["route_type"], df["ligne"])]
    return {
        "chart": {"type": "bar", "height": max(160, 60 + 34 * len(df))},
        "title": {"text": None},
        "xAxis": {"categories": categories, "title": {"text": None}},
        "yAxis": {"title": {"text": "Passages"}, "min": 0, "allowDecimals": False},
        "series": [
            {"name": "Passages à plus de 5 min", "data": [int(v) for v in df["cnt_gt300"]], "color": BLACK_FOREST},
            {"name": "Arrêts sautés", "data": [int(v) for v in df["skipped"]], "color": BLACK_FOREST_35},
        ],
        "plotOptions": {"bar": {"stacking": "normal", "borderRadius": 3, "groupPadding": 0.1}},
        "legend": {"enabled": True},
    }


def risk_by_label_chart(table: pd.DataFrame, label_col: str) -> dict:
    """Part des passages > 5 min par créneau ou par jour (palier « pourcent »)."""
    data = [{"y": round(float(v), 1), "color": palette_hex(round(float(v), 1), "pourcent"),
             "passages": int(o)} for v, o in zip(table["pct_gt300"], table["obs"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 260},
        "title": {"text": None},
        "xAxis": {"categories": table[label_col].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "Retards > 5 min", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,.0f}"}}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.08, "pointPadding": 0.05}},
    }


def daily_status_chart(daily: pd.DataFrame, threshold: float) -> dict:
    """Bande quotidienne : part des passages > 5 min par jour, seuil du jour dégradé tracé."""
    d = (daily.groupby("date_service").agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum"))
         .reset_index())
    d = d[d["obs"] > 0]
    data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": round(float(c) / float(o) * 100, 1),
             "color": palette_hex(round(float(c) / float(o) * 100, 1), "pourcent"), "passages": int(o)}
            for ts, o, c in zip(d["date_service"], d["obs"], d["cnt_gt300"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 240},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0,
                  "plotLines": [{"value": threshold, "color": SUNLIT_CLAY, "dashStyle": "Dash", "width": 1,
                                 "zIndex": 3, "label": {"text": "jour dégradé", "align": "right",
                                                        "style": {"color": OLIVE_LEAF_70}}}]},
        "series": [{"name": "Retards > 5 min", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,.0f}"}}],
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
    }


def cancellations_chart(df: pd.DataFrame) -> dict:
    """Courses supprimées par jour de service."""
    data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": int(c), "trips": int(t)}
            for ts, c, t in zip(df["date_service"], df["cancelled"], df["trips"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 240},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Courses supprimées"}, "min": 0, "allowDecimals": False},
        "series": [{"name": "Courses supprimées", "data": data, "color": BLACK_FOREST,
                    "tooltip": {"pointFormat": "<b>{point.y}</b> sur {point.trips} courses connues"}}],
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
    }
