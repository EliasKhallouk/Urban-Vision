"""Tests de l'archive du GTFS statique (archive_gtfs.py)."""

import io
import json
import zipfile
from datetime import datetime

import pytest

import archive_gtfs as ag


def _gtfs(version="01/10/2026-04:51", extra=b""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name in ag.REQUIRED_FILES:
            z.writestr(name, "id\n1\n")
        z.writestr("feed_info.txt", "feed_publisher_name,feed_start_date,feed_end_date,feed_version\n"
                                    f"Mecatran,20261001,20261230,{version}\n")
        if extra:
            z.writestr("extra.txt", extra)
    return buffer.getvalue()


class TestArchive:
    def test_nouvelle_version_puis_inchangee(self, tmp_path):
        content = _gtfs()
        first = ag.archive(content, tmp_path, datetime(2026, 10, 1, 6, 15))
        assert first["status"] == "nouvelle version"
        assert first["start_date"] == "20261001" and first["end_date"] == "20261230"
        assert (tmp_path / first["file"]).read_bytes() == content
        second = ag.archive(content, tmp_path, datetime(2026, 10, 2, 6, 15))
        assert second["status"] == "inchangé"
        index = json.loads((tmp_path / "index.json").read_text())
        assert len(index["versions"]) == 1
        assert index["last_checked"] == "2026-10-02T06:15:00"

    def test_version_modifiee_archivee_a_cote(self, tmp_path):
        ag.archive(_gtfs(), tmp_path, datetime(2026, 10, 1))
        result = ag.archive(_gtfs(version="08/10/2026-04:50"), tmp_path, datetime(2026, 10, 8))
        assert result["status"] == "nouvelle version"
        assert len(list(tmp_path.glob("gtfs_*.zip"))) == 2

    def test_gtfs_incomplet_refuse(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("routes.txt", "x")
        with pytest.raises(ValueError, match="stop_times.txt"):
            ag.archive(buffer.getvalue(), tmp_path, datetime(2026, 10, 1))
        assert not list(tmp_path.glob("gtfs_*.zip"))
