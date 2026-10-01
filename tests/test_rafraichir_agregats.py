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


class TestMain:
    def test_recalcule_les_deux_jours(self, conn, db_path, tmp_path, monkeypatch):
        import db as dbio

        seen = []
        monkeypatch.setattr(dbio, "refresh_aggregates", lambda c, days=None: seen.append(days))
        assert ra.main(["--db", str(db_path), "--log", str(tmp_path / "collect.log")]) == 0
        assert len(seen) == 1 and len(seen[0]) == 2
        assert "Agrégats rafraîchis en" in (tmp_path / "collect.log").read_text()

    def test_echec_signale_par_le_code_retour(self, conn, db_path, tmp_path, monkeypatch):
        import db as dbio

        def boom(c, days=None):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dbio, "refresh_aggregates", boom)
        assert ra.main(["--db", str(db_path), "--log", str(tmp_path / "collect.log")]) == 1
        assert "Refresh des agrégats échoué : database is locked" in (tmp_path / "collect.log").read_text()
