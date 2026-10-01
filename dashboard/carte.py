"""Carte des arrêts : composant Streamlit (st.components.v2) qui pilote deck.gl.

Le composant regroupe les quais d'un même arrêt quand la carte est dézoomée et
les sépare au-delà de SPLIT_ZOOM, affiche une infobulle par arrêt, signale
l'arrêt sélectionné (halo et bandeau) et renvoie à Python l'arrêt cliqué.
deck.gl et MapLibre sont chargés depuis jsDelivr ; le fond de carte est le style
vectoriel « Positron » de Carto (les tuiles raster de Carto exigent une clé).
"""

from __future__ import annotations

import base64
import io
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image, ImageDraw

_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
from palette import (  # noqa: E402
    BLACK_FOREST,
    COPPERWOOD,
    CORNSILK,
    OLIVE_LEAF,
    SUNLIT_CLAY,
    hex as palette_hex,
    mode_marker,
)

MARKER_SHAPES = ("circle", "square", "triangle", "diamond")
MARKER_CELL = 48
STOP_SIZE_METERS = (120.0, 240.0)
STOP_SIZE_PIXELS = (8, 28)
SPLIT_ZOOM = 14.0
FOCUS_ZOOM = 15.0
MIN_SEP_PX = 30
MAP_HEIGHT = 480
NETWORK_VIEW = {"longitude": -0.579, "latitude": 44.838, "zoom": 10.0}
COMMUNE_ZOOM = 11.5

_JS = Path(__file__).with_name("carte_arrets.js").read_text(encoding="utf-8")
_CSS = f"""
.uv-map {{ position: relative; width: 100%; border-radius: 10px; overflow: hidden;
          border: 1px solid rgba(221, 161, 94, .45); background: #F4F4EF; font-family: Inter, 'Segoe UI', sans-serif; }}
.uv-canvas {{ position: absolute !important; inset: 0; width: 100%; height: 100%; }}
.uv-map canvas {{ outline: none; }}
.uv-tip {{ position: absolute; z-index: 3; display: none; pointer-events: none; max-width: 340px;
          background: #FFFFFF; color: {BLACK_FOREST}; border: 1px solid rgba(221, 161, 94, .6);
          border-radius: 10px; padding: 10px 12px; box-shadow: 0 6px 18px rgba(40, 54, 24, .18);
          font-size: 12.5px; line-height: 1.45; }}
.uv-title {{ font-weight: 700; font-size: 14px; margin-bottom: 2px; }}
.uv-sub {{ color: rgba(96, 108, 56, .8); font-size: 11.5px; margin-bottom: 4px; }}
.uv-row {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
.uv-chip {{ flex: none; width: 10px; height: 10px; border-radius: 2px; }}
.uv-num {{ margin-left: auto; padding-left: 10px; white-space: nowrap; color: rgba(40, 54, 24, .8); }}
.uv-more {{ color: rgba(96, 108, 56, .8); font-size: 11.5px; }}
.uv-foot {{ margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(221, 161, 94, .35);
           color: rgba(96, 108, 56, .85); font-size: 11.5px; }}
.uv-cta {{ margin-top: 6px; font-weight: 600; }}
.uv-badge {{ position: absolute; z-index: 2; top: 10px; left: 10px; display: flex; align-items: center; gap: 8px;
            max-width: calc(100% - 20px); background: {CORNSILK}; color: {BLACK_FOREST};
            border: 1.5px solid {BLACK_FOREST}; border-radius: 999px; padding: 5px 6px 5px 12px;
            font-size: 12.5px; box-shadow: 0 2px 8px rgba(40, 54, 24, .18); }}
.uv-badge span {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.uv-dot {{ flex: none; width: 10px; height: 10px; border-radius: 50%; background: {BLACK_FOREST};
          box-shadow: 0 0 0 3px rgba(40, 54, 24, .25); }}
.uv-badge button {{ flex: none; border: none; border-radius: 999px; background: {BLACK_FOREST}; color: #FFFFFF;
                   padding: 3px 10px; font-size: 12px; cursor: pointer; }}
.uv-hint {{ position: absolute; z-index: 2; left: 10px; bottom: 8px; background: rgba(255, 255, 255, .88);
           color: rgba(40, 54, 24, .85); border-radius: 6px; padding: 2px 8px; font-size: 11.5px; }}
.uv-error {{ padding: 24px; color: {BLACK_FOREST}; font-size: 14px; }}
"""

COMPONENT_NAME = "urban_vision_carte_arrets"


def _register():
    return st.components.v2.component(COMPONENT_NAME, js=_JS, css=_CSS)


_carte = _register()


@lru_cache(maxsize=1)
def marker_atlas() -> tuple[str, dict]:
    """Atlas PNG des icônes (forme du mode × couleur de palier, plus le halo de sélection) et son index."""
    colors = (OLIVE_LEAF, SUNLIT_CLAY, COPPERWOOD)
    scale, cell = 4, MARKER_CELL
    atlas = Image.new("RGBA", (cell * len(MARKER_SHAPES), cell * (len(colors) + 1)), (0, 0, 0, 0))
    mapping = {}
    n, m, w = cell * scale, 5 * scale, 3 * scale
    for j, color in enumerate(colors):
        for i, shape in enumerate(MARKER_SHAPES):
            big = Image.new("RGBA", (n, n), (0, 0, 0, 0))
            draw = ImageDraw.Draw(big)
            if shape == "circle":
                draw.ellipse([m, m, n - m, n - m], fill=color, outline="white", width=w)
            elif shape == "square":
                draw.rectangle([m + 2 * scale, m + 2 * scale, n - m - 2 * scale, n - m - 2 * scale],
                               fill=color, outline="white", width=w)
            elif shape == "triangle":
                draw.polygon([(n / 2, m), (n - m, n - m), (m, n - m)], fill=color, outline="white", width=w)
            else:
                draw.polygon([(n / 2, m), (n - m, n / 2), (n / 2, n - m), (m, n / 2)],
                             fill=color, outline="white", width=w)
            atlas.paste(big.resize((cell, cell), Image.LANCZOS), (i * cell, j * cell))
            mapping[f"{shape}|{color}"] = {"x": i * cell, "y": j * cell, "width": cell, "height": cell,
                                           "anchorY": cell // 2}
    halo = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([w, w, n - w, n - w], fill=CORNSILK + "E6", outline=BLACK_FOREST, width=w)
    top = cell * len(colors)
    atlas.paste(halo.resize((cell, cell), Image.LANCZOS), (0, top))
    mapping["halo"] = {"x": 0, "y": top, "width": cell, "height": cell, "anchorY": cell // 2}
    buffer = io.BytesIO()
    atlas.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), mapping


def icon_key(route_type, score: float) -> str:
    """Clé d'icône de l'atlas : forme du mode et couleur du palier de score."""
    return f"{mode_marker(route_type)}|{palette_hex(score, 'score')}"


def _size_m(observations: float) -> float:
    low, high = STOP_SIZE_METERS
    return round(low + (min(max(float(observations), 50.0), 400.0) - 50.0) / 350.0 * (high - low), 1)


def map_view(stops: pd.DataFrame, commune: str | None) -> dict:
    """Vue de départ : le réseau entier, ou la commune centrée sur ses arrêts."""
    if commune is None or stops.empty:
        return dict(NETWORK_VIEW)
    return {"longitude": float(stops["lon"].mean()), "latitude": float(stops["lat"].mean()), "zoom": COMMUNE_ZOOM}


def map_payload(stops: pd.DataFrame, groups: pd.DataFrame, selected_id: str | None, commune: str | None,
                focus_on_load: bool = False) -> dict:
    """Données transmises au composant : arrêts, regroupements, arrêt sélectionné et réglages.

    `stops` : un arrêt par ligne (stop_id, stop_name, direction, lat, lon,
    route_type, score_fiabilite, pct_retard_5min, observations, lignes) ;
    `groups` : sortie de group_stops (members = stop_id du groupe, stop_id =
    sens le moins fiable). Les listes sont triées du plus fiable au moins
    fiable : deck.gl dessine les moins fiables par-dessus.
    """
    ordered = stops.sort_values("score_fiabilite", ascending=False).reset_index(drop=True)
    index = {sid: i for i, sid in enumerate(ordered["stop_id"])}
    group_of = [0] * len(ordered)
    out_groups = []
    for g in groups.sort_values("score_fiabilite", ascending=False).itertuples():
        members = [index[m] for m in g.members if m in index]
        if not members:
            continue
        for m in members:
            group_of[m] = len(out_groups)
        out_groups.append({"x": round(float(g.lon), 6), "y": round(float(g.lat), 6),
                           "k": icon_key(g.route_type, g.score_fiabilite), "z": _size_m(g.observations),
                           "w": g.stop_id, "m": members, "o": int(g.observations)})
    out_stops = [{"i": r.stop_id, "n": r.stop_name, "d": r.direction or "", "y": round(float(r.lat), 6),
                  "x": round(float(r.lon), 6), "k": icon_key(r.route_type, r.score_fiabilite),
                  "s": round(float(r.score_fiabilite), 1), "p": round(float(r.pct_retard_5min), 1),
                  "o": int(r.observations), "l": r.lignes, "g": group_of[i], "z": _size_m(r.observations)}
                 for i, r in enumerate(ordered.itertuples())]
    uri, mapping = marker_atlas()
    return {
        "stops": out_stops, "groups": out_groups, "selected": selected_id if selected_id in index else None,
        "view": map_view(stops, commune), "view_key": commune or "__reseau__", "focus_on_load": focus_on_load,
        "atlas": {"url": uri, "mapping": mapping}, "size_px": list(STOP_SIZE_PIXELS), "split_zoom": SPLIT_ZOOM,
        "focus_zoom": FOCUS_ZOOM, "min_sep_px": MIN_SEP_PX, "height": MAP_HEIGHT,
    }


def carte_arrets(payload: dict, key: str, on_click) -> None:
    """Affiche la carte ; `on_click` est appelé quand un arrêt est cliqué (valeur dans session_state[key]).

    Le composant est enregistré à l'import ; si le module a été importé hors du
    serveur Streamlit (registre provisoire), il est réenregistré au premier
    affichage.
    """
    global _carte
    try:
        _carte(key=key, data=payload, on_clicked_change=on_click, height=MAP_HEIGHT)
    except ValueError as error:
        if "is not registered" not in str(error):
            raise
        _carte = _register()
        _carte(key=key, data=payload, on_clicked_change=on_click, height=MAP_HEIGHT)
