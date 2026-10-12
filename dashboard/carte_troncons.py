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


_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
from palette import (  # noqa: E402
    ACTION,
    GARONNE,
    INK,
    INK_MUTED,
    LIE,
    LINE,
    MEDIUM,
    NEGATIVE,
    PIERRE,
    POSITIVE,
    SCALE,
    SCREEN_NO_DATA,
    SURFACE,
    TRONCON_ANCHORS,
    VIN,
    retained_seconds,
)

SLOTS = 25
TRAM_TYPE = 0
WHOLE_DAY_SLOT = 24
MAP_HEIGHT = 640
OFFSET_METERS = 7.0
NETWORK_VIEW = {"longitude": -0.579, "latitude": 44.838, "zoom": 11.0}
COMMUNE_ZOOM = 12.5
TIER_CODES = {POSITIVE: "v", MEDIUM: "o", NEGATIVE: "c"}
STATUS_CODES = {"peu": "p", "terminus": "t", "douteux": "d"}
ABSENT = "-"
COLORS = {"v": GARONNE, "o": PIERRE, "c": VIN, "x": LIE, "grey": SCREEN_NO_DATA}
SCALE_PAYLOAD = {"anchors": list(TRONCON_ANCHORS), "colors": list(SCALE)}

_JS = Path(__file__).with_name("carte_troncons.js").read_text(encoding="utf-8")
_CSS = f"""
.uvt {{ position: relative; width: 100%; border-radius: 12px; overflow: hidden; background: #F2F2EE;
       border: 1px solid rgba(43, 37, 35, .18); font-family: 'Source Sans', 'Source Sans 3', sans-serif; color: {INK}; }}
.uvt-canvas {{ position: absolute !important; inset: 0; }}
.uvt canvas {{ outline: none; }}
.uvt-head {{ position: absolute; z-index: 3; top: 12px; left: 12px; max-width: min(440px, calc(100% - 90px));
            background: rgba(255, 255, 255, .95); border-radius: 10px; padding: 11px 14px 10px;
            box-shadow: 0 2px 10px rgba(43, 37, 35, .12); }}
.uvt-label {{ font-size: 22px; font-weight: 700; line-height: 1.15; margin-bottom: 4px; letter-spacing: -.01em; }}
.uvt-verdict {{ font-size: 16px; font-weight: 600; line-height: 1.35; }}
.uvt-sub {{ color: {INK_MUTED}; font-size: 12.5px; margin-top: 4px; }}
.uvt-bottom {{ position: absolute; z-index: 3; left: 12px; right: 12px; bottom: 12px; background: rgba(255, 255, 255, .96);
              border-radius: 10px; padding: 10px 14px 9px; box-shadow: 0 2px 10px rgba(43, 37, 35, .12); }}
.uvt-controls {{ display: flex; align-items: center; gap: 16px; }}
.uvt-clock {{ min-width: 128px; font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }}
.uvt-range-box {{ flex: 1; }}
.uvt-range {{ width: 100%; accent-color: {ACTION}; cursor: pointer; margin: 0; }}
.uvt-range.off {{ opacity: .4; }}
.uvt-range:focus-visible, .uvt-allday:focus-visible, .uvt-play:focus-visible, .uvt-close:focus-visible {{ outline: 2px solid {ACTION}; outline-offset: 2px; }}
.uvt-ticks {{ display: flex; justify-content: space-between; color: {INK_MUTED}; font-size: 11.5px; margin-top: 1px; }}
.uvt-allday {{ flex: none; border: 1.5px solid {ACTION}; border-radius: 999px; background: #FFFFFF; color: {ACTION};
              padding: 6px 14px; font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; }}
.uvt-allday[aria-pressed="true"] {{ background: {ACTION}; border-color: {ACTION}; color: #FFFFFF; }}
.uvt-play {{ flex: none; border: none; border-radius: 999px; background: #F2F2EE; color: {INK}; padding: 7px 14px;
            font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap; }}
.uvt-play[aria-pressed="true"] {{ background: {SURFACE}; box-shadow: inset 0 0 0 1.5px {ACTION}; }}
.uvt-legend {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; margin-top: 8px; padding-top: 8px;
              border-top: 1px solid rgba(43, 37, 35, .12); font-size: 12.5px; }}
.uvt-scale {{ display: inline-flex; flex-direction: column; min-width: 230px; flex: 0 1 300px; }}
.uvt-grad {{ height: 10px; border-radius: 3px; }}
.uvt-sticks {{ position: relative; height: 16px; color: {INK_MUTED}; font-size: 11.5px; }}
.uvt-sticks span {{ position: absolute; top: 2px; transform: translateX(-50%); white-space: nowrap; }}
.uvt-sticks span:first-child {{ transform: none; }}
.uvt-sticks span:last-child {{ transform: translateX(-100%); }}
.uvt-sticks span::before {{ content: ""; position: absolute; left: 50%; top: -3px; width: 1px; height: 4px; background: {INK_MUTED}; }}
.uvt-sticks span:first-child::before {{ left: 0; }}
.uvt-sticks span:last-child::before {{ left: 100%; }}
.uvt-words {{ color: {INK}; font-size: 12.5px; font-weight: 600; margin-bottom: 3px; }}
.uvt-key {{ display: inline-flex; align-items: center; gap: 6px; color: {INK_MUTED}; }}
.uvt-swatch {{ display: inline-block; width: 24px; height: 0; border-top: 3px dashed #A8A8A2; }}
.uvt-swatch.ferry {{ border-top: 4px dashed {INK}; }}
.uvt-swatch.bus {{ border-top: 3px solid {INK}; }}
.uvt-swatch.tram {{ border-top: 6px dotted #A9A9A0; }}
.uvt-tip {{ position: absolute; z-index: 5; display: none; pointer-events: none; max-width: 300px; background: #FFFFFF;
           border: 1px solid rgba(43, 37, 35, .2); border-radius: 10px; padding: 9px 11px;
           box-shadow: 0 8px 22px rgba(43, 37, 35, .16); font-size: 13px; line-height: 1.4; }}
.uvt-tip b {{ display: block; font-size: 14px; }}
.uvt-tip .l {{ color: {INK_MUTED}; font-size: 12px; margin-bottom: 4px; }}
.uvt-tip .m {{ color: {INK_MUTED}; font-size: 12px; margin-top: 4px; }}
.uvt-tip .n {{ font-size: 12px; margin-top: 3px; font-variant-numeric: tabular-nums; }}
.uvt-panel {{ position: absolute; z-index: 4; display: none; top: 12px; right: 56px; width: 330px;
             max-height: calc(100% - 175px); overflow: auto; background: #FFFFFF; border-radius: 12px; padding: 14px 16px 12px;
             border: 1px solid rgba(43, 37, 35, .2); box-shadow: 0 10px 28px rgba(43, 37, 35, .2); font-size: 13.5px; line-height: 1.45; }}
.uvt-panel h3 {{ margin: 0 26px 2px 0; font-size: 16px; line-height: 1.25; }}
.uvt-panel p {{ margin: 6px 0; }}
.uvt-lines {{ color: {INK_MUTED}; font-size: 12.5px; }}
.uvt-figure {{ margin-top: 10px; font-size: 28px; font-weight: 700; line-height: 1.1; color: {INK}; }}
.uvt-figure.ok {{ font-size: 20px; }}
.uvt-grey-reason {{ margin-top: 10px !important; font-weight: 600; }}
.uvt-foot {{ color: {INK_MUTED}; font-size: 12px; }}
.uvt-tram-row {{ display: flex; justify-content: space-between; gap: 12px; margin-top: 10px; align-items: baseline; }}
.uvt-tram-row b {{ font-size: 18px; white-space: nowrap; }}
.uvt-tram-row b.na {{ font-size: 13px; font-weight: 600; color: {INK_MUTED}; }}
.uvt-tram-row + .uvt-foot {{ margin-top: 0 !important; }}
.uvt-close {{ position: absolute; top: 8px; right: 8px; width: 28px; height: 28px; border: none; border-radius: 50%;
             background: #F2F2EE; color: {INK}; font-size: 18px; line-height: 1; cursor: pointer; }}
.uvt-strip {{ display: grid; grid-template-columns: repeat(24, 1fr); gap: 2px; margin-top: 10px; }}
.uvt-strip span {{ height: 16px; border-radius: 2px; }}
.uvt-strip span.now {{ outline: 2px solid {INK}; outline-offset: 1px; }}
.uvt-strip-axis {{ display: flex; justify-content: space-between; color: {INK_MUTED}; font-size: 11px; margin-top: 3px; }}
.uvt-error {{ padding: 24px; font-size: 14px; }}
.uvt-top {{ margin-top: 14px; font-family: 'Source Sans', 'Source Sans 3', sans-serif; color: {INK}; }}
.uvt-top h4 {{ margin: 0 0 8px; font-size: 17px; font-weight: 700; }}
.uvt-top-list {{ margin: 0; padding: 0 0 0 1.6rem; display: grid; gap: 6px; }}
.uvt-top-list li {{ padding-left: 4px; }}
.uvt-top-list li::marker {{ color: {INK_MUTED}; font-weight: 700; }}
.uvt-top-item {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; width: 100%; text-align: left;
                 background: #FFFFFF; border: 1px solid {LINE}; border-radius: 8px; padding: 8px 12px; font: inherit;
                 color: {INK}; cursor: pointer; }}
.uvt-top-item:hover {{ border-color: {ACTION}; }}
.uvt-top-item:focus-visible {{ outline: 2px solid {ACTION}; outline-offset: 2px; }}
.uvt-top-chip {{ flex: none; width: 22px; height: 6px; border-radius: 3px; align-self: center; }}
.uvt-top-name {{ font-size: 15px; font-weight: 700; }}
.uvt-top-fig {{ font-size: 14px; }}
.uvt-top-lines {{ flex-basis: 100%; color: {INK_MUTED}; font-size: 13px; padding-left: 32px; }}
.uvt-top-note, .uvt-top-empty {{ color: {INK_MUTED}; font-size: 13px; margin: 8px 0 0; max-width: 72ch; }}
.uvt-bar b {{ font-weight: 600; }}
.uvt-bar .sh {{ display: none; }}
@media (max-width: 640px) {{
  .uvt-head {{ max-width: calc(100% - 60px); top: 8px; left: 8px; padding: 8px 10px; }}
  .uvt-label {{ font-size: 17px; margin-bottom: 2px; }}
  .uvt-verdict {{ font-size: 14px; }}
  .uvt-sub {{ font-size: 11.5px; }}
  .uvt-bottom {{ left: 6px; right: 6px; bottom: 6px; padding: 8px 10px 7px; }}
  .uvt-controls {{ flex-wrap: wrap; gap: 6px 10px; }}
  .uvt-clock {{ min-width: 0; font-size: 18px; }}
  .uvt-range-box {{ order: 3; flex-basis: 100%; }}
  .uvt-allday {{ margin-left: auto; padding: 4px 10px; font-size: 13px; }}
  .uvt-play {{ padding: 4px 10px; font-size: 13px; }}
  .uvt-legend {{ gap: 4px 10px; margin-top: 6px; padding-top: 6px; font-size: 11.5px; }}
  .uvt-bar .lg {{ display: none; }}
  .uvt-bar .sh {{ display: inline; }}
  .uvt-key:not(.keep) {{ display: none; }}
  .uvt-panel {{ top: auto; bottom: 6px; left: 6px; right: 6px; width: auto; max-height: 64%; }}
}}
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


def in_scope(seg_from: str, seg_to: str, routes: list, communes: dict, scope: dict) -> bool:
    """Vrai si le tronçon appartient au périmètre du fil d'Ariane : commune (l'un des deux arrêts),
    ligne (parmi celles qui l'empruntent) et arrêt (l'une des deux extrémités)."""
    if scope.get("commune") and scope["commune"] not in (communes.get(seg_from), communes.get(seg_to)):
        return False
    if scope.get("route_id") and scope["route_id"] not in routes:
        return False
    if scope.get("stop_id") and scope["stop_id"] not in (seg_from, seg_to):
        return False
    return True


def tram_lines_payload(rows: pd.DataFrame, line_names: dict) -> dict:
    """Temps perdu de bout en bout du tram : route_id -> nom et, par terminus principal, 25 tranches
    [secondes perdues, durée prévue en secondes, trajets] ou None (`rows` : table troncons_tram d'un jour type)."""
    out: dict = {}
    for r in rows.sort_values(["route_id", "direction_id", "terminus"]).itertuples(index=False):
        line = out.setdefault(r.route_id, {"n": str(line_names.get(r.route_id, r.route_id)), "s": []})
        if not line["s"] or line["s"][-1]["vers"] != r.terminus:
            line["s"].append({"vers": r.terminus, "v": [None] * SLOTS})
        line["s"][-1]["v"][slot_index(r.heure)] = [int(round(r.perte_s)), int(round(r.prevu_s)), int(r.trajets)]
    return out


def troncons_payload(geo: pd.DataFrame, cells: pd.DataFrame, lines: dict, names: dict, communes: dict,
                     tram_paths: list, scope: dict | None = None, route_ids: dict | None = None,
                     measurable: set | None = None, tram_lines: dict | None = None,
                     date_info: dict | None = None) -> dict:
    """Données du composant : tronçons (tracé décalé, mode, codes des 25 tranches, valeurs mesurées,
    appartenance au périmètre), tracés du tram ({"r": route_id, "p": tracé}) et son temps perdu de
    bout en bout (`tram_lines`, voir `tram_lines_payload`), cadrage, tranche d'ouverture et textes.
    `date_info` : day, label (« mercredi 07/10/2026 »), reason (week-end, jour férié ou service faible),
    pour une date précise.

    `geo` : from_stop_id, to_stop_id, route_type, coords (liste) ; `cells` : heure, from_stop_id,
    to_stop_id, statut, palier, mediane_s, mediane_pct, passages, part_2min ; `lines` : (de, vers) ->
    noms des lignes ; `route_ids` : (de, vers) -> identifiants des lignes, dans le même ordre ;
    `measurable` : lignes dont le temps réel est fiable (les autres ne sont pas citées, `u` les
    signale) ; `communes` : stop_id -> commune ; `scope` : commune, route_id, stop_id, place
    (« à Mérignac »), label (nom affiché sur la carte), unmeasured (ligne choisie sans temps réel ou au
    temps réel non fiable : son tracé est gris).
    """
    scope = scope or {}
    route_ids = route_ids or {}
    by_seg: dict = {}
    for r in cells.itertuples(index=False):
        seg = by_seg.setdefault((r.from_stop_id, r.to_stop_id), {"codes": [ABSENT] * SLOTS, "values": {}})
        i = slot_index(r.heure)
        seg["codes"][i] = cell_code(r.statut, r.palier)
        if r.statut == "mesure":
            part = getattr(r, "part_2min", None)
            seg["values"][i] = [int(round(r.mediane_s)), int(round(r.mediane_pct)), int(r.passages),
                                int(round(part)) if part is not None and part == part else 0,
                                round(retained_seconds(r.mediane_s, r.mediane_pct), 1)]
        elif r.statut == "peu":
            seg["values"][i] = [None, None, int(r.passages), None]
    segments, lons, lats, all_lons, all_lats = [], [], [], [], []
    focused = any(scope.get(k) for k in ("commune", "route_id", "stop_id"))
    for g in geo.itertuples(index=False):
        seg = by_seg.get((g.from_stop_id, g.to_stop_id))
        if seg is None and date_info and g.route_type != TRAM_TYPE:
            seg = {"codes": [ABSENT] * SLOTS, "values": {}}
        if seg is None:
            continue
        routes = route_ids.get((g.from_stop_id, g.to_stop_id), [])
        inside = in_scope(g.from_stop_id, g.to_stop_id, routes, communes, scope)
        all_lons += [p[0] for p in g.coords]
        all_lats += [p[1] for p in g.coords]
        if focused and inside:
            ends = [(g.from_stop_id, g.coords[0]), (g.to_stop_id, g.coords[-1])]
            points = ([pt for sid, pt in ends if communes.get(sid) == scope["commune"]]
                      if scope.get("commune") and not (scope.get("route_id") or scope.get("stop_id")) else g.coords)
            lons += [pt[0] for pt in points]
            lats += [pt[1] for pt in points]
        names_here = lines.get((g.from_stop_id, g.to_stop_id), [])
        if isinstance(names_here, str):
            names_here = [n.strip() for n in names_here.split(",") if n.strip()]
        shown = [n for n, r in zip(names_here, routes or [None] * len(names_here))
                 if measurable is None or r in measurable]
        segments.append({
            "p": offset_right(g.coords), "m": int(g.route_type), "t": "".join(seg["codes"]), "v": seg["values"],
            "n": f"{names.get(g.from_stop_id, g.from_stop_id)} vers {names.get(g.to_stop_id, g.to_stop_id)}",
            "l": ", ".join(dict.fromkeys(shown)), "u": 1 if len(set(shown)) < len(set(names_here)) else 0,
            "c": 1 if inside else 0,
        })
    tram_lines = tram_lines or {}
    tram_line = scope.get("route_id") if scope.get("route_id") in {p["r"] for p in tram_paths} else None
    trams = []
    for path in tram_paths:
        inside = not focused or (tram_line is not None and path["r"] == tram_line)
        trams.append({"r": path["r"], "p": path["p"], "c": 1 if inside else 0})
        if tram_line is not None and inside:
            lons += [pt[0] for pt in path["p"]]
            lats += [pt[1] for pt in path["p"]]
    view = dict(NETWORK_VIEW)
    box_lons, box_lats = (lons, lats) if lons else (all_lons, all_lats)
    bounds = None
    if box_lons:
        lo_x, hi_x = _trimmed(box_lons, 0.02 if focused else 0.01)
        lo_y, hi_y = _trimmed(box_lats, 0.02 if focused else 0.01)
        bounds = [[lo_x, lo_y], [hi_x, hi_y]]
        view = {"longitude": (lo_x + hi_x) / 2, "latitude": (lo_y + hi_y) / 2,
                "zoom": COMMUNE_ZOOM if lons else NETWORK_VIEW["zoom"]}
    default = worst_slot([s["t"] for s in segments if s["c"]])
    return {"segments": segments, "tram": trams, "tram_lines": tram_lines, "tram_line": tram_line,
            "slot": WHOLE_DAY_SLOT if default is None or tram_line else default,
            "view": view, "bounds": bounds,
            "view_key": "|".join(str(scope.get(k) or "") for k in ("commune", "route_id", "stop_id")) or "__reseau__",
            "place": scope.get("place") or "sur le réseau", "label": scope.get("label") or "", "focused": focused,
            "unmeasured": bool(scope.get("unmeasured")), "date": date_info,
            "mode": date_info["day"] if date_info else "type",
            "colors": COLORS, "scale": SCALE_PAYLOAD, "height": MAP_HEIGHT}


def carte_troncons(payload: dict, key: str) -> None:
    """Affiche la carte des tronçons (réenregistre le composant s'il a été importé hors du serveur)."""
    global _carte
    try:
        _carte(key=key, data=payload, height="content")
    except ValueError as error:
        if "is not registered" not in str(error):
            raise
        _carte = _register()
        _carte(key=key, data=payload, height="content")
