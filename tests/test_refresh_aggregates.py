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
    seen = _epoch_local(2026, 9, 11, 8, 20)
    rows = [
        ("t1", "20260911", "A", 0, 1, "s1", "SCHEDULED", None, 10, _epoch_local(2026, 9, 11, 8, 0), seen),
        ("t1", "20260911", "A", 0, 2, "s1", "SCHEDULED", 20, 20, _epoch_local(2026, 9, 11, 8, 5), seen),
        ("t1", "20260911", "A", 0, 3, "s2", "SCHEDULED", 300, 300, _epoch_local(2026, 9, 11, 8, 10), seen),
        ("t2", "20260911", "A", 0, 4, "s1", "SCHEDULED", 400, 400, _epoch_local(2026, 9, 11, 8, 15), seen),
        ("t2", "20260911", "A", 0, 5, "s1", "SKIPPED", None, None, None, seen),
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


def _seed_two_days(conn):
    rows = []
    for day in (11, 12):
        start_date = f"202609{day}"
        trips = [
            ("A", "s1", 7, 0, "SCHEDULED", 30),
            ("A", "s2", 7, 10, "SCHEDULED", 400),
            ("A", "s3", 7, 20, "SKIPPED", None),
            ("B", "s1", 17, 45, "SCHEDULED", -90),
            ("B", "s4", 23, 50, "SCHEDULED", 200),
        ]
        for seq, (route, stop, hour, minute, rel, delay) in enumerate(trips, start=1):
            departure = _epoch_local(2026, 9, day, hour, minute) if delay is not None else None
            rows.append((f"t{day}{route}", start_date, route, 0, seq, stop, rel, delay, delay,
                         departure, _epoch_local(2026, 9, day, hour, minute) + 120))
        after_midnight = _epoch_local(2026, 9, day + 1, 0, 45)
        rows.append((f"n{day}", start_date, "B", 1, 1, "s5", "SCHEDULED", 50, 50,
                     _epoch_local(2026, 9, day + 1, 0, 40), after_midnight))
        rows.append((f"n{day}", start_date, "B", 1, 2, "s6", "SKIPPED", None, None, None, after_midnight))
    conn.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def _dump_aggregates(conn, days):
    import db as dbio

    in_days = "', '".join(days)
    return {
        table: conn.execute(
            f"SELECT * FROM {table} WHERE date_service IN ('{in_days}') ORDER BY 1, 2, 3, 4"
        ).fetchall()
        for table in dbio.AGG_TABLES
    }


class TestRefreshIncrementalBorne:
    def test_plan_ne_lit_observations_que_par_les_index_bornes(self, conn):
        import db as dbio

        for sql, params in dbio.incremental_statements(["2026-09-11", "2026-09-12"]):
            accesses = [
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                if row[3].startswith(("SCAN o", "SEARCH o"))
            ]
            assert accesses
            for access in accesses:
                assert ("idx_observations_departure_time" in access
                        or "idx_observations_last_seen_at" in access), access

    def test_incremental_identique_au_calcul_complet(self, conn):
        import db as dbio

        days = ["2026-09-11", "2026-09-12"]
        _seed_two_days(conn)
        dbio.refresh_aggregates(conn, days=None)
        full = _dump_aggregates(conn, days)
        for table in dbio.AGG_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        dbio.refresh_aggregates(conn, days=days)
        assert _dump_aggregates(conn, days) == full
        assert all(full[table] for table in dbio.AGG_TABLES)

    def test_arret_saute_vu_apres_minuit_compte_dans_son_jour_de_service(self, conn):
        import db as dbio

        _seed_two_days(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-12"])
        skipped, eligible = conn.execute(
            "SELECT skipped, eligible FROM agg_daily_stop "
            "WHERE date_service = '2026-09-12' AND route_id = 'B' AND stop_id = 's6'"
        ).fetchone()
        assert (skipped, eligible) == (1, 1)
