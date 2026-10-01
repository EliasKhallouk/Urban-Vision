"""Tests de db.refresh_segments : retard pris tronçon par tronçon (agg_daily_segment)."""

import json
from datetime import datetime


def _epoch_local(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


def _seen(row):
    start_date, departure_time = row[1], row[8]
    noon = _epoch_local(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:]), 12)
    return departure_time or noon


def _insert(conn, rows):
    conn.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
        [row + (_seen(row),) for row in rows],
    )
    conn.commit()


def _seed(conn):
    """Ligne A, direction 0, 2026-09-11.

    t1 : s1 (+60) → s2 (+180) → s3 SKIPPED → s4 (+150) → s5 (+400)
    t2 : s1 (+0)  → s2 (+30)  → s4 (+30)   → s5 (+40)
    t3 (direction absente) : s1 (+10) → s2 (+20)
    """
    t = lambda h, m: _epoch_local(2026, 9, 11, h, m)  # noqa: E731
    _insert(conn, [
        ("t1", "20260911", "A", 0, 1, "s1", "SCHEDULED", 60, t(8, 0)),
        ("t1", "20260911", "A", 0, 2, "s2", "SCHEDULED", 180, t(8, 5)),
        ("t1", "20260911", "A", 0, 3, "s3", "SKIPPED", None, None),
        ("t1", "20260911", "A", 0, 4, "s4", "SCHEDULED", 150, t(8, 15)),
        ("t1", "20260911", "A", 0, 5, "s5", "SCHEDULED", 400, t(8, 20)),
        ("t2", "20260911", "A", 0, 1, "s1", "SCHEDULED", 0, t(9, 0)),
        ("t2", "20260911", "A", 0, 2, "s2", "SCHEDULED", 30, t(9, 5)),
        ("t2", "20260911", "A", 0, 4, "s4", "SCHEDULED", 30, t(9, 15)),
        ("t2", "20260911", "A", 0, 5, "s5", "SCHEDULED", 40, t(9, 20)),
        ("t3", "20260911", "A", None, 1, "s1", "SCHEDULED", 10, t(10, 0)),
        ("t3", "20260911", "A", None, 2, "s2", "SCHEDULED", 20, t(10, 5)),
    ])


_COLS = ("eligible, skipped, sum_seq, obs, sum_delay, pairs, sum_prev_delay, "
         "sum_gain, cnt_gain_gt120, prev_stop_id, hist_gain")


def _row(conn, stop_id, direction_id=0, day="2026-09-11"):
    r = conn.execute(
        f"SELECT {_COLS} FROM agg_daily_segment WHERE date_service = ? AND route_id = 'A' "
        "AND direction_id = ? AND stop_id = ?",
        (day, direction_id, stop_id),
    ).fetchone()
    if r is None:
        return None
    return dict(zip([c.strip() for c in _COLS.split(",")], r))


class TestRefreshSegments:
    def test_premier_arret_sans_troncon_amont(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        s1 = _row(conn, "s1")
        assert (s1["obs"], s1["sum_delay"], s1["pairs"], s1["sum_gain"]) == (2, 60, 0, 0)
        assert s1["prev_stop_id"] is None
        assert json.loads(s1["hist_gain"]) == {}

    def test_retard_pris_et_retard_deja_present(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        s2 = _row(conn, "s2")
        assert s2["pairs"] == 2
        assert s2["sum_prev_delay"] == 60 + 0
        assert s2["sum_gain"] == 120 + 30
        assert s2["sum_delay"] == 180 + 30
        assert s2["cnt_gain_gt120"] == 0
        assert s2["prev_stop_id"] == "s1"
        assert json.loads(s2["hist_gain"]) == {"120": 1, "30": 1}

    def test_arret_saute_ignore_par_le_troncon_mais_compte(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        s4 = _row(conn, "s4")
        assert s4["prev_stop_id"] == "s2"
        assert s4["sum_gain"] == (150 - 180) + (30 - 30)
        s3 = _row(conn, "s3")
        assert (s3["eligible"], s3["skipped"], s3["obs"], s3["sum_seq"]) == (1, 1, 0, 3)

    def test_troncons_de_plus_de_2_minutes(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        s5 = _row(conn, "s5")
        assert s5["cnt_gain_gt120"] == 1
        assert s5["sum_gain"] == 250 + 10

    def test_ordre_moyen_par_arrets_attendus(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        s4 = _row(conn, "s4")
        assert s4["eligible"] == 2
        assert s4["sum_seq"] / s4["eligible"] == 4

    def test_direction_absente_isolee(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        s2_nodir = _row(conn, "s2", direction_id=-1)
        assert (s2_nodir["obs"], s2_nodir["pairs"], s2_nodir["sum_gain"]) == (1, 1, 10)
        assert _row(conn, "s2")["obs"] == 2

    def test_arret_precedent_le_plus_frequent(self, conn):
        import db as dbio

        t = lambda m: _epoch_local(2026, 9, 11, 12, m)  # noqa: E731
        _insert(conn, [
            ("u1", "20260911", "A", 1, 1, "x", "SCHEDULED", 0, t(0)),
            ("u1", "20260911", "A", 1, 2, "z", "SCHEDULED", 10, t(1)),
            ("u2", "20260911", "A", 1, 1, "y", "SCHEDULED", 0, t(2)),
            ("u2", "20260911", "A", 1, 2, "z", "SCHEDULED", 10, t(3)),
            ("u3", "20260911", "A", 1, 1, "y", "SCHEDULED", 0, t(4)),
            ("u3", "20260911", "A", 1, 2, "z", "SCHEDULED", 10, t(5)),
        ])
        dbio.refresh_segments(conn, days=["2026-09-11"])
        assert _row(conn, "z", direction_id=1)["prev_stop_id"] == "y"

    def test_voyage_a_cheval_sur_minuit(self, conn):
        import db as dbio

        _insert(conn, [
            ("n1", "20260911", "A", 0, 1, "s1", "SCHEDULED", 30, _epoch_local(2026, 9, 11, 23, 55)),
            ("n1", "20260911", "A", 0, 2, "s2", "SCHEDULED", 90, _epoch_local(2026, 9, 12, 0, 5)),
        ])
        dbio.refresh_segments(conn, days=["2026-09-12"])
        s2 = _row(conn, "s2", day="2026-09-12")
        assert (s2["pairs"], s2["sum_prev_delay"], s2["sum_gain"]) == (1, 30, 60)
        assert conn.execute(
            "SELECT COUNT(*) FROM agg_daily_segment WHERE date_service = '2026-09-11' AND obs > 0"
        ).fetchone()[0] == 0

    def test_incremental_idempotent_et_egal_au_complet(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        dbio.refresh_segments(conn, days=["2026-09-11"])
        incremental = conn.execute(
            "SELECT * FROM agg_daily_segment ORDER BY route_id, direction_id, stop_id"
        ).fetchall()
        conn.execute("DELETE FROM agg_daily_segment")
        dbio.refresh_segments(conn)
        complet = conn.execute(
            "SELECT * FROM agg_daily_segment ORDER BY route_id, direction_id, stop_id"
        ).fetchall()
        assert incremental == complet
        assert len(complet) == 7

    def test_plan_ne_lit_observations_que_par_les_index_bornes(self, conn):
        import db as dbio

        sql, params = dbio.incremental_segment_statement(["2026-09-11", "2026-09-12"])
        accesses = [
            row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
            if row[3].startswith(("SCAN o", "SEARCH o"))
        ]
        assert accesses
        for access in accesses:
            assert ("idx_observations_departure_time" in access
                    or "idx_observations_last_seen_at" in access), access
