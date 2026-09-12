"""Tests du chargement du GTFS statique (routes/stops) sur un zip synthétique."""

import io
import zipfile

import pytest

import gtfs_static

SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]

ROUTES_CSV = (
    "route_id,route_short_name,route_long_name,route_type\n"
    "lineA,1,Ligne circulaire,3\n"
    "tramC,,Tramway ligne C,0\n"
)
STOPS_CSV = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "s1,Arret Central,44.84,-0.57\n"
    "s2,,44.85,-0.58\n"
)


def _make_zip(routes_content=ROUTES_CSV, stops_content=STOPS_CSV):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("routes.txt", routes_content)
        archive.writestr("stops.txt", stops_content)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


class TestLoadRoutes:
    def test_charge_et_compte(self, conn):
        gtfs_static.create_static_tables(conn)
        assert gtfs_static.load_routes(conn, _make_zip()) == 2
        rows = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes ORDER BY route_id"
        ).fetchall()
        assert rows == [
            ("lineA", "1", "Ligne circulaire", 3),
            ("tramC", "", "Tramway ligne C", 0),
        ]

    def test_upsert_ecrase_les_valeurs_existantes(self, conn):
        gtfs_static.create_static_tables(conn)
        gtfs_static.load_routes(conn, _make_zip())
        gtfs_static.load_routes(conn, _make_zip(routes_content=(
            "route_id,route_short_name,route_type\n"
            "lineA,99,2\n"  # sans route_long_name : champ absent -> None
        )))
        row = conn.execute(
            "SELECT route_short_name, route_long_name, route_type FROM routes "
            "WHERE route_id = 'lineA'"
        ).fetchone()
        assert row == ("99", None, 2)
        assert conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 2


class TestLoadStops:
    def test_charge_et_compte(self, conn):
        gtfs_static.create_static_tables(conn)
        assert gtfs_static.load_stops(conn, _make_zip()) == 2
        rows = conn.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops ORDER BY stop_id"
        ).fetchall()
        assert rows == [
            ("s1", "Arret Central", 44.84, -0.57),
            ("s2", "", 44.85, -0.58),
        ]

    def test_upsert_idempotent(self, conn):
        gtfs_static.create_static_tables(conn)
        gtfs_static.load_stops(conn, _make_zip())
        gtfs_static.load_stops(conn, _make_zip(stops_content=(
            "stop_id,stop_name,stop_lat,stop_lon\ns1,Arret Renaissance,44.84,-0.57\n"
        )))
        assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 2
        name = conn.execute(
            "SELECT stop_name FROM stops WHERE stop_id = 's1'"
        ).fetchone()[0]
        assert name == "Arret Renaissance"


class TestDownload:
    def test_telecharge_et_ouvre_le_zip(self, monkeypatch):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("routes.txt", ROUTES_CSV)
        payload = buffer.getvalue()

        class FakeResponse:
            content = payload

            def raise_for_status(self):
                pass

        monkeypatch.setattr(gtfs_static.requests, "get", lambda url, timeout: FakeResponse())
        archive = gtfs_static.download_gtfs_zip("https://example.invalid/gtfs.zip")
        assert "routes.txt" in archive.namelist()

    def test_erreur_http_propagee(self, monkeypatch):
        import requests

        class Boom:
            def raise_for_status(self):
                raise requests.HTTPError("404 Not Found")

        monkeypatch.setattr(gtfs_static.requests, "get", lambda url, timeout: Boom())
        with pytest.raises(requests.HTTPError):
            gtfs_static.download_gtfs_zip("https://example.invalid/gtfs.zip")