import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import db as dbio

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "urban_vision.db"
LOG_PATH = PROJECT_ROOT / "data" / "collect.log"
DB_BUSY_TIMEOUT_MS = 120_000
REFRESH_WARN_SECONDS = 60

logger = logging.getLogger("rafraichir_agregats")


def days_to_refresh(now: datetime) -> list[str]:
    return [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]


def log_refresh_duration(seconds: float, what: str = "agrégats") -> None:
    if seconds > REFRESH_WARN_SECONDS:
        logger.warning(
            "Rafraîchissement des %s lent : %.1f s (seuil %d s)", what, seconds, REFRESH_WARN_SECONDS
        )
    else:
        logger.info("%s rafraîchis en %.1f s", what.capitalize(), seconds)


def refresh(conn, now: datetime) -> float:
    start = time.monotonic()
    dbio.refresh_aggregates(conn, days=days_to_refresh(now))
    return time.monotonic() - start


def ensure_segments(conn) -> int:
    conn.executescript(dbio.AGG_DDL)
    if conn.execute("SELECT 1 FROM agg_daily_segment LIMIT 1").fetchone() is not None:
        return 0
    days = [d for (d,) in conn.execute("SELECT DISTINCT date_service FROM agg_daily ORDER BY 1")]
    logger.info("Rattrapage des tronçons (agg_daily_segment) : %d jours…", len(days))
    start = time.monotonic()
    for day in days:
        dbio.refresh_segments(conn, days=[day])
    logger.info("Rattrapage des tronçons terminé en %.0f s", time.monotonic() - start)
    return len(days)


def refresh_segments(conn, now: datetime) -> float:
    start = time.monotonic()
    dbio.refresh_segments(conn, days=days_to_refresh(now))
    return time.monotonic() - start


def configure_logging(log_path: Path) -> None:
    handlers = [logging.StreamHandler()]
    if log_path.parent.exists():
        handlers.append(logging.FileHandler(str(log_path)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers, force=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Recalcule les agrégats et les tronçons d'hier et d'aujourd'hui.")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--log", default=str(LOG_PATH))
    args = ap.parse_args(argv)
    configure_logging(Path(args.log))
    conn = sqlite3.connect(args.db, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS};")
    status = 0
    now = datetime.now()
    try:
        try:
            log_refresh_duration(refresh(conn, now))
        except Exception as e:
            logger.warning("Refresh des agrégats échoué : %s", e)
            status = 1
        try:
            ensure_segments(conn)
            log_refresh_duration(refresh_segments(conn, now), "tronçons")
        except Exception as e:
            logger.warning("Refresh des tronçons échoué : %s", e)
            status = 1
    finally:
        conn.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
