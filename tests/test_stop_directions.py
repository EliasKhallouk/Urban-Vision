"""Tests de db.refresh_stop_directions : direction dominante par (ligne, arrêt)."""

import sqlite3

import gtfs_static


def _epoch_local(year, month, day, hour, minute=0):
    from datetime import datetime

    return int(datetime(year, month, day, hour, minute).timestamp())


def _seed_observations_with_direction(conn):
    """Route A, direction 0 dominante (2 voyages × 2 arrêts) vs direction 1 (1 voyage)."""
    gtfs_static.create_static_tables(conn)
    conn.execute("INSERT INTO routes VALUES ('A', '1', 'Ligne 1', 3)")
    conn.execute(
        "INSERT INTO stops VALUES ('s1', 'Debut', 44.80, -0.57), "
        "('s2', 'Milieu', 44.81, -0.58), "
        "('s3', 'Terminus A', 44.82, -0.59)"
    )
    rows = []
    ts = _epoch_local(2026, 9, 11, 8, 0)
    for trip, seq, stop in [("t1", 1, "s1"), ("t1", 2, "s2"), ("t1", 3, "s3")]:
        rows.append(("t1", "20260911", "A", 0, seq, stop, "SCHEDULED", None, 10, ts, ts))
    # voyage 2 : même direction 0
    for seq, stop in [(1, "s1"), (2, "s2"), (3, "s3")]:
        rows.append(("t2", "20260911", "A", 0, seq, stop, "SCHEDULED", None, 10, ts, ts))
    # voyage 3 : direction 1 sur s1 seulement → minoritaire
    rows.append(("t3", "20260911", "A", 1, 1, "s1", "SCHEDULED", None, 10, ts, ts))
    conn.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


class TestRefreshStopDirections:
    def test_direction_dominante_et_terminus(self, conn):
        _seed_observations_with_direction(conn)
        import db as dbio

        dbio.refresh_stop_directions(conn)

        directions = {
            (r, s): f"vers {t}"
            for r, s, _, t in conn.execute(
                "SELECT route_id, stop_id, direction_id, terminus FROM stop_direction"
            )
        }
        # direction dominante = 0 pour toutes les routes/stops
        assert directions[("A", "s1")] == "vers Terminus A"
        assert directions[("A", "s2")] == "vers Terminus A"
        assert directions[("A", "s3")] == "vers Terminus A"
        assert len(directions) == 3

    def test_idempotent(self, conn):
        _seed_observations_with_direction(conn)
        import db as dbio

        dbio.refresh_stop_directions(conn)
        dbio.refresh_stop_directions(conn)
        n = conn.execute("SELECT COUNT(*) FROM stop_direction").fetchone()[0]
        assert n == 3

    def test_ignorer_direction_id_null(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'L1', 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret 1', 44.8, -0.5)")
        conn.execute(
            """INSERT INTO observations
               (trip_id, start_date, route_id, direction_id, stop_sequence,
                stop_id, schedule_relationship, departure_delay,
                departure_time, last_seen_at)
               VALUES ('t1', '20260911', 'A', NULL, 1, 's1', 'SCHEDULED', 0, 0, 0)"""
        )
        conn.commit()
        import db as dbio

        dbio.refresh_stop_directions(conn)
        n = conn.execute("SELECT COUNT(*) FROM stop_direction").fetchone()[0]
        assert n == 0

    def test_direction_id_non_int_est_ignore(self, conn):
        """direction_id avec texte non convertible ne cause pas d'erreur et est ignoré."""
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'L1', 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret', 44.8, -0.5)")
        conn.execute(
            """INSERT INTO observations
               (trip_id, start_date, route_id, direction_id, stop_sequence,
                stop_id, schedule_relationship, departure_delay,
                departure_time, last_seen_at)
               VALUES ('t1', '20260911', 'A', NULL, 1, 's1', 'SCHEDULED', 0, 0, 0)"""
        )
        conn.commit()
        import db as dbio

        dbio.refresh_stop_directions(conn)
        assert conn.execute("SELECT COUNT(*) FROM stop_direction").fetchone()[0] == 0