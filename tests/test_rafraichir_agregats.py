"""Tests du recalcul des agrégats lancé par timer (rafraichir_agregats.py)."""

import sqlite3
from datetime import datetime

import rafraichir_agregats as ra


class CapturingLogger:
    def __init__(self):
        self.messages = []

    def info(self, msg, *args):
        self.messages.append(("info", msg % args))

    def warning(self, msg, *args):
        self.messages.append(("warning", msg % args))


class TestJours:
    def test_hier_et_aujourd_hui(self):
        assert ra.days_to_refresh(datetime(2026, 10, 1, 0, 2)) == ["2026-09-30", "2026-10-01"]


class TestDuree:
    def test_info_puis_warning_au_dela_du_seuil(self, monkeypatch):
        log = CapturingLogger()
        monkeypatch.setattr(ra, "logger", log)
        ra.log_refresh_duration(12.34)
        ra.log_refresh_duration(ra.REFRESH_WARN_SECONDS + 15)
        assert log.messages == [
            ("info", "Agrégats rafraîchis en 12.3 s"),
            ("warning", "Rafraîchissement des agrégats lent : 75.0 s (seuil 60 s)"),
        ]


class TestDureeTroncons:
    def test_libelle_des_troncons(self, monkeypatch):
        log = CapturingLogger()
        monkeypatch.setattr(ra, "logger", log)
        ra.log_refresh_duration(5.3, "tronçons")
        ra.log_refresh_duration(ra.REFRESH_WARN_SECONDS + 1, "tronçons")
        assert log.messages == [
            ("info", "Tronçons rafraîchis en 5.3 s"),
            ("warning", "Rafraîchissement des tronçons lent : 61.0 s (seuil 60 s)"),
        ]


def _segment_obs(c):
    c.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence, stop_id,
            schedule_relationship, arrival_delay, departure_delay, departure_time, last_seen_at)
           VALUES (?, '20260911', 'A', 0, ?, ?, 'SCHEDULED', NULL, ?, ?, ?)""",
        [("t1", 1, "s1", 0, 1_789_120_000, 1_789_120_000), ("t1", 2, "s2", 60, 1_789_120_300, 1_789_120_300)],
    )
    c.commit()


class TestRattrapageTroncons:
    def test_table_vide_recalculee_jour_par_jour(self, conn, monkeypatch):
        import db as dbio

        _segment_obs(conn)
        dbio.refresh_aggregates(conn, days=None)
        calls = []
        pauses = []
        real = dbio.refresh_segments
        monkeypatch.setattr(dbio, "refresh_segments", lambda c, days=None: (calls.append(days), real(c, days=days)))
        monkeypatch.setattr(ra.time, "sleep", pauses.append)
        assert ra.ensure_segments(conn) == 1
        assert calls == [["2026-09-11"]]
        assert pauses == [ra.BACKFILL_PAUSE_SECONDS]
        assert conn.execute("SELECT SUM(pairs), SUM(sum_gain) FROM agg_daily_segment").fetchone() == (1, 60)

    def test_table_remplie_laissee_intacte(self, conn, monkeypatch):
        import db as dbio

        _segment_obs(conn)
        dbio.refresh_segments(conn)
        calls = []
        monkeypatch.setattr(dbio, "refresh_segments", lambda c, days=None: calls.append(days))
        assert ra.ensure_segments(conn) == 0
        assert calls == []


class TestMain:
    def test_recalcule_agregats_et_troncons(self, conn, db_path, tmp_path, monkeypatch):
        import db as dbio

        seen = []
        monkeypatch.setattr(dbio, "refresh_aggregates", lambda c, days=None: seen.append(("agg", days)))
        monkeypatch.setattr(dbio, "refresh_segments", lambda c, days=None: seen.append(("seg", days)))
        monkeypatch.setattr(ra, "ensure_segments", lambda c: 0)
        assert ra.main(["--db", str(db_path), "--log", str(tmp_path / "collect.log")]) == 0
        assert [kind for kind, _ in seen] == ["agg", "seg"]
        assert all(len(days) == 2 for _, days in seen)
        text = (tmp_path / "collect.log").read_text()
        assert "Agrégats rafraîchis en" in text and "Tronçons rafraîchis en" in text

    def test_echec_des_troncons_n_empeche_pas_les_agregats(self, conn, db_path, tmp_path, monkeypatch):
        import db as dbio

        seen = []
        monkeypatch.setattr(dbio, "refresh_aggregates", lambda c, days=None: seen.append(days))

        def boom(c, days=None):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dbio, "refresh_segments", boom)
        monkeypatch.setattr(ra, "ensure_segments", lambda c: 0)
        assert ra.main(["--db", str(db_path), "--log", str(tmp_path / "collect.log")]) == 1
        assert len(seen) == 1
        assert "Refresh des tronçons échoué : database is locked" in (tmp_path / "collect.log").read_text()

    def test_echec_signale_par_le_code_retour(self, conn, db_path, tmp_path, monkeypatch):
        import db as dbio

        def boom(c, days=None):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dbio, "refresh_aggregates", boom)
        assert ra.main(["--db", str(db_path), "--log", str(tmp_path / "collect.log")]) == 1
        assert "Refresh des agrégats échoué : database is locked" in (tmp_path / "collect.log").read_text()


class TestDepartsEnAvanceParHeure:
    def _seed(self, conn):
        departure = int(datetime(2026, 9, 11, 8, 0).timestamp())
        conn.executemany(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, stop_sequence, stop_id, "
            "schedule_relationship, departure_delay, departure_time, last_seen_at) "
            "VALUES (?, '20260911', 'A', 0, 1, 's1', 'SCHEDULED', ?, ?, ?)",
            [("t1", -120, departure, departure), ("t2", 30, departure + 600, departure + 600)])
        conn.commit()

    def test_colonne_ajoutee_a_une_ancienne_table(self):
        import db as dbio

        old = sqlite3.connect(":memory:")
        old.execute("CREATE TABLE agg_hourly (date_service TEXT, route_id TEXT, heure INTEGER, obs INTEGER, "
                    "sum_delay INTEGER, cnt_le300 INTEGER, cnt_gt300 INTEGER)")
        old.execute("INSERT INTO agg_hourly VALUES ('2026-09-11', 'A', 8, 2, 0, 2, 0)")
        dbio.migrate_agg_columns(old)
        assert old.execute("SELECT cnt_lt60 FROM agg_hourly").fetchone() == (0,)

    def test_jours_sans_departs_en_avance_recalcules(self, conn, monkeypatch):
        import db as dbio

        self._seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        assert conn.execute("SELECT SUM(cnt_lt60) FROM agg_hourly").fetchone() == (1,)
        assert dbio.hourly_early_missing_days(conn) == []
        conn.execute("UPDATE agg_hourly SET cnt_lt60 = 0")
        conn.execute("UPDATE agg_hourly_stop SET cnt_lt60 = 0")
        assert dbio.hourly_early_missing_days(conn) == ["2026-09-11"]
        monkeypatch.setattr(ra.time, "sleep", lambda s: None)
        assert ra.ensure_hourly_early(conn, "2026-09-11") == 0
        assert ra.ensure_hourly_early(conn, "2026-09-12") == 1
        assert conn.execute("SELECT SUM(cnt_lt60) FROM agg_hourly_stop").fetchone() == (1,)
        assert dbio.hourly_early_missing_days(conn) == []
