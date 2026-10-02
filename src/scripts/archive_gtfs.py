import argparse
import csv
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import requests

import route_shapes
from gtfs_static import GTFS_STATIC_URL

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = ROOT / "data" / "gtfs_archive"
INDEX_NAME = "index.json"
DOWNLOAD_TIMEOUT_SECONDS = 180
REQUIRED_FILES = ("trips.txt", "stop_times.txt", "routes.txt", "stops.txt")


def download(url: str = GTFS_STATIC_URL) -> bytes:
    response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.content


def feed_info(content: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = set(z.namelist())
        missing = [name for name in REQUIRED_FILES if name not in names]
        if missing:
            raise ValueError(f"GTFS incomplet, fichiers absents : {', '.join(missing)}")
        if "feed_info.txt" not in names:
            return {}
        with z.open("feed_info.txt") as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
    row = rows[0] if rows else {}
    return {key: row.get(f"feed_{key}", "") for key in ("start_date", "end_date", "version")}


def load_index(archive_dir: Path) -> dict:
    path = archive_dir / INDEX_NAME
    if not path.exists():
        return {"versions": [], "last_checked": None}
    return json.loads(path.read_text())


def save_index(archive_dir: Path, index: dict) -> None:
    path = archive_dir / INDEX_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False))
    tmp.replace(path)


def archive(content: bytes, archive_dir: Path, now: datetime) -> dict:
    archive_dir.mkdir(parents=True, exist_ok=True)
    info = feed_info(content)
    sha = hashlib.sha256(content).hexdigest()
    index = load_index(archive_dir)
    index["last_checked"] = now.isoformat(timespec="seconds")
    known = {v["sha256"] for v in index["versions"]}
    if sha in known:
        save_index(archive_dir, index)
        return {"status": "inchangé", "sha256": sha}
    name = f"gtfs_{now:%Y-%m-%d}_{sha[:12]}.zip"
    tmp = archive_dir / (name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(archive_dir / name)
    entry = {"file": name, "sha256": sha, "size": len(content), "archived_at": index["last_checked"], **info}
    index["versions"].append(entry)
    save_index(archive_dir, index)
    return {"status": "nouvelle version", **entry}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Archive le GTFS statique TBM quand il change.")
    ap.add_argument("--dest", default=str(ARCHIVE_DIR))
    ap.add_argument("--db", default=str(route_shapes.DB_PATH))
    args = ap.parse_args(argv)
    content = download()
    result = archive(content, Path(args.dest), datetime.now())
    if result["status"] == "inchangé":
        print(f"GTFS statique inchangé ({result['sha256'][:12]}).")
    else:
        print(f"Nouvelle version archivée : {result['file']} ({result['size'] / 1e6:.1f} Mo, "
              f"valide du {result.get('start_date') or '?'} au {result.get('end_date') or '?'}).")
    try:
        conn = sqlite3.connect(args.db, timeout=120)
        try:
            empty = route_shapes.shapes_count(conn) == 0
            if result["status"] != "inchangé" or empty:
                print(f"{route_shapes.refresh_route_shapes(conn, content)} tracés de lignes mis à jour.")
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, KeyError) as e:
        print(f"Tracés des lignes non mis à jour : {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
