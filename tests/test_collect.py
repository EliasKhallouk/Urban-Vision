import sqlite3
import threading
import time

import pytest

from gtfs_factory import trip_update_feed


def _simple_feed(departure_delay=30):
    return trip_update_feed(
        [
            {
                "id": "e1",
                "trip_id": "T1",
                "start_date": "20260911",
                "route_id": "A",
                "direction_id": 0,
                "stop_times": [
                    {"seq": 1, "stop_id": "s1", "departure_delay": 0, "departure_time": 100000},
                    {
                        "seq": 2,
                        "stop_id": "s2",
                        "arrival_delay": departure_delay,
                        "departure_delay": departure_delay,
                        "departure_time": 100120,
                    },
                ],
            }
        ]
    )


class TestProcessFeed:
    def test_upsert_observations_et_trip_status(self, conn):
        import collect as coll

        coll.process_feed(conn, _simple_feed())

        rows = conn.execute(
            "SELECT trip_id, route_id, stop_sequence, stop_id, schedule_relationship, "
            "arrival_delay, departure_delay, departure_time "
            "FROM observations ORDER BY stop_sequence"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][:4] == ("T1", "A", 1, "s1")
        assert rows[0][4] == "SCHEDULED"
        assert rows[0][5] is None  # seq 1 : arrival_delay volontairement ignoré
        assert rows[0][6] == 0
        assert rows[1][5] == 30  # seq 2 : arrival conservé
        assert rows[1][6] == 30

        status = conn.execute(
            "SELECT trip_id, route_id, schedule_relationship FROM trip_status"
        ).fetchall()
        assert status == [("T1", "A", "SCHEDULED")]

    def test_upsert_est_idempotent_derniere_valeur_gagne(self, conn):
        import collect as coll

        coll.process_feed(conn, _simple_feed(departure_delay=30))
        coll.process_feed(conn, _simple_feed(departure_delay=45))

        n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert n == 2  # pas de doublon

        delay = conn.execute(
            "SELECT departure_delay FROM observations WHERE stop_sequence = 2"
        ).fetchone()[0]
        assert delay == 45

    def test_stop_saute_enregistre_SKIPPED(self, conn):
        import collect as coll

        feed = trip_update_feed(
            [
                {
                    "id": "e1",
                    "trip_id": "T2",
                    "start_date": "20260911",
                    "route_id": "A",
                    "stop_times": [
                        {"seq": 2, "stop_id": "s9",
                         "schedule_relationship": "skipped",
                         "departure_time": 100120},
                    ],
                }
            ]
        )
        coll.process_feed(conn, feed)
        rel = conn.execute(
            "SELECT schedule_relationship FROM observations WHERE stop_id = 's9'"
        ).fetchone()[0]
        assert rel == "SKIPPED"


class TestGapDetection:
    def test_trou_au_dela_du_seuil_enregistre(self, conn, db_path):
        import collect as coll

        now = 1_000_000
        coll.record_gap_if_any(conn, last_success_ts=now - 190, now=now)
        gaps = conn.execute("SELECT gap_start, gap_end FROM collection_gaps").fetchall()
        assert gaps == [(now - 190, now)]

    def test_pas_de_trou_sous_le_seuil(self, conn):
        import collect as coll

        coll.record_gap_if_any(conn, last_success_ts=999_900, now=1_000_000)
        n = conn.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0]
        assert n == 0

    def test_pas_de_trou_sans_dernier_succes(self, conn):
        import collect as coll

        coll.record_gap_if_any(conn, last_success_ts=None, now=1_000_000)
        n = conn.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0]
        assert n == 0


class TestConfig:
    def test_collecteur_configure_busy_timeout(self, db_path):
        import collect as coll

        assert coll.DB_BUSY_TIMEOUT_MS == 120_000
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute(f"PRAGMA busy_timeout = {coll.DB_BUSY_TIMEOUT_MS};")
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 120_000
        c.close()


class TestBouclePrincipale:
    def test_acquisition_continue_et_refresh_throttle(self, monkeypatch, db_path):
        import db as dbio
        import collect as coll

        c = sqlite3.connect(str(db_path))
        dbio.init_db(c)
        c.close()

        feed = _simple_feed()

        class NullLogger:
            def debug(self, *a, **k):
                pass

            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                pass

            def exception(self, *a, **k):
                pass

        fetches = {"n": 0}

        monkeypatch.setattr(coll, "DB_PATH", str(db_path))
        monkeypatch.setattr(coll, "POLL_INTERVAL_SECONDS", 1)
        monkeypatch.setattr(coll, "AGGREGATE_REFRESH_INTERVAL_SECONDS", 2)
        monkeypatch.setattr(coll, "fetch_feed", lambda: (fetches.__setitem__("n", fetches["n"] + 1) or feed))
        monkeypatch.setattr(coll, "logger", NullLogger())

        refresh_times = []
        real_refresh = dbio.refresh_aggregates

        def counting_refresh(conn, days=None):
            refresh_times.append(time.monotonic())
            return real_refresh(conn, days=days)

        monkeypatch.setattr(dbio, "refresh_aggregates", counting_refresh)

        t = threading.Thread(target=coll.main, daemon=True)
        t.start()
        time.sleep(2.6)
        t.join(timeout=1)

        assert fetches["n"] >= 2, "la collecte doit tourner en continu"

        cc = sqlite3.connect(str(db_path))
        obs = cc.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        cc.close()
        assert obs == 2  # les upserts sont idempotents

        assert 1 <= len(refresh_times) <= 3, "refresh trop fréquent"
        if len(refresh_times) >= 2:
            assert refresh_times[1] - refresh_times[0] >= 1.8, (
                "le refresh doit être espacé d'au moins l'intervalle configuré"
            )