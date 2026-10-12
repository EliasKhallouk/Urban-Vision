"""Tests de la carte des arrêts (dashboard/carte.py) : icônes et données envoyées au composant."""

import base64
import io

import pandas as pd
from PIL import Image

import carte
import app as app_mod


def _stops():
    return pd.DataFrame({
        "stop_id": ["s1", "s2", "s3"],
        "stop_name": ["Blancherie", "Blancherie", "Gare"],
        "direction": ["vers Beaudésert", "vers Blancherie", ""],
        "lat": [44.8400, 44.8402, 44.8500], "lon": [-0.5300, -0.5301, -0.5500],
        "route_type": [3, 3, 0], "score_fiabilite": [78.3, 67.9, 95.0],
        "pct_retard_5min": [13.7, 22.6, 2.0], "observations": [1908, 30, 225],
        "lignes": ["27", "27, 28", "A"],
    })


class TestIcones:
    def test_atlas_formes_par_palier_et_halo(self):
        uri, mapping = carte.marker_atlas()
        assert uri.startswith("data:image/png;base64,")
        levels = carte.score_levels()
        assert levels[0] == 80.0 and levels[-1] == 35.0 and len(levels) == 19
        assert len(mapping) == 4 * 19 + 1
        img = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
        assert img.size == (4 * carte.MARKER_CELL, 20 * carte.MARKER_CELL)
        cell = mapping["square|#A55F73"]
        assert img.getpixel((cell["x"] + cell["width"] // 2, cell["y"] + cell["height"] // 2))[:3] == (0xA5, 0x5F, 0x73)
        halo = mapping["halo"]
        center = img.getpixel((halo["x"] + halo["width"] // 2, halo["y"] + halo["height"] // 2))
        assert all(abs(a - b) <= 2 for a, b in zip(center[:3], (0xF6, 0xF1, 0xEA)))

    def test_cle_d_icone(self):
        assert carte.icon_key(3, 90.0) == "square|#7FAAA6"
        assert carte.icon_key(0, 60.0) == "circle|#E1C17E"
        assert carte.icon_key(11, 10.0) == "diamond|#602D24"
        assert carte.icon_key(3, 61.1) == carte.icon_key(3, 60.0)
        for score in (4.0, 30.0, 47.3, 66.6, 79.9, 100.0):
            assert carte.icon_key(4, score) in carte.marker_atlas()[1]


class TestDonneesDeLaCarte:
    def _payload(self, selected=None, commune=None):
        stops = _stops()
        return carte.map_payload(stops, app_mod.group_stops(stops), selected, commune)

    def test_quais_ordonnes_pour_dessiner_les_moins_fiables_au_dessus(self):
        p = self._payload()
        assert [s["i"] for s in p["stops"]] == ["s3", "s1", "s2"]
        assert [g["w"] for g in p["groups"]] == ["s3", "s2"]

    def test_quais_regroupes_et_leur_groupe(self):
        p = self._payload()
        blanch = next(g for g in p["groups"] if g["w"] == "s2")
        assert sorted(p["stops"][i]["i"] for i in blanch["m"]) == ["s1", "s2"]
        assert blanch["k"] == f"square|{carte.score_color(67.5)}"
        assert all(p["groups"][s["g"]]["w"] in ("s2", "s3") for s in p["stops"])
        assert p["stops"][2]["d"] == "vers Blancherie"

    def test_taille_constante(self):
        p = self._payload()
        assert {s["z"] for s in p["stops"]} | {g["z"] for g in p["groups"]} == {carte.STOP_SIZE_METERS}

    def test_selection_et_vue(self):
        assert self._payload("s1")["selected"] == "s1"
        assert self._payload("inconnu")["selected"] is None
        p = self._payload(commune="Mérignac")
        assert p["view_key"] == "Mérignac"
        assert p["view"]["zoom"] == carte.COMMUNE_ZOOM
        assert self._payload()["view"] == carte.NETWORK_VIEW
        assert p["split_zoom"] == carte.SPLIT_ZOOM and p["size_px"] == list(carte.STOP_SIZE_PIXELS)

    def test_donnees_serialisables(self):
        import json

        json.dumps(self._payload("s2", "Mérignac"))
