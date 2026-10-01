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


def configure_logging(log_path: Path) -> None:
    handlers = [logging.StreamHandler()]
    if log_path.parent.exists():
        handlers.append(logging.FileHandler(str(log_path)))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers, force=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Recalcule les agrégats d'hier et d'aujourd'hui.")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--log", default=str(LOG_PATH))
    args = ap.parse_args(argv)
    configure_logging(Path(args.log))
    conn = sqlite3.connect(args.db, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS};")
    try:
        seconds = refresh(conn, datetime.now())
    except Exception as e:
        logger.warning("Refresh des agrégats échoué : %s", e)
        return 1
    finally:
        conn.close()
    log_refresh_duration(seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
