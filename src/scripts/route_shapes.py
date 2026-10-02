import argparse
import csv
import io
import json
import math
import sqlite3
import sys
import zipfile
from collections import Counter
from pathlib import Path

import db as dbio

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "urban_vision.db"
ARCHIVE_DIR = ROOT / "data" / "gtfs_archive"
SIMPLIFY_METERS = 4.0
METERS_PER_DEGREE = 111_320.0



def _rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def main_shapes(z: zipfile.ZipFile) -> dict:
    """Tracé le plus emprunté de chaque (ligne, sens) : {(route_id, direction_id): (shape_id, nb de courses)}."""
    counts = Counter()
    for row in _rows(z, "trips.txt"):
        if row.get("shape_id"):
            counts[(row["route_id"], int(row.get("direction_id") or 0), row["shape_id"])] += 1
    best = {}
    for (route, direction, shape), n in counts.items():
        if n > best.get((route, direction), ("", 0))[1]:
            best[(route, direction)] = (shape, n)
    return best


def read_points(z: zipfile.ZipFile, shape_ids: set) -> dict:
    points = {}
    for row in _rows(z, "shapes.txt"):
        if row["shape_id"] in shape_ids:
            points.setdefault(row["shape_id"], []).append(
                (int(row["shape_pt_sequence"]), float(row["shape_pt_lon"]), float(row["shape_pt_lat"])))
    return {sid: [(lon, lat) for _, lon, lat in sorted(pts)] for sid, pts in points.items()}


def simplify(points: list, tolerance_m: float = SIMPLIFY_METERS) -> list:
    """Douglas-Peucker sur une projection locale en mètres ; garde les extrémités."""
    if len(points) < 3:
        return list(points)
    lat0 = math.radians(sum(p[1] for p in points) / len(points))
    xy = [(lon * METERS_PER_DEGREE * math.cos(lat0), lat * METERS_PER_DEGREE) for lon, lat in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (ax, ay), (bx, by) = xy[first], xy[last]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        worst, index = 0.0, None
        for i in range(first + 1, last):
            px, py = xy[i]
            dist = (abs(dy * px - dx * py + bx * ay - by * ax) / length) if length else math.hypot(px - ax, py - ay)
            if dist > worst:
                worst, index = dist, i
        if index is not None and worst > tolerance_m:
            keep[index] = True
            stack += [(first, index), (index, last)]
    return [p for p, k in zip(points, keep) if k]


def build_shapes(content: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        best = main_shapes(z)
        points = read_points(z, {shape for shape, _ in best.values()})
    out = []
    for (route, direction), (shape, trips) in sorted(best.items()):
        pts = points.get(shape)
        if pts and len(pts) >= 2:
            coords = [[round(lon, 6), round(lat, 6)] for lon, lat in simplify(pts)]
            out.append({"route_id": route, "direction_id": direction, "shape_id": shape, "trips": trips,
                        "coords": coords})
    return out


def refresh_route_shapes(conn, content: bytes) -> int:
    shapes = build_shapes(content)
    dbio.init_db(conn)
    conn.execute("DELETE FROM route_shapes")
    conn.executemany(
        "INSERT INTO route_shapes (route_id, direction_id, shape_id, trips, coords) VALUES (?, ?, ?, ?, ?)",
        [(s["route_id"], s["direction_id"], s["shape_id"], s["trips"], json.dumps(s["coords"], separators=(",", ":")))
         for s in shapes])
    conn.commit()
    return len(shapes)


def shapes_count(conn) -> int:
    dbio.init_db(conn)
    return conn.execute("SELECT COUNT(*) FROM route_shapes").fetchone()[0]


def latest_archive(archive_dir: Path = ARCHIVE_DIR) -> Path | None:
    index = archive_dir / "index.json"
    if not index.exists():
        return None
    versions = json.loads(index.read_text()).get("versions") or []
    return archive_dir / versions[-1]["file"] if versions else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Charge le tracé principal de chaque ligne depuis un GTFS archivé.")
    ap.add_argument("--zip", help="archive GTFS (défaut : la plus récente de data/gtfs_archive)")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)
    path = Path(args.zip) if args.zip else latest_archive()
    if path is None or not path.exists():
        print("Aucune archive GTFS disponible.", file=sys.stderr)
        return 1
    conn = sqlite3.connect(args.db, timeout=120)
    try:
        n = refresh_route_shapes(conn, path.read_bytes())
    finally:
        conn.close()
    print(f"{n} tracés chargés depuis {path.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
