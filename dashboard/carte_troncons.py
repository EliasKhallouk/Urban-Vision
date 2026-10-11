"""Carte des points de contention : composant Streamlit (st.components.v2) qui pilote deck.gl.

Chaque tronçon entre deux arrêts consécutifs est colorié selon le temps que les bus y perdent
(table `troncons_carte`, calculée chaque nuit par src/scripts/troncons.py). Toutes les tranches
horaires partent en une fois : le composant change de tranche sans relancer Streamlit.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from highcharts import CHART_FONT_CSS

_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
from palette import (  # noqa: E402
    BLACK_FOREST,
    COPPERWOOD,
    MEDIUM,
    NEGATIVE,
    NO_DATA,
    OLIVE_LEAF,
    POSITIVE,
    SUNLIT_CLAY,
)

SLOTS = 25
WHOLE_DAY_SLOT = 24
MAP_HEIGHT = 640
OFFSET_METERS = 7.0
NETWORK_VIEW = {"longitude": -0.579, "latitude": 44.838, "zoom": 11.0}
COMMUNE_ZOOM = 12.5
TIER_CODES = {POSITIVE: "v", MEDIUM: "o", NEGATIVE: "c"}
STATUS_CODES = {"peu": "p", "terminus": "t", "douteux": "d"}
ABSENT = "-"
COLORS = {"v": OLIVE_LEAF, "o": SUNLIT_CLAY, "c": COPPERWOOD, "grey": NO_DATA}

_JS = Path(__file__).with_name("carte_troncons.js").read_text(encoding="utf-8")
_CSS = f"""
@import url("{CHART_FONT_CSS}/400.css");
@import url("{CHART_FONT_CSS}/600.css");
@import url("{CHART_FONT_CSS}/700.css");
.uvt {{ position: relative; width: 100%; border-radius: 12px; overflow: hidden; background: #F2F2EE;
       border: 1px solid rgba(40, 54, 24, .18); font-family: 'Source Sans 3', 'Segoe UI', sans-serif; color: {BLACK_FOREST}; }}
.uvt-canvas {{ position: absolute !important; inset: 0; }}
.uvt canvas {{ outline: none; }}
.uvt-tip {{ position: absolute; z-index: 4; display: none; pointer-events: none; max-width: 320px; background: #FFFFFF;
           border: 1px solid rgba(40, 54, 24, .2); border-radius: 10px; padding: 10px 12px;
           box-shadow: 0 8px 22px rgba(40, 54, 24, .16); font-size: 13px; line-height: 1.45; }}
.uvt-tip b {{ display: block; font-size: 15px; margin-bottom: 2px; }}
.uvt-tip .l {{ color: #606c38; font-size: 12px; margin-bottom: 6px; }}
.uvt-tip .v {{ font-size: 14px; }}
.uvt-tip .v strong {{ font-size: 18px; }}
.uvt-tip .m {{ color: #606c38; font-size: 12px; margin-top: 4px; }}
.uvt-legend {{ position: absolute; z-index: 3; left: 12px; bottom: 12px; right: 12px; display: flex; flex-wrap: wrap;
              align-items: center; gap: 8px 18px; background: rgba(255, 255, 255, .94); border-radius: 10px;
              padding: 9px 14px; box-shadow: 0 2px 10px rgba(40, 54, 24, .12); font-size: 13px; }}
.uvt-bar {{ display: flex; align-items: stretch; border-radius: 6px; overflow: hidden; min-width: 300px; }}
.uvt-bar span {{ flex: 1; padding: 4px 8px; color: #FFFFFF; font-weight: 600; white-space: nowrap; text-align: center; }}
.uvt-bar span.o {{ color: {BLACK_FOREST}; }}
.uvt-grey {{ display: inline-flex; align-items: center; gap: 6px; color: #606c38; }}
.uvt-swatch {{ display: inline-block; width: 26px; height: 0; border-top: 4px solid {NO_DATA}; }}
.uvt-swatch.ferry {{ border-top: 4px dashed {BLACK_FOREST}; }}
.uvt-swatch.bus {{ border-top: 4px solid {BLACK_FOREST}; }}
.uvt-swatch.tram {{ border-top: 7px dotted #A9A9A0; }}
.uvt-error {{ padding: 24px; font-size: 14px; }}
"""

COMPONENT_NAME = "urban_vision_carte_troncons"


def _register():
    return st.components.v2.component(COMPONENT_NAME, js=_JS, css=_CSS)


_carte = _register()


def slot_index(heure: int) -> int:
    return WHOLE_DAY_SLOT if int(heure) < 0 else int(heure)


def cell_code(statut: str, palier: str | None) -> str:
    """Code d'une case : v, o, c (temps perdu mesuré) ; p (pas assez de données), t (début ou fin de
    ligne), d (temps réel de la ligne insuffisant) ; - (aucun passage)."""
    if statut == "mesure" and palier in TIER_CODES:
        return TIER_CODES[palier]
    return STATUS_CODES.get(statut, ABSENT)


def offset_right(coords: list, meters: float = OFFSET_METERS) -> list:
    """Décale un tracé de `meters` vers la droite du sens de circulation, pour séparer les deux sens d'une rue."""
    if len(coords) < 2:
        return coords
    lat0 = sum(p[1] for p in coords) / len(coords)
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 111_320.0
    out = []
    for i, (lon, lat) in enumerate(coords):
        a = coords[max(i - 1, 0)]
        b = coords[min(i + 1, len(coords) - 1)]
        dx, dy = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
        length = math.hypot(dx, dy) or 1.0
        nx, ny = dy / length, -dx / length
        out.append([round(lon + nx * meters / kx, 6), round(lat + ny * meters / ky, 6)])
    return out


def _trimmed(values: list, share: float = 0.01) -> tuple[float, float]:
    """Bornes d'une liste de coordonnées sans les 1 % les plus éloignés de chaque côté (lignes lointaines)."""
    ordered = sorted(values)
    k = int(len(ordered) * share)
    return ordered[k], ordered[-1 - k]


def worst_slot(codes: list[str]) -> int | None:
    """Heure (0-23) où le plus de tronçons perdent beaucoup de temps, puis un peu ; None s'il n'y en a aucune."""
    best, best_key = None, (0, 0)
    for h in range(WHOLE_DAY_SLOT):
        key = (sum(c[h] == "c" for c in codes), sum(c[h] == "o" for c in codes))
        if key > best_key:
            best, best_key = h, key
    return best


def troncons_payload(geo: pd.DataFrame, cells: pd.DataFrame, lines: dict, names: dict, communes: dict,
                     tram_paths: list, commune: str | None, slot: int) -> dict:
    """Données du composant : tronçons (tracé décalé, mode, codes des 25 tranches, valeurs mesurées),
    tracés du tram, vue de départ et tranche affichée.

    `geo` : from_stop_id, to_stop_id, route_type, coords (liste) ; `cells` : heure, from_stop_id,
    to_stop_id, statut, palier, mediane_s, mediane_pct, passages ; `lines` : (de, vers) -> « 1, 16 » ;
    `communes` : stop_id -> commune.
    """
    by_seg: dict = {}
    for r in cells.itertuples(index=False):
        seg = by_seg.setdefault((r.from_stop_id, r.to_stop_id), {"codes": [ABSENT] * SLOTS, "values": {}})
        i = slot_index(r.heure)
        seg["codes"][i] = cell_code(r.statut, r.palier)
        if r.statut == "mesure":
            seg["values"][i] = [int(round(r.mediane_s)), int(round(r.mediane_pct)), int(r.passages)]
    segments, lons, lats, all_lons, all_lats = [], [], [], [], []
    for g in geo.itertuples(index=False):
        seg = by_seg.get((g.from_stop_id, g.to_stop_id))
        if seg is None:
            continue
        inside = commune is None or commune in (communes.get(g.from_stop_id), communes.get(g.to_stop_id))
        all_lons += [p[0] for p in g.coords]
        all_lats += [p[1] for p in g.coords]
        if commune is not None and inside:
            lons += [p[0] for p in g.coords]
            lats += [p[1] for p in g.coords]
        segments.append({
            "p": offset_right(g.coords), "m": int(g.route_type), "t": "".join(seg["codes"]), "v": seg["values"],
            "n": f"{names.get(g.from_stop_id, g.from_stop_id)} vers {names.get(g.to_stop_id, g.to_stop_id)}",
            "l": lines.get((g.from_stop_id, g.to_stop_id), ""), "c": 1 if inside else 0,
        })
    view = dict(NETWORK_VIEW)
    box_lons, box_lats = (lons, lats) if lons else (all_lons, all_lats)
    bounds = None
    if box_lons:
        lo_x, hi_x = _trimmed(box_lons)
        lo_y, hi_y = _trimmed(box_lats)
        bounds = [[lo_x, lo_y], [hi_x, hi_y]]
        view = {"longitude": (lo_x + hi_x) / 2, "latitude": (lo_y + hi_y) / 2,
                "zoom": COMMUNE_ZOOM if lons else NETWORK_VIEW["zoom"]}
    return {"segments": segments, "tram": tram_paths, "slot": int(slot), "view": view, "bounds": bounds,
            "view_key": commune or "__reseau__", "colors": COLORS, "height": MAP_HEIGHT}


def carte_troncons(payload: dict, key: str) -> None:
    """Affiche la carte des tronçons (réenregistre le composant s'il a été importé hors du serveur)."""
    global _carte
    try:
        _carte(key=key, data=payload, height=MAP_HEIGHT)
    except ValueError as error:
        if "is not registered" not in str(error):
            raise
        _carte = _register()
        _carte(key=key, data=payload, height=MAP_HEIGHT)
