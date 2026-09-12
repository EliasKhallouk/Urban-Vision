"""Tests du rattachement des arrêts aux communes (jointure spatiale spatiale)."""

import io
import json

import pytest

from assign_stop_municipalities import (
    assign_stops,
    export_unassigned,
    initialize_tables,
    load_boundaries,
)
import gtfs_static

SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
HOLE = [[3.0, 3.0], [7.0, 3.0], [7.0, 7.0], [3.0, 7.0], [3.0, 3.0]]

BOUNDARIES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"insee": "33001", "nom": "Commune Test"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [SQUARE, HOLE],
            },
        }
    ],
}


def _seed_stops(conn):
    """s1 dans le polygone, s2 dans le trou, s3 hors de toute frontière."""
    initialize_tables(conn)  # needed by assign_stops before DELETE/INSERT
    gtfs_static.create_static_tables(conn)
    conn.executemany(
        "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES (?, ?, ?, ?)",
        [
            ("s1", "Arret Intra", 1.0, 1.0),
            ("s2", "Arret DansTrou", 5.0, 5.0),
            ("s3", "Arret Lointain", 50.0, 50.0),
        ],
    )
    conn.commit()


class TestInitializeTables:
    def test_creer_les_tables_et_l_index(self, conn):
        initialize_tables(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"municipalities", "stop_municipalities"} <= tables
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_stop_municipalities_commune'"
        ).fetchone()
        assert index is not None

    def test_idempotent(self, conn):
        initialize_tables(conn)
        initialize_tables(conn)  # ne doit pas lever


class TestAssignStops:
    def test_rattachement_point_in_polygon_et_trou_exclu(self, conn):
        _seed_stops(conn)
        unassigned = assign_stops(conn, BOUNDARIES, "source-de-test", resolve_outside=False)

        assert [stop[0] for stop in unassigned] == ["s2", "s3"]

        rows = conn.execute(
            "SELECT stop_id, insee_code, commune_name, assignment_method "
            "FROM stop_municipalities ORDER BY stop_id"
        ).fetchall()
        assert rows == [("s1", "33001", "Commune Test", "point-in-polygon")]

        commune = conn.execute(
            "SELECT insee_code, commune_name, boundary_source FROM municipalities"
        ).fetchone()
        assert commune == ("33001", "Commune Test", "source-de-test")

    def test_idempotent_pas_de_doublon(self, conn):
        _seed_stops(conn)
        assign_stops(conn, BOUNDARIES, "source", resolve_outside=False)
        assign_stops(conn, BOUNDARIES, "source", resolve_outside=False)
        assert conn.execute("SELECT COUNT(*) FROM stop_municipalities").fetchone()[0] == 1

    def test_fallback_api_adresse_pour_les_arrêts_hors_metropole(self, conn, monkeypatch):
        _seed_stops(conn)
        monkeypatch.setattr(
            "assign_stop_municipalities.reverse_geocode_commune",
            lambda lat, lon: ("33056", "Cenon"),
        )
        # empêcher le sleep réel entre appels API
        import time

        monkeypatch.setattr(time, "sleep", lambda _: None)

        unassigned = assign_stops(conn, BOUNDARIES, "source", resolve_outside=True)
        # s3 est résolu par l'API ; s2 (dans le trou) aussi — plus rien non résolu.
        assert unassigned == []

        rows = conn.execute(
            "SELECT stop_id, insee_code, commune_name, assignment_method "
            "FROM stop_municipalities ORDER BY stop_id"
        ).fetchall()
        assert (rows[0][0], rows[0][3]) == ("s1", "point-in-polygon")
        assert (rows[-1][0], rows[-1][1], rows[-1][3]) == ("s3", "33056", "api-adresse-reverse")

        fallback = conn.execute(
            "SELECT commune_name FROM municipalities WHERE insee_code = '33056'"
        ).fetchone()
        assert fallback == ("Cenon",)

    def test_geometries_invalides_ignorees(self, conn):
        _seed_stops(conn)
        boundaries = {
            "type": "FeatureCollection",
            "features": [
                BOUNDARIES["features"][0],  # polygone valide conservé
                {"type": "Feature", "properties": {"insee": "33002", "nom": "Pts"},
                 "geometry": {"type": "Point", "coordinates": [1.0, 1.0]}},
                {"type": "Feature", "properties": {"insee": "33003", "nom": "SansGeo"},
                 "geometry": None},
            ],
        }
        unassigned = assign_stops(conn, boundaries, "source", resolve_outside=False)
        assert [stop[0] for stop in unassigned] == ["s2", "s3"]

    def test_sans_aucun_polygone_exploitable_leve(self, conn):
        _seed_stops(conn)
        with pytest.raises(ValueError, match="Aucun polygone"):
            assign_stops(conn, {"type": "FeatureCollection", "features": []},
                         "source", resolve_outside=False)


class TestExportUnassigned:
    def test_ecrit_le_csv_avec_motif(self, tmp_path):
        destination = tmp_path / "out" / "stops.csv"
        rows = [("s2", "Arret DansTrou", 5.0, 5.0)]
        export_unassigned(rows, destination)
        content = destination.read_text(encoding="utf-8")
        assert content.splitlines()[0].split(",") == [
            "stop_id", "stop_name", "stop_lat", "stop_lon", "reason"
        ]
        assert "commune_non_resolue" in content


class TestLoadBoundaries:
    def test_depuis_un_fichier(self, tmp_path):
        path = tmp_path / "contours.geojson"
        path.write_text(json.dumps(BOUNDARIES), encoding="utf-8")
        data, source = load_boundaries(path, "https://ign/url")
        assert data == BOUNDARIES
        assert str(source).endswith("contours.geojson")

    def test_depuis_une_url(self, monkeypatch):
        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self._it = iter(payload)

                def _read():
                    pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._payload

        import json as _json

        data_bytes = json.dumps(BOUNDARIES).encode("utf-8")

        class FakeConnection(FakeResponse):
            def __init__(self):
                super().__init__(data_bytes)
                self.closed = False

            def close(self):
                self.closed = True

        def fake_json(f):
            return _json.loads(f.read())

        fake_conn = FakeConnection()
        monkeypatch.setattr("assign_stop_municipalities.urlopen", lambda url, timeout: fake_conn)
        data, source = load_boundaries(None, "https://exemple.fr/contours.geojson")
        assert data == BOUNDARIES
        assert source == "https://exemple.fr/contours.geojson"