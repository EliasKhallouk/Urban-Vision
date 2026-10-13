"""Configurations Highcharts pour le dashboard Urban Vision.

Charte écran « Pierre et vin », jetons et seuils de reports/palette.py (source unique) :
- encre : titres, éléments neutres (axes, séries sans polarité, évolutions) ;
- texte secondaire et trait : libellés d'axes, grilles, bordures ;
- scores : dégradé continu `score_color` (repères fixes) ;
- autres mesures à paliers : `screen_hex` (Garonne, pierre, vin) ;
- blanc : fond des graphiques et de la page.

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
    ACTION,
    INK,
    INK_MUTED,
    LINE,
    SURFACE,
    VIN,
    RISK_MEDIAN_S,
    RISK_PCT_GT300,
    RISK_ZONE_LABELS,
    CRITICAL,
    FREQUENT_SHORT,
    LOW,
    RARE_LONG,
    WHITE,
    GARONNE,
    PIERRE,
    SCREEN_NO_DATA,
    score_color,
    screen_hex,
    mode_glyph,
    mode_marker,
    risk_zone,
)

FONT_MICRO, FONT_LABEL, FONT_SMALL, FONT_BLOCK = "12px", "13px", "14px", "17px"
INK_35 = "rgba(43, 37, 35, 0.35)"
LINE_SOFT = "rgba(229, 220, 208, 0.75)"


def measure_color(value: float, kind: str) -> str:
    """Couleur d'une valeur : dégradé continu pour un score, paliers écran pour les autres mesures."""
    return score_color(value) if kind == "score" else screen_hex(value, kind)

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
        "style": {"color": INK, "fontSize": FONT_MICRO, "textOutline": "none", "fontWeight": "400"},
    }


GLYPH_LEGEND = {"symbolWidth": 0, "symbolHeight": 0, "symbolPadding": 0, "squareSymbol": False}


def _mode_line_series(route_type, mode: str, data: list) -> dict:
    return {
        "name": mode,
        "data": data,
        "color": INK,
        "dashStyle": MODE_DASH.get(_route_type(route_type), "LongDash"),
        "lineWidth": 2,
        "marker": {"enabled": True, "symbol": mode_marker(route_type), "radius": 4,
                   "lineColor": INK, "lineWidth": 1},
    }

LIGHT_THEME = {
    "colors": [INK, INK_35, GARONNE, PIERRE, VIN],
    "chart": {
        "backgroundColor": "#FFFFFF",
        "style": {"color": INK_MUTED, "fontFamily": "'Source Sans', 'Source Sans 3', sans-serif", "fontSize": FONT_LABEL},
        "borderRadius": 8,
        "spacing": [12, 12, 12, 12],
    },
    "title": {"style": {"color": INK, "fontSize": FONT_BLOCK, "fontWeight": "700"}},
    "xAxis": {
        "labels": {"autoRotation": False, "style": {"color": INK_MUTED, "fontSize": FONT_LABEL}},
        "lineColor": LINE,
        "tickColor": LINE,
        "gridLineColor": LINE_SOFT,
        "title": {"style": {"color": INK_MUTED, "fontSize": FONT_LABEL, "fontWeight": "600"}},
    },
    "yAxis": {
        "labels": {"style": {"color": INK_MUTED, "fontSize": FONT_LABEL}},
        "lineColor": LINE,
        "tickColor": LINE,
        "gridLineColor": LINE_SOFT,
        "title": {"style": {"color": INK_MUTED, "fontSize": FONT_LABEL, "fontWeight": "600"}},
    },
    "legend": {
        "itemStyle": {"color": INK, "fontSize": FONT_LABEL, "fontWeight": "600"},
        "itemHoverStyle": {"color": INK},
    },
    "tooltip": {
        "backgroundColor": "#FFFFFF",
        "borderColor": LINE,
        "style": {"color": INK, "fontSize": FONT_SMALL},
        "shadow": True,
        "xDateFormat": "%A %e %B %Y",
    },
    "lang": {
        "locale": "fr-FR",
        "decimalPoint": ",",
        "thousandsSep": "\u202f",
        "months": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
                   "octobre", "novembre", "décembre"],
        "shortMonths": ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.",
                        "nov.", "déc."],
        "weekdays": ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"],
        "shortWeekdays": ["dim.", "lun.", "mar.", "mer.", "jeu.", "ven.", "sam."],
        "resetZoom": "Réinitialiser le zoom",
        "rangeSelectorZoom": "Zoom",
    },
    "credits": {"enabled": False},
}

HOUR_HEADER = '<span style="font-size:12px">De {point.key} h à {point.key} h 59</span><br/>'
LEXIQUE_FOOTER = (f'<div style="margin-top:6px;padding-top:5px;border-top:1px solid {LINE};'
                  'font-size:13px"><a href="__BASE__?lexique=__TERME__" target="_blank" rel="noopener" '
                  f'style="color:{ACTION};text-decoration-color:{ACTION}">Définitions dans le lexique ↗</a></div>')


def unit_tooltip(suffix: str, decimals: int = 1, header: str | None = None) -> dict:
    """Infobulle d'une série : « Nom de la série : valeur unité »."""
    out = {"pointFormat": f"{{series.name}} : <b>{{point.y:,.{decimals}f}}{suffix}</b><br/>"}
    if header is not None:
        out["headerFormat"] = header
    return out


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


HIGHCHARTS_CDN = "https://cdn.jsdelivr.net/npm/highcharts@13.1.1"
CHART_FONT_CSS = "https://cdn.jsdelivr.net/npm/@fontsource/source-sans-3@5.2.5"


def _html(chart_config: dict, height: int, use_stock: bool = False) -> str:
    config = dict(chart_config)
    term = config.pop("lexique", "")
    acc = dict(config.get("accessibility") or {})
    acc["enabled"] = True
    acc.setdefault("description", _accessibility_description(config))
    config["accessibility"] = acc
    chart_id = "hc_" + str(abs(hash(json.dumps(config, sort_keys=True, default=str, ensure_ascii=False))))[:10]
    constructor = "stockChart" if use_stock else "chart"
    scripts = "".join(f'<link rel="stylesheet" href="{CHART_FONT_CSS}/{w}.css">' for w in (400, 600, 700))
    scripts += f'\n<script src="{HIGHCHARTS_CDN}/highstock.js"></script>'
    if not use_stock:
        scripts += f'\n<script src="{HIGHCHARTS_CDN}/highcharts-more.js"></script>'
        scripts += f'\n<script src="{HIGHCHARTS_CDN}/modules/accessibility.js"></script>'
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<style>body{{margin:0;font-family:'Source Sans 3',sans-serif}}</style>
{scripts}
</head><body>
<div id="{chart_id}" style="width:100%;height:{height}px;"></div>
<script>
const theme = {json.dumps(LIGHT_THEME, ensure_ascii=False)};
const base = (() => {{ try {{ const l = window.parent.location; return l.origin + l.pathname; }} catch (e) {{ return ""; }} }})();
if (base) {{
  theme.tooltip.useHTML = true;
  theme.tooltip.stickOnContact = true;
  theme.tooltip.footerFormat = {json.dumps(LEXIQUE_FOOTER, ensure_ascii=False)}
    .replace("__BASE__", base).replace("__TERME__", {json.dumps(term)});
}}
Highcharts.setOptions(theme);
Highcharts.{constructor}('{chart_id}', {json.dumps(config, ensure_ascii=False)});
</script>
</body></html>"""


def render(config: dict, height: int = 300, use_stock: bool = False) -> None:
    st.components.v1.html(_html(config, height, use_stock), height=height + 60)


def ranking_chart(df: pd.DataFrame) -> dict:
    df = df.sort_values("score_fiabilite")
    data = [{"y": round(r["score_fiabilite"], 1), "color": score_color(r["score_fiabilite"]),
             "id": r.get("route_id")}
            for _, r in df.iterrows()]
    labels = df["ligne_plot"].tolist() if "ligne_plot" in df.columns else df["ligne"].tolist()
    return {
        "legend": {"enabled": False},
        "chart": {"type": "bar", "height": 390},
        "title": {"text": None},
        "xAxis": {"categories": labels, "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "max": 100, "min": 0},
        "series": [{"name": "Score de fiabilité", "data": data, "tooltip": unit_tooltip(" / 100")}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
        "lexique": "score-de-fiabilite",
    }


def commune_ranking_chart(df: pd.DataFrame) -> dict:
    df = df.sort_values("score_fiabilite")
    data = [{"y": round(r["score_fiabilite"], 1), "color": score_color(r["score_fiabilite"]),
             "pct": round(r["pct_a_l_heure"], 1), "passages": int(r["observations"])}
            for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "bar", "height": 430},
        "title": {"text": None},
        "xAxis": {"categories": df["commune"].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "max": 100, "min": 0},
        "series": [{"name": "Score", "data": data,
                    "tooltip": {"pointFormat": "Score de fiabilité : <b>{point.y:.1f} / 100</b><br/>À l'heure : {point.pct:.1f} %<br/>Passages : {point.passages:,.0f}<br/>"}}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
        "lexique": "score-de-fiabilite",
    }


def zone_labels_series(df: pd.DataFrame) -> dict:
    """Libellés des quatre zones de risque, posés aux coins de la carte de risque."""
    x_low = min(0.0, float(df["retard_median_s"].min())) if not df.empty else 0.0
    x_high = max(RISK_MEDIAN_S * 3, float(df["retard_median_s"].max())) if not df.empty else RISK_MEDIAN_S * 3
    y_high = max(RISK_PCT_GT300 * 2, float(df["pct_retard_5min"].max())) if not df.empty else RISK_PCT_GT300 * 2
    corners = [(x_low, y_high, "left", "top", RARE_LONG), (x_high, y_high, "right", "top", CRITICAL),
               (x_high, 0.0, "right", "bottom", FREQUENT_SHORT), (x_low, 0.0, "left", "bottom", LOW)]
    data = [{"x": x, "y": y, "dataLabels": {"align": align, "verticalAlign": valign,
                                             "format": RISK_ZONE_LABELS[zone],
                                             "x": 6 if align == "left" else -6, "y": 0}}
            for x, y, align, valign, zone in corners]
    return {"type": "scatter", "name": "Zones", "data": data, "showInLegend": False,
            "enableMouseTracking": False, "marker": {"enabled": False},
            "dataLabels": {"enabled": True, "crop": False, "overflow": "allow",
                           "backgroundColor": "rgba(255, 255, 255, 0.88)", "borderRadius": 4, "padding": 4,
                           "style": {"color": INK, "fontSize": FONT_LABEL, "fontWeight": "700",
                                     "textOutline": "none"}}, "zIndex": 5}


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
                "color": score_color(score),
            })
        series.append({
            "type": "bubble",
            "name": sub["mode"].iloc[0] if "mode" in sub.columns else "Autre",
            "data": data,
            "color": INK,
            "marker": {"symbol": mode_marker(rt), "lineColor": WHITE, "lineWidth": 1},
            "tooltip": {"headerFormat": "",
                        "pointFormat": "<b>{point.name}</b> ({point.series.name})<br/>{point.zone}<br/>"
                                       "Retard médian : {point.x:.0f} s<br/>&gt; 5 min : {point.y:.1f} %<br/>"
                                       "Score : {point.score:.1f} / 100<br/>Passages : {point.z:,.0f}<br/>"},
        })
    threshold_line = {"color": INK_MUTED, "dashStyle": "Dash", "width": 1, "zIndex": 3}
    series.append(zone_labels_series(df))
    return {
        "lexique": "retard-median",
        "chart": {"type": "bubble", "height": 390},
        "title": {"text": None},
        "xAxis": {"title": {"text": "Retard médian (secondes)"},
                  "plotLines": [dict(threshold_line, value=RISK_MEDIAN_S,
                                     label={"text": f"{RISK_MEDIAN_S:.0f} s", "style": {"color": INK_MUTED}})]},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0,
                  "plotLines": [dict(threshold_line, value=RISK_PCT_GT300,
                                     label={"text": f"{RISK_PCT_GT300:.0f} %", "align": "right",
                                            "style": {"color": INK_MUTED}})]},
        "series": series,
        "plotOptions": {"bubble": {"minSize": 10, "maxSize": 60, "opacity": 0.85}},
        "legend": {"enabled": True, "verticalAlign": "bottom", "align": "center",
                   "bubbleLegend": {"enabled": True, "color": INK_35, "borderColor": INK,
                                    "connectorColor": INK, "ranges": [{}, {}, {}],
                                    "labels": {"format": "{value:,.0f} passages",
                                               "style": {"color": INK_MUTED, "fontSize": FONT_MICRO}}}},
    }


def mode_comparison_chart(mode_stats: pd.DataFrame) -> dict:
    metrics = [("pct_a_l_heure", "À l'heure (−1 à +5 min)", True),
               ("pct_retard_5min", "Retards > 5 min", False),
               ("pct_avance_1min", "En avance > 1 min", False),
               ("pct_arrets_sautes", "Arrêts non desservis", False)]
    kinds = {"pct_a_l_heure": "score", "pct_retard_5min": "pourcent",
             "pct_avance_1min": "pourcent", "pct_arrets_sautes": "pourcent"}
    series = []
    for _, r in mode_stats.iterrows():
        data = [{"y": round(r[m], 1), "color": measure_color(round(r[m], 1), kinds[m])} for m, _, _ in metrics]
        series.append({"name": mode_series_name(r["route_type"], r["mode"]), "color": INK,
                       "data": data, "dataLabels": _glyph_labels(r["route_type"]), "tooltip": unit_tooltip(" %")})
    return {
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [label for _, label, _ in metrics], "title": {"text": None}},
        "yAxis": {"title": {"text": "%"}, "min": 0, "max": 100},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.1, "pointPadding": 0.05}},
        "legend": GLYPH_LEGEND,
    }


PERIOD_DETAILS = {
    "Matin": "lun. à ven.<br>6\u00a0h à 10\u00a0h", "Journée": "lun. à ven.<br>10\u00a0h à 16\u00a0h",
    "Pointe du soir": "lun. à ven.<br>16\u00a0h à 20\u00a0h", "Soirée & nuit": "lun. à ven.<br>20\u00a0h à 6\u00a0h",
    "Week-end": "samedi<br>et dimanche",
}
TREND_METRICS = {
    "score_fiabilite": ("Score de fiabilité", " / 100", 1, "score"),
    "pct_a_l_heure": ("À l'heure (−1 à +5 min)", " %", 1, "score"),
    "pct_retard_5min": ("Retards de plus de 5 min", " %", 1, "pourcent"),
    "retard_moyen_s": ("Retard moyen", " s", 0, "retard"),
    "pct_arrets_sautes": ("Arrêts non desservis", " %", 2, "pourcent"),
}
TREND_TERMS = {"score_fiabilite": "score-de-fiabilite", "pct_a_l_heure": "a-l-heure-ponctualite",
               "pct_retard_5min": "retards-de-plus-de-5-min", "retard_moyen_s": "retard-moyen",
               "pct_arrets_sautes": "arret-non-desservi"}
GROUP_ALL = "Ensemble"


def period_category(label: str) -> str:
    """Libellé d'axe d'un créneau : son nom, puis ses jours et heures en dessous."""
    detail = PERIOD_DETAILS.get(label)
    return f"{label}<br><span style=\"font-size:12px\">{detail}</span>" if detail else label


def period_score_chart(df: pd.DataFrame) -> dict:
    """Score de fiabilité par créneau (couleur = palier)."""
    data = [{"y": round(float(r["score_fiabilite"]), 1), "color": score_color(r["score_fiabilite"]),
             "ponct": round(float(r["pct_a_l_heure"]), 1), "skip": round(float(r["pct_arrets_sautes"]), 2),
             "passages": int(r["observations"])}
            for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [period_category(p) for p in df["période"]], "title": {"text": None}},
        "yAxis": {"title": {"text": "Score de fiabilité / 100"}, "min": 0, "max": 100},
        "series": [{"name": "Score de fiabilité", "data": data,
                    "tooltip": {"pointFormat": "Score de fiabilité : <b>{point.y:.1f} / 100</b><br/>"
                                               "À l'heure : {point.ponct:.1f} %<br/>"
                                               "Arrêts non desservis : {point.skip:.2f} %<br/>"
                                               "Passages : {point.passages:,.0f}<br/>"}}],
        "plotOptions": {"column": {"borderRadius": 4, "groupPadding": 0.05, "pointPadding": 0.08}},
        "lexique": "score-de-fiabilite",
    }


def _group_series(group: str, route_type, data: list, unit: str, decimals: int) -> dict:
    tooltip = unit_tooltip(unit, decimals)
    if group == GROUP_ALL:
        return {"name": GROUP_ALL, "data": data, "color": INK, "lineWidth": 3, "zIndex": 3,
                "marker": {"enabled": True, "radius": 4, "symbol": "circle"}, "tooltip": tooltip}
    return {**_mode_line_series(route_type, mode_series_name(route_type, group), data), "lineWidth": 1.5,
            "marker": {"enabled": True, "symbol": mode_marker(route_type), "radius": 4,
                       "fillColor": INK, "lineColor": INK, "lineWidth": 1},
            "tooltip": tooltip}


def groups_trend_chart(df: pd.DataFrame, metric: str, groups: list[str]) -> dict:
    """Une série par groupe (« Ensemble » ou un mode), jour par jour ; moyenne sur 7 jours pour l'ensemble seul."""
    label, unit, decimals, kind = TREND_METRICS[metric]
    series = []
    for group in groups:
        sub = df[df["groupe"] == group].sort_values("date_service")
        if sub.empty:
            continue
        values = sub[metric].astype(float)
        dates = [int(pd.Timestamp(ts).timestamp() * 1000) for ts in sub["date_service"]]
        data = [{"x": ms, "y": round(float(v), decimals), "color": measure_color(abs(float(v)), kind)}
                for ms, v in zip(dates, values) if pd.notna(v)]
        series.append(_group_series(group, sub["route_type"].iloc[0], data, unit, decimals))
        if groups == [GROUP_ALL]:
            ma = [[ms, round(float(values.iloc[max(0, i - 6):i + 1].mean()), decimals)] for i, ms in enumerate(dates)]
            series.append({"name": "Moyenne sur 7 jours", "data": ma, "color": INK_MUTED, "dashStyle": "ShortDash",
                           "lineWidth": 2, "marker": {"enabled": False}, "tooltip": unit_tooltip(unit, decimals)})
    y_axis = {"title": {"text": f"{label} ({unit.strip()})"}, "min": 0}
    if unit != " s":
        y_axis["max"] = 100
    return {
        "chart": {"type": "line", "height": 340},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": y_axis,
        "series": series,
        "tooltip": {"shared": True},
        "legend": {"enabled": len(series) > 1},
        "lexique": TREND_TERMS[metric],
    }


MIN_HOUR_PASSAGES = 30
MIN_HOUR_SHARE = 0.05


def thin_hours(passages: dict) -> set:
    """Heures à trop peu de passages pour être lues comme les autres : moins de MIN_HOUR_PASSAGES (un
    pourcentage y varie de ± 15 points d'un tirage à l'autre) ou moins de MIN_HOUR_SHARE de l'heure la plus
    chargée (service marginal, peu représentatif)."""
    if not passages:
        return set()
    busiest = max(passages.values())
    return {h for h, n in passages.items() if n < MIN_HOUR_PASSAGES or n < MIN_HOUR_SHARE * busiest}


def groups_hourly_chart(df: pd.DataFrame, groups: list[str]) -> dict:
    """Retards de plus de 5 min heure par heure, pour l'ensemble et les modes choisis ; les heures à trop peu
    de passages (`thin_hours`) sont grises, en pointillé, avec la raison dans l'infobulle."""
    series = []
    for group in groups:
        sub = df[df["groupe"] == group]
        if sub.empty:
            continue
        by_hour = {int(h): round(float(v), 1) for h, v in zip(sub["heure"], sub["pct_retard_5min"]) if pd.notna(v)}
        passages = ({int(h): int(n) for h, n in zip(sub["heure"], sub["observations"]) if pd.notna(n)}
                    if "observations" in sub else {})
        thin = thin_hours(passages)
        data = []
        for h in range(24):
            if h not in by_hour:
                data.append({"y": None})
            elif h in thin:
                data.append({"y": by_hour[h], "color": SCREEN_NO_DATA, "peu": passages.get(h, 0),
                             "marker": {"fillColor": "#FFFFFF", "lineColor": INK_MUTED, "lineWidth": 1.5}})
            else:
                data.append({"y": by_hour[h], "color": screen_hex(by_hour[h], "pourcent")})
        zones = []
        for h in range(24):
            grey = h in thin or (h + 1) in thin
            zones.append({"value": h + 1, **({"color": SCREEN_NO_DATA, "dashStyle": "Dot"} if grey else {})})
        tip = unit_tooltip(" %", header=HOUR_HEADER)
        tip["pointFormat"] += ('{#if point.peu}<span style="font-size:12px">Peu de passages à cette heure '
                               '({point.peu:,.0f}) : à lire avec prudence.</span><br/>{/if}')
        series.append({**_group_series(group, sub["route_type"].iloc[0], data, " %", 1),
                       "zoneAxis": "x", "zones": zones, "tooltip": tip})
    return {
        "chart": {"type": "line", "height": 330},
        "title": {"text": None},
        "xAxis": {"categories": [str(h) for h in range(24)], "title": {"text": "Heure locale"}, "labels": {"step": 2}},
        "yAxis": {"title": {"text": "Retards de plus de 5 min (%)"}, "min": 0},
        "series": series,
        "legend": {"enabled": len(series) > 1},
        "lexique": "retards-de-plus-de-5-min",
    }


def score_sparkline(daily: pd.DataFrame) -> dict:
    """Score de fiabilité jour par jour sur la période (accueil) : courbe à l'encre, points à la couleur du
    score, repères 80 et 50 en pointillés ; `daily` : date_service, score_fiabilite (ensemble du périmètre)."""
    df = daily.dropna(subset=["score_fiabilite"]).sort_values("date_service")
    data = [{"x": int(pd.Timestamp(d).timestamp() * 1000), "y": round(float(v), 1), "color": score_color(float(v))}
            for d, v in zip(df["date_service"], df["score_fiabilite"])]
    low = min([p["y"] for p in data] + [50.0])
    guide = {"color": INK_MUTED, "dashStyle": "Dot", "width": 1, "zIndex": 2}
    return {
        "chart": {"type": "spline", "height": 190, "spacing": [8, 8, 4, 4]},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "labels": {"format": "{value:%e %b}"}, "tickLength": 0},
        "yAxis": {"title": {"text": None}, "min": max(0, int(low // 10 * 10) - 5), "max": 100, "tickInterval": 25,
                  "plotLines": [{**guide, "value": 80, "label": {"text": "80 fiable", "align": "right", "x": -4,
                                                                  "style": {"color": INK_MUTED, "fontSize": FONT_MICRO}}},
                                {**guide, "value": 50, "label": {"text": "50 problématique", "align": "right", "x": -4,
                                                                  "style": {"color": INK_MUTED, "fontSize": FONT_MICRO}}}]},
        "legend": {"enabled": False},
        "series": [{"name": "Score de fiabilité", "data": data, "color": INK, "lineWidth": 2,
                    "marker": {"enabled": True, "radius": 4, "lineColor": "#FFFFFF", "lineWidth": 1},
                    "tooltip": {"pointFormat": "Score : <b>{point.y:,.0f} / 100</b><br/>", "xDateFormat": "%A %e %B"}}],
        "lexique": "score-de-fiabilite",
    }


def period_mode_chart(df: pd.DataFrame) -> dict:
    categories = list(dict.fromkeys(df["période"]))
    series = []
    for mode, sub in df.groupby("mode", sort=False):
        pa = sub.set_index("période").reindex(categories)
        route_type = sub["route_type"].iloc[0]
        data = [{"y": round(float(r["pct_retard_5min"]), 1),
                 "color": screen_hex(round(float(r["pct_retard_5min"]), 1), "pourcent")}
                if pd.notna(r["pct_retard_5min"]) else {"y": None}
                for _, r in pa.iterrows()]
        series.append({"name": mode_series_name(route_type, mode), "data": data, "color": INK,
                       "dataLabels": _glyph_labels(route_type), "tooltip": unit_tooltip(" %")})
    return {
        "lexique": "retards-de-plus-de-5-min",
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": {"categories": [period_category(p) for p in categories], "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": series,
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.06, "pointPadding": 0.05}},
        "legend": GLYPH_LEGEND,
    }


def engagement_progression_chart(prog: pd.DataFrame) -> dict:
    """Évolution du score de fiabilité par ligne (moitié récente − moitié précédente)."""
    df = prog.sort_values("delta_score")
    data = [{"y": round(r["delta_score"], 1),
             "color": INK if abs(r["delta_score"]) >= 2 else INK_35,
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
                    "tooltip": {"pointFormat": "Évolution : <b>{point.y:+.1f} points</b><br/>"
                                               "Score : {point.prec:.1f} → {point.score:.1f} / 100<br/>"}}],
        "plotOptions": {"bar": {"borderRadius": 4, "groupPadding": 0.1}},
        "lexique": "evolution",
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
             "color": screen_hex(drift_seconds.get(r["plage"], 1500), "retard")}
            for _, r in df.iterrows()]
    labels = [p if "min" in p else f"{p} min" for p in df["plage"]]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": labels, "title": {"text": "Écart à l'horaire théorique"}, "labels": {"rotation": -35}},
        "yAxis": {"title": {"text": "Nombre de passages"}},
        "series": [{"name": "Passages", "data": data,
                    "tooltip": {"headerFormat": "Écart à l'horaire : {point.key}<br/>",
                                "pointFormat": "<b>{point.y:,.0f} passages</b><br/>"}}],
        "lexique": "retard-median",
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
            "buttonTheme": {"fill": "#FFFFFF", "stroke": LINE, "style": {"color": INK_MUTED, "fontSize": FONT_MICRO}},
        },
        "navigator": {
            "enabled": True,
            "series": {"color": INK, "lineWidth": 1},
            "xAxis": {"labels": {"style": {"color": INK_MUTED}}},
        },
        "scrollbar": {
            "enabled": True,
            "barBackgroundColor": "#FFFFFF",
            "barBorderColor": LINE,
            "buttonBackgroundColor": LINE_SOFT,
            "buttonBorderColor": LINE,
            "trackBackgroundColor": LINE_SOFT,
            "trackBorderColor": LINE,
        },
        "title": {"text": None},
        "series": [{"type": "line", "name": "Observations", "data": data, "color": INK, "marker": {"enabled": False},
                    "tooltip": {"pointFormat": "<b>{point.y:,.0f} observations</b> reçues dans la minute<br/>"}}],
        "lexique": "observation",
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
    }


def hourly_distribution_chart(df: pd.DataFrame) -> dict:
    data = [int(r["observations"]) for _, r in df.iterrows()]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 280},
        "title": {"text": None},
        "xAxis": {"categories": [str(int(r["heure"])) for _, r in df.iterrows()], "title": {"text": "Heure locale"}, "labels": {"step": 2}},
        "yAxis": {"title": {"text": "Observations"}, "min": 0},
        "series": [{"name": "Observations", "data": data, "color": INK,
                    "tooltip": {"headerFormat": HOUR_HEADER,
                                "pointFormat": "<b>{point.y:,.0f} observations</b><br/>"}}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.05, "pointPadding": 0.05}},
        "lexique": "observation",
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
            band = {"from": start - 0.5, "to": i - 0.5, "color": SURFACE, "borderColor": LINE,
                    "borderWidth": 1, "zIndex": 0}
            if not bands and commune_label:
                band["label"] = {"text": commune_label, "align": "left", "x": 4, "y": 12,
                                 "style": {"color": INK_MUTED, "fontSize": FONT_MICRO, "fontWeight": "600"}}
            bands.append(band)
            start = None
    return bands


def _stop_line(stop_ids: list, stop_id: str | None) -> list:
    if stop_id is None or stop_id not in stop_ids:
        return []
    return [{"value": stop_ids.index(stop_id), "color": INK, "dashStyle": "Dash", "width": 2,
             "zIndex": 4, "label": {"text": "Cet arrêt", "rotation": 0, "y": 12, "x": 4,
                                    "style": {"color": INK, "fontWeight": "600", "fontSize": FONT_MICRO}}}]


def _profile_axis(p: pd.DataFrame, highlight_stop_id, commune_stop_ids, commune_label) -> dict:
    ids = p["stop_id"].tolist()
    return {"categories": p["stop_name"].tolist(), "labels": {"rotation": -50, "style": {"fontSize": FONT_MICRO}},
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
    gains = [{"y": round(float(g), 1), "color": VIN if sid in hot else INK_35,
              "prev": prev if isinstance(prev, str) else "n.d."}
             for sid, g, prev in zip(p["stop_id"], p["gain_s"], p["prev_stop_name"])]
    delays = [round(float(v), 1) if pd.notna(v) else None for v in p["delay_s"]]
    return {
        "chart": {"height": 360},
        "title": {"text": None},
        "xAxis": _profile_axis(p, highlight_stop_id, commune_stop_ids, commune_label),
        "yAxis": [{"title": {"text": "Retard (secondes)"}}],
        "series": [
            {"type": "column", "name": "Retard pris sur le tronçon", "data": gains, "color": INK_35,
             "tooltip": {"pointFormat": "Retard pris depuis {point.prev} : <b>{point.y:+.0f} s</b><br/>"}},
            {"type": "line", "name": "Retard moyen à l'arrêt", "data": delays, "color": INK,
             "lineWidth": 2, "marker": {"enabled": False}, "tooltip": unit_tooltip(" s", 0)},
        ],
        "tooltip": {"shared": True},
        "lexique": "retard-deja-present-retard-pris",
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
            {"name": slot_label, "data": _values("slot_delay"), "color": INK, "lineWidth": 3,
             "marker": {"enabled": True, "radius": 3, "symbol": "circle"}, "connectNulls": True},
            {"name": "D'habitude (autres jours et heures)", "data": _values("usual_delay"),
             "color": INK_35, "dashStyle": "ShortDash", "lineWidth": 2,
             "marker": {"enabled": False}, "connectNulls": True},
        ],
        "tooltip": {"shared": True, "valueSuffix": " s", "valueDecimals": 0},
        "legend": {"enabled": True},
        "lexique": "retard-moyen",
    }


def skip_profile_chart(profile: pd.DataFrame, commune_stop_ids: set | None = None,
                       commune_label: str | None = None, highlight_stop_id: str | None = None) -> dict:
    """Taux d'arrêts non desservis arrêt par arrêt le long d'une direction (palier « pourcent »)."""
    p = profile.sort_values("order").reset_index(drop=True)
    rates = (p["skipped"] / p["eligible"].where(p["eligible"] > 0) * 100).fillna(0.0)
    data = [{"y": round(float(r), 1), "color": screen_hex(round(float(r), 1), "pourcent"),
             "skipped": int(k)} for r, k in zip(rates, p["skipped"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 300},
        "title": {"text": None},
        "xAxis": _profile_axis(p, highlight_stop_id, commune_stop_ids, commune_label),
        "yAxis": {"title": {"text": "Arrêts non desservis (%)"}, "min": 0},
        "series": [{"name": "Arrêts non desservis", "data": data,
                    "tooltip": {"pointFormat": "Arrêts non desservis : <b>{point.y:.1f} %</b> ({point.skipped:,.0f} passages)<br/>"}}],
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
        "lexique": "arret-non-desservi",
    }


def stop_lines_chart(lines: pd.DataFrame) -> dict:
    """Passages > 5 min et arrêts non desservis par ligne à un arrêt (nombres absolus)."""
    df = lines.sort_values("cnt_gt300", ascending=False)
    categories = [f"{mode_glyph(rt)} {l}" for rt, l in zip(df["route_type"], df["ligne"])]
    return {
        "chart": {"type": "bar", "height": max(160, 60 + 34 * len(df))},
        "title": {"text": None},
        "xAxis": {"categories": categories, "title": {"text": None}},
        "yAxis": {"title": {"text": "Passages"}, "min": 0, "allowDecimals": False},
        "series": [
            {"name": "Passages à plus de 5 min", "data": [int(v) for v in df["cnt_gt300"]], "color": INK,
             "tooltip": unit_tooltip(" passages", 0)},
            {"name": "Arrêts non desservis", "data": [int(v) for v in df["skipped"]], "color": INK_35,
             "tooltip": unit_tooltip(" passages", 0)},
        ],
        "lexique": "retards-de-plus-de-5-min",
        "plotOptions": {"bar": {"stacking": "normal", "borderRadius": 3, "groupPadding": 0.1}},
        "legend": {"enabled": True},
    }


def risk_by_label_chart(table: pd.DataFrame, label_col: str) -> dict:
    """Part des passages > 5 min par créneau ou par jour (palier « pourcent »)."""
    data = [{"y": round(float(v), 1), "color": screen_hex(round(float(v), 1), "pourcent"),
             "passages": int(o)} for v, o in zip(table["pct_gt300"], table["obs"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 260},
        "title": {"text": None},
        "xAxis": {"categories": table[label_col].tolist(), "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0},
        "series": [{"name": "Retards > 5 min", "data": data,
                    "tooltip": {"pointFormat": "Retards de plus de 5 min : <b>{point.y:.1f} %</b><br/>Passages : {point.passages:,.0f}<br/>"}}],
        "plotOptions": {"column": {"borderRadius": 3, "groupPadding": 0.08, "pointPadding": 0.05}},
        "lexique": "retards-de-plus-de-5-min",
    }


def daily_status_chart(daily: pd.DataFrame, threshold: float) -> dict:
    """Bande quotidienne : part des passages > 5 min par jour, seuil du jour dégradé tracé."""
    d = (daily.groupby("date_service").agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum"))
         .reset_index())
    d = d[d["obs"] > 0]
    data = [{"x": int(pd.Timestamp(ts).timestamp() * 1000), "y": round(float(c) / float(o) * 100, 1),
             "color": screen_hex(round(float(c) / float(o) * 100, 1), "pourcent"), "passages": int(o)}
            for ts, o, c in zip(d["date_service"], d["obs"], d["cnt_gt300"])]
    return {
        "legend": {"enabled": False},
        "chart": {"type": "column", "height": 240},
        "title": {"text": None},
        "xAxis": {"type": "datetime", "title": {"text": None}},
        "yAxis": {"title": {"text": "Retards > 5 min (%)"}, "min": 0,
                  "plotLines": [{"value": threshold, "color": INK_MUTED, "dashStyle": "Dash", "width": 1,
                                 "zIndex": 3, "label": {"text": "jour dégradé", "align": "right",
                                                        "style": {"color": INK_MUTED}}}]},
        "series": [{"name": "Retards > 5 min", "data": data,
                    "tooltip": {"pointFormat": "Retards de plus de 5 min : <b>{point.y:.1f} %</b><br/>Passages : {point.passages:,.0f}<br/>"}}],
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
        "lexique": "jour-degrade",
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
        "series": [{"name": "Courses supprimées", "data": data, "color": INK,
                    "tooltip": {"pointFormat": "<b>{point.y} courses supprimées</b> sur {point.trips:,.0f} connues<br/>"}}],
        "plotOptions": {"column": {"borderRadius": 2, "groupPadding": 0.05, "pointPadding": 0.05}},
        "lexique": "course-supprimee",
    }
