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
    def test_collecte_journalisee_sans_recalcul_des_agregats(self, monkeypatch, db_path):
        import db as dbio
        import collect as coll
        import requests

        c = sqlite3.connect(str(db_path))
        dbio.init_db(c)
        c.close()

        feed = _simple_feed()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("réseau coupé")
            return feed

        class NullLogger:
            def debug(self, *a, **k):
                pass

            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                pass

            def exception(self, *a, **k):
                pass

        monkeypatch.setattr(coll, "DB_PATH", str(db_path))
        monkeypatch.setattr(coll, "POLL_INTERVAL_SECONDS", 1)
        monkeypatch.setattr(coll, "fetch_feed", fetch)
        monkeypatch.setattr(coll, "logger", NullLogger())
        monkeypatch.setattr(dbio, "refresh_aggregates", lambda *a, **k: pytest.fail("le collecteur ne doit plus recalculer"))

        t = threading.Thread(target=coll.main, daemon=True)
        t.start()
        time.sleep(2.6)
        t.join(timeout=1)

        cc = sqlite3.connect(str(db_path))
        runs = cc.execute(
            "SELECT feed_ts, entities, rows_written, error FROM collection_runs ORDER BY started_at"
        ).fetchall()
        obs = cc.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        cc.close()
        assert calls["n"] >= 2, "la collecte doit tourner en continu"
        assert runs[0][3].startswith("Flux indisponible : réseau coupé")
        assert runs[1][:3] == (feed.header.timestamp, 1, 2)
        assert runs[1][3] is None
        assert obs == 2


def _prediction_feed(feed_ts, departure_time):
    return trip_update_feed(
        [{
            "id": "e1", "trip_id": "T9", "start_date": "20260911", "route_id": "A",
            "stop_times": [{"seq": 3, "stop_id": "s3", "departure_delay": 0, "departure_time": departure_time}],
        }],
        timestamp=feed_ts,
    )


def _predictions(conn):
    return conn.execute(
        "SELECT pred_dep_10, pred_dep_5, pred_dep_2, departure_time FROM observations WHERE trip_id = 'T9'"
    ).fetchone()


class TestTrajectoireDesPrevisions:
    def test_prevision_retenue_au_franchissement_de_chaque_horizon(self, conn):
        import collect as coll

        t0 = 1_000_000
        planned = t0 + 1200
        coll.process_feed(conn, _prediction_feed(t0, planned))
        assert _predictions(conn)[:3] == (None, None, None)
        coll.process_feed(conn, _prediction_feed(t0 + 660, planned + 30))
        assert _predictions(conn)[:3] == (planned + 30, None, None)
        coll.process_feed(conn, _prediction_feed(t0 + 960, planned + 60))
        assert _predictions(conn)[:3] == (planned + 30, planned + 60, None)
        coll.process_feed(conn, _prediction_feed(t0 + 1200, planned + 90))
        assert _predictions(conn)[:3] == (planned + 30, planned + 60, planned + 90)
        coll.process_feed(conn, _prediction_feed(t0 + 1400, planned + 95))
        assert _predictions(conn) == (planned + 30, planned + 60, planned + 90, planned + 95)

    def test_seuls_les_horizons_franchis_sont_retenus(self, conn):
        import collect as coll

        t0 = 1_000_000
        coll.process_feed(conn, _prediction_feed(t0, t0 + 240))
        coll.process_feed(conn, _prediction_feed(t0 + 60, t0 + 250))
        coll.process_feed(conn, _prediction_feed(t0 + 180, t0 + 250))
        assert _predictions(conn)[:3] == (None, None, t0 + 250)

    def test_arret_saute_sans_heure_de_depart(self, conn):
        import collect as coll

        feed = trip_update_feed([{
            "id": "e1", "trip_id": "T9", "start_date": "20260911", "route_id": "A",
            "stop_times": [{"seq": 3, "stop_id": "s3", "schedule_relationship": "skipped"}],
        }], timestamp=1_000_000)
        coll.process_feed(conn, feed)
        coll.process_feed(conn, feed)
        assert _predictions(conn) == (None, None, None, None)


class TestJournalDeCollecte:
    def test_dernier_succes_lu_dans_le_journal(self, conn):
        import collect as coll

        coll.record_run(conn, 1000, {"feed_ts": 999, "entities": 10, "rows_written": 50})
        coll.record_run(conn, 2000, {"error": "Flux indisponible : 502"})
        assert coll.get_last_known_success(conn) == 1000.0

    def test_sans_journal_repli_sur_les_observations(self, conn):
        import collect as coll

        coll.process_feed(conn, _simple_feed())
        assert coll.get_last_known_success(conn) == 1789219000.0


class TestMigrationDuSchema:
    def test_colonnes_de_prevision_ajoutees_a_une_base_existante(self, db_path):
        import db as dbio

        c = sqlite3.connect(str(db_path))
        c.execute(
            "CREATE TABLE observations (trip_id TEXT NOT NULL, start_date TEXT NOT NULL, "
            "route_id TEXT NOT NULL, direction_id INTEGER, stop_sequence INTEGER NOT NULL, "
            "stop_id TEXT NOT NULL, schedule_relationship TEXT, arrival_delay INTEGER, "
            "departure_delay INTEGER, last_seen_at INTEGER NOT NULL, "
            "PRIMARY KEY (trip_id, start_date, stop_sequence))"
        )
        c.execute("INSERT INTO observations VALUES ('t', '20260911', 'A', 0, 1, 's', 'SCHEDULED', NULL, 30, 5)")
        c.commit()
        dbio.init_db(c)
        columns = [row[1] for row in c.execute("PRAGMA table_info(observations)")]
        assert columns[-4:] == ["departure_time", "pred_dep_10", "pred_dep_5", "pred_dep_2"]
        assert c.execute("SELECT departure_delay FROM observations").fetchone()[0] == 30
        assert "collection_runs" in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        c.close()
