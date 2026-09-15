"""Tests du module d'export open data (src/scripts/export_open_data.py)."""

import csv
import json

import db as dbio
import export_open_data as eod
import gtfs_static
from assign_stop_municipalities import initialize_tables


def _seed(conn):
    conn.row_factory = __import__("sqlite3").Row
    gtfs_static.create_static_tables(conn)
    initialize_tables(conn)
    conn.executescript(dbio.STOP_DIRECTION_DDL)
    conn.execute(
        "INSERT INTO routes (route_id, route_short_name, route_type) VALUES ('A', '1', 3)"
    )
    conn.execute(
        "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) "
        "VALUES ('s1', 'Cours Gambetta', 44.84, -0.58)"
    )
    conn.execute(
        "INSERT INTO stop_municipalities (stop_id, insee_code, commune_name, "
        "assignment_method, assigned_at) VALUES ('s1', '33200', 'Lormont', 'point-in-polygon', 1)"
    )
    conn.execute(
        "INSERT INTO stop_direction (route_id, stop_id, direction_id, terminus) "
        "VALUES ('A', 's1', 0, 'Bordeaux centre')"
    )
    conn.executemany(
        """INSERT INTO agg_daily
           (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
            skipped, eligible, histogram)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("2026-09-10", "A", 4, 400, 3, 1, 0, 1, 5,
             '{"10":1,"20":1,"300":1,"400":1}'),
            ("2026-09-11", "A", 6, 600, 4, 2, 0, 0, 6,
             '{"10":1,"20":1,"300":1,"400":1,"600":1,"700":1}'),
        ],
    )
    conn.executemany(
        """INSERT INTO agg_daily_stop
           (date_service, route_id, stop_id, obs, sum_delay, cnt_le300, cnt_gt300,
            cnt_lt60, skipped, eligible, histogram)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("2026-09-10", "A", "s1", 4, 400, 3, 1, 0, 1, 5,
             '{"10":1,"20":1,"300":1,"400":1}'),
            ("2026-09-11", "A", "s1", 6, 600, 4, 2, 0, 0, 6,
             '{"10":1,"20":1,"300":1,"400":1,"600":1,"700":1}'),
        ],
    )
    conn.executemany(
        """INSERT INTO agg_hourly
           (date_service, route_id, heure, obs, sum_delay, cnt_le300, cnt_gt300)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            ("2026-09-10", "A", 8, 4, 400, 3, 1),
            ("2026-09-11", "A", 8, 6, 600, 4, 2),
        ],
    )
    conn.commit()


class TestDatasetRows:
    def test_lignes_avec_mediane_et_modes(self, conn):
        _seed(conn)
        rows = eod.dataset_rows(conn, "lignes_journalier", "2026-09-10", "2026-09-12")
        assert len(rows) == 2
        first = rows[0]
        assert first["ligne"] == "1"
        assert first["mode"] == "Bus"
        assert first["retard_moyen_s"] == 100.0
        assert first["retard_median_s"] == 160.0
        assert round(first["pct_a_l_heure"], 2) == 75.0
        assert round(first["pct_arrets_sautes"], 2) == 20.0

    def test_arrets_avec_commune_et_direction(self, conn):
        _seed(conn)
        rows = eod.dataset_rows(conn, "arrets_journalier")
        assert len(rows) == 2
        r = rows[0]
        assert r["stop_name"] == "Cours Gambetta"
        assert r["commune"] == "Lormont"
        assert r["direction"] == "vers Bordeaux centre"
        assert r["stop_lat"] == 44.84

    def test_horaire(self, conn):
        _seed(conn)
        rows = eod.dataset_rows(conn, "horaire")
        assert len(rows) == 2
        assert rows[0]["heure"] == 8
        assert rows[0]["retard_moyen_s"] == 100.0

    def test_communes(self, conn):
        _seed(conn)
        rows = eod.dataset_rows(conn, "communes_journalier")
        assert len(rows) == 2
        r = rows[0]
        assert r["commune"] == "Lormont"
        assert r["insee_code"] == "33200"
        assert r["n_lignes"] == 1

    def test_intervalles_demi_ouverts(self, conn):
        _seed(conn)
        rows = eod.dataset_rows(conn, "lignes_journalier", "2026-09-11", "2026-09-12")
        assert [r["date_service"] for r in rows] == ["2026-09-11"]

    def test_dataset_inconnu(self, conn):
        _seed(conn)
        try:
            eod.dataset_rows(conn, "inconnu")
            raise AssertionError("pas de ValueError")
        except ValueError:
            pass


class TestWriteDatasets:
    def test_ecrit_4_csv_et_metadata(self, conn, tmp_path):
        _seed(conn)
        out = tmp_path / "out"
        meta = eod.write_datasets(conn, out)
        names = sorted(p.name for p in out.iterdir())
        assert names == [
            "METADATA.json", "arrets_journalier.csv", "communes_journalier.csv",
            "horaire.csv", "lignes_journalier.csv",
        ]
        assert meta["row_counts"] == {
            "lignes_journalier": 2, "arrets_journalier": 2,
            "horaire": 2, "communes_journalier": 2,
        }
        with open(out / "lignes_journalier.csv", newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2
        assert rows[0]["route_id"] == "A"
        meta_json = json.loads((out / "METADATA.json").read_text(encoding="utf-8"))
        assert meta_json["row_counts"]["horaire"] == 2

    def test_cli_liste_les_datasets(self, capsys):
        code = eod.main(["--print-datasets"])
        assert code == 0
        assert capsys.readouterr().out.splitlines() == eod.DATASETS