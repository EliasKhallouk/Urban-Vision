import json
import sqlite3
from datetime import datetime


def _epoch_local(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


def _seed_observations(conn):
    """4 passages SCHEDULED + 1 arrêt sauté sur la route A, journée 2026-09-11.

    Route A, jour-service 20260911 :
      seq 1 | s1 | délai +10  (08:00 local)
      seq 2 | s1 | délai +20  (08:05)
      seq 3 | s2 | délai +300 (08:10)
      seq 4 | s1 | délai +400 (08:15)
      seq 5 | s1 | SKIPPED             (pas de departure_time)
    """
    rows = [
        ("t1", "20260911", "A", 0, 1, "s1", "SCHEDULED", None, 10, _epoch_local(2026, 9, 11, 8, 0), 0),
        ("t1", "20260911", "A", 0, 2, "s1", "SCHEDULED", 20, 20, _epoch_local(2026, 9, 11, 8, 5), 0),
        ("t1", "20260911", "A", 0, 3, "s2", "SCHEDULED", 300, 300, _epoch_local(2026, 9, 11, 8, 10), 0),
        ("t2", "20260911", "A", 0, 4, "s1", "SCHEDULED", 400, 400, _epoch_local(2026, 9, 11, 8, 15), 0),
        ("t2", "20260911", "A", 0, 5, "s1", "SKIPPED", None, None, None, 0),
    ]
    conn.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


class TestRefreshAgrees:
    def test_agregat_daily_exact(self, conn):
        import db as dbio

        _seed_observations(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])

        r = conn.execute(
            "SELECT obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60, "
            "skipped, eligible, histogram FROM agg_daily "
            "WHERE date_service = '2026-09-11' AND route_id = 'A'"
        ).fetchone()
        assert r is not None
        obs, sum_delay, le300, gt300, lt60, skipped, eligible, hist = r
        assert obs == 4
        assert sum_delay == 730
        assert le300 == 3
        assert gt300 == 1
        assert lt60 == 0
        assert skipped == 1
        assert eligible == 5
        assert json.loads(hist) == {"10": 1, "20": 1, "300": 1, "400": 1}

    def test_agregat_hourly_exact(self, conn):
        import db as dbio

        _seed_observations(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])

        r = conn.execute(
            "SELECT heure, obs, sum_delay, cnt_le300, cnt_gt300 FROM agg_hourly "
            "WHERE date_service = '2026-09-11' AND route_id = 'A'"
        ).fetchone()
        assert r == (8, 4, 730, 3, 1)

    def test_agregats_par_arret_exacts(self, conn):
        import db as dbio

        _seed_observations(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])

        s1 = conn.execute(
            "SELECT obs, sum_delay, cnt_le300, cnt_gt300, skipped, eligible, histogram "
            "FROM agg_daily_stop WHERE date_service = '2026-09-11' "
            "AND route_id = 'A' AND stop_id = 's1'"
        ).fetchone()
        assert s1[0] == 3        # délais 10, 20, 400
        assert s1[1] == 430
        assert s1[2] == 2        # ≤ 300 : 10 et 20
        assert s1[3] == 1        # > 300 : 400
        assert s1[4] == 1        # skipped
        assert s1[5] == 4        # eligible : 3 sched + 1 skipped
        assert json.loads(s1[6]) == {"10": 1, "20": 1, "400": 1}

        s2 = conn.execute(
            "SELECT obs, sum_delay, skipped, eligible FROM agg_daily_stop "
            "WHERE date_service = '2026-09-11' AND route_id = 'A' AND stop_id = 's2'"
        ).fetchone()
        assert s2 == (1, 300, 0, 1)

        h1 = conn.execute(
            "SELECT heure, obs, sum_delay FROM agg_hourly_stop "
            "WHERE date_service = '2026-09-11' AND route_id = 'A' AND stop_id = 's1'"
        ).fetchone()
        assert h1 == (8, 3, 430)

    def test_refresh_incremental_idempotent(self, conn, db_path):
        import db as dbio

        _seed_observations(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        dbio.refresh_aggregates(conn, days=["2026-09-11"])

        n_daily = conn.execute(
            "SELECT COUNT(*) FROM agg_daily WHERE date_service = '2026-09-11'"
        ).fetchone()[0]
        assert n_daily == 1  # INSERT OR REPLACE : pas de doublon

    def test_refresh_complet_days_none_equivalent(self, conn):
        import db as dbio

        _seed_observations(conn)
        dbio.refresh_aggregates(conn, days=None)

        r = conn.execute(
            "SELECT obs, sum_delay, skipped FROM agg_daily "
            "WHERE date_service = '2026-09-11' AND route_id = 'A'"
        ).fetchone()
        assert r == (4, 730, 1)