"""Tests des tracés de lignes (route_shapes.py) et de leur affichage sur la carte."""

import io
import json
import sqlite3
import zipfile

import pandas as pd
import pytest

import archive_gtfs as ag
import route_shapes as rs


def _gtfs():
    trips = ["route_id,service_id,trip_id,direction_id,shape_id",
             "A,s,t1,0,10", "A,s,t2,0,10", "A,s,t3,0,11", "A,s,t4,1,20", "B,s,t5,0,30", "B,s,t6,0,"]
    shapes = ["shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence"]
    for i, lon in enumerate([-0.60, -0.59, -0.58, -0.57]):
        shapes.append(f"10,44.840000,{lon:.6f},{3 - i}")
    shapes += ["11,44.85,-0.60,0", "11,44.85,-0.50,1", "20,44.84,-0.57,0", "20,44.84,-0.60,1",
               "30,44.80,-0.55,0", "30,44.81,-0.55,1"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("trips.txt", "\n".join(trips) + "\n")
        z.writestr("shapes.txt", "\n".join(shapes) + "\n")
        for name in ("stop_times.txt", "routes.txt", "stops.txt"):
            z.writestr(name, "id\n1\n")
    return buffer.getvalue()


class TestTraces:
    def test_trace_le_plus_emprunte_par_sens(self):
        shapes = {(s["route_id"], s["direction_id"]): s for s in rs.build_shapes(_gtfs())}
        assert set(shapes) == {("A", 0), ("A", 1), ("B", 0)}
        assert shapes[("A", 0)]["shape_id"] == "10"
        assert shapes[("A", 0)]["trips"] == 2

    def test_points_dans_l_ordre_et_simplifies(self):
        a0 = next(s for s in rs.build_shapes(_gtfs()) if (s["route_id"], s["direction_id"]) == ("A", 0))
        assert a0["coords"] == [[-0.57, 44.84], [-0.6, 44.84]]

    def test_simplification_garde_les_virages(self):
        corner = [(-0.60, 44.84), (-0.59, 44.84), (-0.59, 44.85)]
        assert rs.simplify(corner) == corner
        straight = [(-0.60, 44.84), (-0.5950, 44.840001), (-0.59, 44.84)]
        assert rs.simplify(straight) == [straight[0], straight[-1]]

    def test_chargement_en_base(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "t.db")
        assert rs.refresh_route_shapes(conn, _gtfs()) == 3
        assert rs.refresh_route_shapes(conn, _gtfs()) == 3
        rows = conn.execute("SELECT route_id, direction_id, coords FROM route_shapes ORDER BY 1, 2").fetchall()
        assert [(r, d) for r, d, _ in rows] == [("A", 0), ("A", 1), ("B", 0)]
        assert json.loads(rows[0][2])[0] == [-0.57, 44.84]

    def test_archive_met_a_jour_les_traces(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag, "download", _gtfs)
        db = tmp_path / "t.db"
        assert ag.main(["--dest", str(tmp_path / "arch"), "--db", str(db)]) == 0
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM route_shapes").fetchone()[0] == 3

    def test_derniere_archive(self, tmp_path):
        assert rs.latest_archive(tmp_path) is None
        (tmp_path / "index.json").write_text(json.dumps({"versions": [{"file": "a.zip"}, {"file": "b.zip"}]}))
        assert rs.latest_archive(tmp_path) == tmp_path / "b.zip"


class TestCarte:
    def test_traces_de_l_arret_selon_le_sens_et_l_etat(self):
        import app as app_mod

        lines = pd.DataFrame({"route_id": ["A", "B"], "ligne": ["1", "2"], "route_type": [0, 3],
                              "score_fiabilite": [40.0, 90.0]})
        shapes = {"A": {0: [[0, 0], [1, 1]], 1: [[1, 1], [0, 0]]}, "B": {0: [[2, 2], [3, 3]], 1: [[3, 3], [2, 2]]}}
        paths = app_mod.stop_route_paths(lines, shapes, {"A": 1})
        assert [(p["r"], p["p"][0]) for p in paths] == [("B", [2, 2]), ("B", [3, 3]), ("A", [1, 1])]
        a = paths[-1]
        assert (a["l"], a["e"], a["g"]) == ("1", "problématique", "●")
        assert a["c"] == [0xBC, 0x6C, 0x25]
        assert a["t"] == [255, 255, 255]

    def test_ligne_sans_trace_ignoree(self):
        import app as app_mod

        lines = pd.DataFrame({"route_id": ["Z"], "ligne": ["Z"], "route_type": [3], "score_fiabilite": [70.0]})
        assert app_mod.stop_route_paths(lines, {}, {}) == []

    def test_traces_transmis_au_composant(self):
        import carte

        stops = pd.DataFrame({"stop_id": ["s1"], "stop_name": ["Arrêt"], "direction": [""], "lat": [44.8],
                              "lon": [-0.5], "route_type": [3], "score_fiabilite": [80.0], "pct_retard_5min": [5.0],
                              "observations": [100], "lignes": ["1"]})
        groups = pd.DataFrame({"members": [["s1"]], "stop_id": ["s1"], "lat": [44.8], "lon": [-0.5],
                               "route_type": [3], "score_fiabilite": [80.0], "observations": [100]})
        payload = carte.map_payload(stops, groups, "s1", None, paths=[{"r": "1"}])
        assert payload["paths"] == [{"r": "1"}]
        assert carte.map_payload(stops, groups, None, None)["paths"] == []


@pytest.fixture(autouse=True)
def _no_streamlit_cache():
    import streamlit as st

    st.cache_data.clear()
    yield
