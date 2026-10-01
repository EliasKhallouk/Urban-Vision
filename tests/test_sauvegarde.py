"""Tests de la sauvegarde quotidienne (sauvegarde.py) : copie, contrôle, restauration, rotation, envoi."""

import json
import shutil
from datetime import datetime

import pytest
import requests

import sauvegarde as sv


def _seed(conn):
    conn.executemany(
        "INSERT INTO observations (trip_id, start_date, route_id, stop_sequence, stop_id, "
        "schedule_relationship, departure_delay, departure_time, last_seen_at) "
        "VALUES (?, '20260911', 'A', ?, 's', 'SCHEDULED', 30, 1000, 1000)",
        [(f"t{i}", i) for i in range(50)],
    )
    conn.commit()


class TestSauvegarde:
    def test_copie_controlee_puis_restauree(self, conn, db_path, tmp_path):
        _seed(conn)
        dest = tmp_path / "sauvegardes"
        manifest = sv.run_backup(db_path, dest, datetime(2026, 10, 2, 2, 30))
        archive = dest / manifest["file"]
        assert archive.exists()
        assert manifest["quick_check"] == "ok"
        assert manifest["rows"]["observations"] == 50
        assert manifest["uploaded"] is False
        report = sv.verify(archive)
        assert report == {"quick_check": "ok", "rows": manifest["rows"]}
        assert json.loads((dest / "urban_vision_2026-10-02.json").read_text())["sha256"] == manifest["sha256"]

    def test_compression_gzip_sans_zstd(self, conn, db_path, tmp_path, monkeypatch):
        _seed(conn)
        monkeypatch.setattr(sv.shutil, "which", lambda name: None)
        manifest = sv.run_backup(db_path, tmp_path / "s", datetime(2026, 10, 2))
        assert manifest["file"].endswith(".db.gz")
        assert sv.verify(tmp_path / "s" / manifest["file"])["rows"]["observations"] == 50

    def test_archive_alteree_detectee(self, conn, db_path, tmp_path):
        _seed(conn)
        manifest = sv.run_backup(db_path, tmp_path, datetime(2026, 10, 2))
        archive = tmp_path / manifest["file"]
        with open(archive, "ab") as f:
            f.write(b"x")
        with pytest.raises(RuntimeError, match="SHA-256"):
            sv.verify(archive)

    def test_envoi_hors_vm(self, conn, db_path, tmp_path, monkeypatch):
        _seed(conn)
        sent = []

        class Ok:
            def raise_for_status(self):
                pass

        def put(url, data, timeout, headers):
            sent.append((url, len(data.read())))
            return Ok()

        monkeypatch.setattr(sv.requests, "put", put)
        manifest = sv.run_backup(db_path, tmp_path, datetime(2026, 10, 2), par_url="https://objets.example/p/abc/o/")
        assert manifest["uploaded"] is True
        assert sent[0][0] == "https://objets.example/p/abc/o/" + manifest["file"]
        assert sent[0][1] == manifest["size"]

    def test_echec_d_envoi_garde_la_copie_locale(self, conn, db_path, tmp_path, monkeypatch):
        _seed(conn)

        def put(*a, **k):
            raise requests.ConnectionError("bucket injoignable")

        monkeypatch.setattr(sv.requests, "put", put)
        manifest = sv.run_backup(db_path, tmp_path, datetime(2026, 10, 2), par_url="https://objets.example/o/")
        assert manifest["uploaded"] is False
        assert "bucket injoignable" in manifest["upload_error"]
        assert (tmp_path / manifest["file"]).exists()
        saved = json.loads((tmp_path / "urban_vision_2026-10-02.json").read_text())
        assert saved["upload_error"] == manifest["upload_error"]

    def test_main_code_retour_2_si_envoi_echoue(self, conn, db_path, tmp_path, monkeypatch):
        _seed(conn)
        monkeypatch.setenv("UV_BACKUP_PAR_URL", "https://objets.example/o/")
        monkeypatch.setattr(sv.requests, "put", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
        assert sv.main(["--db", str(db_path), "--dest", str(tmp_path)]) == 2
        assert sv.main(["--db", str(db_path), "--dest", str(tmp_path), "--sans-envoi"]) == 0
        assert sv.main(["--dest", str(tmp_path), "--verifier"]) == 0


class TestRotation:
    def test_garde_sept_jours_et_le_premier_de_six_mois(self):
        dates = [f"2026-{m:02d}-{d:02d}" for m in range(1, 11) for d in (1, 15, 28)]
        keep = sv.to_keep(dates)
        assert keep == set(dates[-7:]) | {"2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"}
        assert "2026-04-01" not in keep

    def test_rotation_supprime_archive_et_manifeste(self, tmp_path, monkeypatch):
        for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
            (tmp_path / f"urban_vision_{day}.json").write_text("{}")
            (tmp_path / f"urban_vision_{day}.db.zst").write_text("x")
        monkeypatch.setattr(sv, "KEEP_DAILY", 1)
        monkeypatch.setattr(sv, "KEEP_MONTHLY", 1)
        assert sv.rotate(tmp_path) == ["2026-08-02"]
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "urban_vision_2026-08-01.db.zst", "urban_vision_2026-08-01.json",
            "urban_vision_2026-08-03.db.zst", "urban_vision_2026-08-03.json",
        ]


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd absent")
def test_zstd_utilise_quand_disponible(conn, db_path, tmp_path):
    _seed(conn)
    assert sv.run_backup(db_path, tmp_path, datetime(2026, 10, 2))["file"].endswith(".db.zst")
