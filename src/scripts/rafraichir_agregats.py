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
BACKFILL_PAUSE_SECONDS = 1.0

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


def ensure_v2_history(conn, today: str) -> int:
    conn.executescript(dbio.AGG_DDL)
    known = {d for (d,) in conn.execute("SELECT date_service FROM quality_days")}
    missing = [d for (d,) in conn.execute("SELECT DISTINCT date_service FROM agg_daily ORDER BY 1")
               if d not in known and d < today]
    if not missing:
        return 0
    logger.info("Méthode 2.0 : calcul de l'historique, %d jours…", len(missing))
    start = time.monotonic()
    for day in missing:
        dbio.refresh_v2(conn, days=[day])
        time.sleep(BACKFILL_PAUSE_SECONDS)
    dbio.refresh_quality_days(conn, None, today)
    conn.commit()
    logger.info("Méthode 2.0 : historique calculé en %.0f s", time.monotonic() - start)
    return len(missing)


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
    now = datetime.now()
    status = 0
    try:
        try:
            log_refresh_duration(refresh(conn, now))
        except Exception as e:
            logger.warning("Refresh des agrégats échoué : %s", e)
            status = 1
        try:
            ensure_v2_history(conn, now.strftime("%Y-%m-%d"))
            conn.execute("PRAGMA optimize")
        except Exception as e:
            logger.warning("Méthode 2.0 : calcul de l'historique échoué : %s", e)
            status = 1
    finally:
        conn.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
