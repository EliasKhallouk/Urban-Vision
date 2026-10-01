import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "urban_vision.db"
BACKUP_DIR = ROOT / "data" / "sauvegardes"
KEEP_DAILY = 7
KEEP_MONTHLY = 6
PREFIX = "urban_vision_"
UPLOAD_TIMEOUT_SECONDS = 3600
CHUNK = 1024 * 1024
CHECK_TABLES = ("observations", "trip_status", "agg_daily", "agg_daily_stop", "collection_gaps")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(db_path: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=120)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def inspect(db_file: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {}
        for table in CHECK_TABLES:
            if table in tables:
                counts[table] = conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0] or 0
        return {"quick_check": check, "rows": counts}
    finally:
        conn.close()


def compress(raw: Path, final_stem: Path) -> Path:
    if shutil.which("zstd"):
        out = final_stem.with_name(final_stem.name + ".db.zst")
        subprocess.run(["zstd", "-q", "-f", "-T0", "-3", str(raw), "-o", str(out)], check=True)
        return out
    out = final_stem.with_name(final_stem.name + ".db.gz")
    with open(raw, "rb") as src, gzip.open(out, "wb", compresslevel=3) as dst:
        shutil.copyfileobj(src, dst, CHUNK)
    return out


def decompress(archive: Path, target: Path) -> None:
    if archive.name.endswith(".zst"):
        subprocess.run(["zstd", "-q", "-d", "-f", str(archive), "-o", str(target)], check=True)
    else:
        with gzip.open(archive, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)


def backup_date(path: Path) -> str:
    return path.name[len(PREFIX):len(PREFIX) + 10]


def backups(dest: Path) -> list[Path]:
    return sorted(p for p in dest.glob(PREFIX + "*.json"))


def to_keep(dates: list[str], keep_daily: int | None = None, keep_monthly: int | None = None) -> set[str]:
    keep_daily = KEEP_DAILY if keep_daily is None else keep_daily
    keep_monthly = KEEP_MONTHLY if keep_monthly is None else keep_monthly
    dates = sorted(set(dates))
    keep = set(dates[-keep_daily:])
    first_of_month = {}
    for d in dates:
        first_of_month.setdefault(d[:7], d)
    keep.update(sorted(first_of_month.values())[-keep_monthly:])
    return keep


def rotate(dest: Path) -> list[str]:
    manifests = backups(dest)
    keep = to_keep([backup_date(m) for m in manifests])
    removed = []
    for manifest in manifests:
        day = backup_date(manifest)
        if day in keep:
            continue
        for f in dest.glob(f"{PREFIX}{day}.*"):
            f.unlink()
        removed.append(day)
    return removed


def upload(archive: Path, par_url: str) -> None:
    url = par_url.rstrip("/") + "/" + archive.name
    with open(archive, "rb") as f:
        response = requests.put(url, data=f, timeout=UPLOAD_TIMEOUT_SECONDS,
                                headers={"Content-Type": "application/octet-stream"})
    response.raise_for_status()


def run_backup(db_path: Path, dest: Path, now: datetime, par_url: str | None = None) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    day = now.strftime("%Y-%m-%d")
    stem = dest / f"{PREFIX}{day}"
    start = time.monotonic()
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        raw = Path(tmp) / "copie.db"
        snapshot(db_path, raw)
        report = inspect(raw)
        if report["quick_check"] != "ok":
            raise RuntimeError(f"Copie corrompue : {report['quick_check']}")
        raw_size = raw.stat().st_size
        archive = compress(raw, stem)
    manifest = {
        "date": day,
        "created_at": now.isoformat(timespec="seconds"),
        "file": archive.name,
        "size": archive.stat().st_size,
        "uncompressed_size": raw_size,
        "sha256": sha256_of(archive),
        "quick_check": report["quick_check"],
        "rows": report["rows"],
        "duration_s": round(time.monotonic() - start, 1),
        "uploaded": False,
    }
    manifest_path = stem.with_name(stem.name + ".json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    if par_url:
        try:
            upload(archive, par_url)
            manifest["uploaded"] = True
        except (OSError, requests.RequestException) as e:
            manifest["upload_error"] = str(e)[:300]
        manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest["removed"] = rotate(dest)
    return manifest


def verify(archive: Path) -> dict:
    manifest_path = archive.with_name(archive.name.split(".db.")[0] + ".json")
    expected = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if expected.get("sha256") and sha256_of(archive) != expected["sha256"]:
        raise RuntimeError("Empreinte SHA-256 différente de celle du manifeste.")
    with tempfile.TemporaryDirectory(dir=archive.parent) as tmp:
        raw = Path(tmp) / "restauration.db"
        decompress(archive, raw)
        report = inspect(raw)
    if report["quick_check"] != "ok":
        raise RuntimeError(f"Sauvegarde corrompue : {report['quick_check']}")
    if expected.get("rows") and report["rows"] != expected["rows"]:
        raise RuntimeError("Contenu différent de celui du manifeste.")
    return report


def latest_archive(dest: Path) -> Path | None:
    manifests = backups(dest)
    if not manifests:
        return None
    return dest / json.loads(manifests[-1].read_text())["file"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sauvegarde quotidienne de la base Urban Vision.")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dest", default=str(BACKUP_DIR))
    ap.add_argument("--verifier", nargs="?", const="derniere", default=None,
                    help="restaure une sauvegarde dans un dossier temporaire et la contrôle (par défaut : la dernière)")
    ap.add_argument("--sans-envoi", action="store_true", dest="sans_envoi",
                    help="ne pas copier la sauvegarde hors de la VM, même si UV_BACKUP_PAR_URL est défini")
    args = ap.parse_args(argv)
    dest = Path(args.dest)

    if args.verifier:
        archive = latest_archive(dest) if args.verifier == "derniere" else Path(args.verifier)
        if archive is None or not archive.exists():
            print("Aucune sauvegarde à vérifier.", file=sys.stderr)
            return 1
        report = verify(archive)
        print(f"Sauvegarde {archive.name} restaurée et contrôlée : {report['quick_check']}, {report['rows']}")
        return 0

    par_url = None if args.sans_envoi else (os.environ.get("UV_BACKUP_PAR_URL") or None)
    manifest = run_backup(Path(args.db), dest, datetime.now(), par_url)
    print(
        f"Sauvegarde {manifest['file']} : {manifest['size'] / 1e6:.0f} Mo "
        f"({manifest['uncompressed_size'] / 1e6:.0f} Mo non compressés), {manifest['duration_s']} s, "
        f"contrôle {manifest['quick_check']}, copie hors VM : {'oui' if manifest['uploaded'] else 'non'}, "
        f"anciennes supprimées : {', '.join(manifest['removed']) or 'aucune'}"
    )
    if manifest.get("upload_error"):
        print(f"Échec de la copie hors VM : {manifest['upload_error']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
