"""Configurations Highcharts pour le dashboard Urban Vision.

Charte graphique alignée sur les rapports mensuels. Palette et seuils partagés
via reports/palette.py (source unique de vérité) :
- Black Forest (#283618) : marque, titres, éléments neutres (axes, séries sans polarité).
- Olive Leaf (#606c38) : palier positif (bonne performance).
- Sunlit Clay (#DDA15E) : palier moyen (performance intermédiaire).
- Copperwood (#bc6c25) : palier négatif (performance dégradée).
- Cornsilk (#FEFAE0) : fond de page.
- Olive Leaf (#606c38) : texte secondaire (opacité 70 %).
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
from palette import BLACK_FOREST, COPPERWOOD, OLIVE_LEAF, SUNLIT_CLAY, CORNSILK, WHITE, TEAL, hex as palette_hex  # noqa: E402

# Variantes dérivées (opacités de la charte) pour les bordures et textes secondaires.
SUNLIT_CLAY_40 = "rgba(221, 161, 94, 0.40)"
SUNLIT_CLAY_30 = "rgba(221, 161, 94, 0.30)"
OLIVE_LEAF_70 = "rgba(96, 108, 56, 0.70)"

# Chaque mode a une couleur propre et fixe. Le ferry est pointillé (MODE_DASH)
# pour se distinguer même en cas de couleurs proches.
# Teal bus (#2A6F6F) · Copperwood tram (#bc6c25) · Black Forest ferry (#283618).
MODE_COLORS = {0: COPPERWOOD, 3: TEAL, 4: BLACK_FOREST}
MODE_DASH = {0: "Solid", 3: "Solid", 4: "Dot"}

LIGHT_THEME = {
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
        "chart": {"type": "bar", "height": 430},
        "title": {"text": None},
        "xAxis": {"categories": df["commune"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "max": 100, "min": 0},
        "series": [{"name": "Score", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f}</b> / 100<br/>Ponctualité ≤ 5 min : {point.pct:.1f} %<br/>Passages : {point.passages:,}"}}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
    }


def scatter_chart(df: pd.DataFrame) -> dict:
    """Carte de risque : retard médian (x) vs retards > 5 min (y), coloré par moyen de transport."""
    series_data: dict[str, dict] = {}
    for _, r in df.iterrows():
        mode = r.get("mode", "Autre")
        if mode not in series_data:
            series_data[mode] = {"color": r.get("mode_color", BLACK_FOREST), "data": []}
        series_data[mode]["data"].append({
            "x": round(r["retard_median_s"], 1),
            "y": round(r["pct_retard_5min"], 1),
            "z": max(r["observations"], 1),
            "name": r["ligne"],
            "score": round(r["score_fiabilite"], 1),
        })
    series = [{"type": "bubble", "name": name, "data": d["data"], "color": d["color"],
               "tooltip": {"pointFormat": "<b>{point.name}</b> ({point.series.name})<br/>Retard médian : {point.x:.0f} s<br/>&gt; 5 min : {point.y:.1f} %<br/>Score : {point.score}/100<br/>Passages : {point.z:,}"}}
              for name, d in series_data.items() if d["data"]]
    return {
        "chart": {"type": "bubble", "height": 390},
        "title": {"text": None},
        "xAxis": {"title": {"text": "Retard médian (secondes)"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}},
        "series": series,
        "plotOptions": {"bubble": {"minSize": 10, "maxSize": 60, "opacity": 0.8}},
        "legend": {"enabled": True, "verticalAlign": "bottom", "align": "center"},
    }


def network_daily_chart(df: pd.DataFrame) -> dict:
    data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": round(r, 1),
             "passages": int(obs), "color": palette_hex(r, "pourcent")}
            for ts, r, obs in zip(df["date_service"], df["pct_retard_5min"], df["observations"])]
    return {
        "chart": {"type": "line", "height": 300},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "Retards > 5 min", "data": data, "color": BLACK_FOREST, "lineWidth": 2,
                    "marker": {"enabled": True, "radius": 5},
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,}"}}],
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
    series = []
    for _, r in mode_stats.iterrows():
        color = r.get("mode_color", MODE_COLORS.get(int(r["route_type"]), BLACK_FOREST))
        data = [round(r[m], 1) for m, _, _ in metrics]
        series.append({"name": r["mode"], "color": color, "data": data})
    return {
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [label for _, label, _ in metrics], "title": {"text": None}},
        "yAxis": {"title": {"text": "%"}, "min": 0, "max": 100},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.1, "pointPadding": 0.05}},
    }


def mode_daily_chart(df: pd.DataFrame) -> dict:
    series = []
    for mode, sub in df.groupby("mode"):
        sub = sub.sort_values("date_service")
        data = [[int(pd.Timestamp(ts).timestamp() * 1000), round(v, 1)] for ts, v in zip(sub["date_service"], sub["pct_retard_5min"])]
        dash = MODE_DASH.get(sub["route_type"].iloc[0], "Solid")
        series.append({"name": mode, "data": data, "color": sub["mode_color"].iloc[0], "dashStyle": dash, "lineWidth": 2,
                       "marker": {"enabled": False, "states": {"hover": {"enabled": True}}}})
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
        data = [{"y": hm.get(h, 0), "color": sub["mode_color"].iloc[0] if h in hm else SUNLIT_CLAY} for h in range(24)]
        dash = MODE_DASH.get(sub["route_type"].iloc[0], "Solid")
        series.append({"name": mode, "data": data, "color": sub["mode_color"].iloc[0], "dashStyle": dash})
    return {
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [str(h) for h in range(24)], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.1, "pointPadding": 0.05}},
    }


def period_punctuality_chart(df: pd.DataFrame) -> dict:
    data = [{"y": round(r["pct_a_l_heure"], 1), "color": palette_hex(r["pct_a_l_heure"], "score"),
             "passages": int(r["observations"]), "moy": float(r["retard_moyen_s"])}
            for _, r in df.iterrows()]
    return {
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": df["période"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Ponctualité ≤ 5 min (%)"}, "min": 0, "max": 100},
        "series": [{"name": "Ponctualité", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:.1f} %</b><br/>Passages : {point.passages:,}<br/>Retard moyen : {point.moy:+.0f} s"}}],
        "plotOptions": {"column": {"borderRadius": 4, "groupPadding": 0.05, "pointPadding": 0.08}},
    }


def period_mode_chart(df: pd.DataFrame) -> dict:
    categories = list(dict.fromkeys(df["période"]))
    series = []
    for mode, sub in df.groupby("mode", sort=False):
        pa = sub.set_index("période").reindex(categories)
        data = [round(float(r["pct_retard_5min"]), 1) if pd.notna(r["pct_retard_5min"]) else 0
                for _, r in pa.iterrows()]
        series.append({"name": mode, "data": data, "color": pa["mode_color"].iloc[0]})
    return {
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": categories, "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.06, "pointPadding": 0.05}},
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
        "chart": {"type": "bar", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": df["ligne"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Évolution du score (/100)"}},
        "series": [{"name": "Δ score", "data": data,
                    "tooltip": {"pointFormat": "<b>{point.y:+.1f} pts</b><br/>"
                                               "Récent : {point.score} · Précédent : {point.prec}"}}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
    }


def timeline_chart(df: pd.DataFrame) -> dict:
    data = [[int(pd.Timestamp(ts).timestamp() * 1000), round(r, 1)] for ts, r in zip(df["date_service"], df["pct_retard_5min"])]
    return {
        "chart": {"type": "area", "height": 285},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"data": data, "fillOpacity": 0.12, "lineWidth": 2, "color": BLACK_FOREST, "marker": {"enabled": False, "states": {"hover": {"enabled": True}}}}],
    }


def hourly_risk_chart(df: pd.DataFrame, threshold: float | None = None) -> dict:
    cat = [str(h) for h in range(24)]
    hm = {int(r["heure"]): r for _, r in df.iterrows()}
    data = []
    for h in range(24):
        if h in hm:
            v = round(hm[h]["pct_retard_5min"], 1)
            data.append({"y": v, "color": palette_hex(v, "pourcent")})
        else:
            data.append({"y": 0, "color": SUNLIT_CLAY})
    return {
        "chart": {"type": "column", "height": 285},
        "title": {"text": None},
        "xAxis": {"categories": cat, "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "> 5 min", "data": data}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0, "pointPadding": 0.05}},
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
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": [str(int(r["heure"])) for _, r in df.iterrows()], "title": {"text": "Heure locale"}},
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
        "series": [{"name": "Passages", "data": data, "color": BLACK_FOREST}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.05, "pointPadding": 0.05}},
    }
