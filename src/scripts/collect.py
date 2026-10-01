"""
collect.py
Interroge le flux GTFS-RT TripUpdates de TBM toutes les X secondes
et enregistre/actualise les observations dans la base SQLite.
Conçu pour tourner en continu, en tâche de fond, sur plusieurs semaines.
"""

import sqlite3
import time
import logging
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

import db as dbio

PROJECT_ROOT = Path(__file__).resolve().parents[2]
URL_TRIPUPDATES = (
    "https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux"
    "?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt"
)
DB_PATH = str(PROJECT_ROOT / "data" / "urban_vision.db")
POLL_INTERVAL_SECONDS = 60
GAP_THRESHOLD_SECONDS = 180  # 3x l'intervalle normal de 60s, marge de sécurité
# Attendre le verrou d'écriture (ex. un autre script qui écrit) au lieu d'échouer.
DB_BUSY_TIMEOUT_MS = 120_000
PREDICTION_HORIZONS = (("pred_dep_10", 600), ("pred_dep_5", 300), ("pred_dep_2", 120))
RUN_ERROR_MAX_CHARS = 500


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(PROJECT_ROOT / "data" / "collect.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def fetch_feed() -> gtfs_realtime_pb2.FeedMessage:
    response = requests.get(URL_TRIPUPDATES, timeout=15)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


_PREDICTION_SET = ",\n".join(
    f"""            {column} = COALESCE(observations.{column}, CASE
                WHEN excluded.departure_time - excluded.last_seen_at <= {seconds}
                 AND observations.departure_time - observations.last_seen_at > {seconds}
                THEN excluded.departure_time END)"""
    for column, seconds in PREDICTION_HORIZONS
)

UPSERT_OBSERVATION_SQL = f"""
        INSERT INTO observations
            (trip_id, start_date, route_id, direction_id, stop_sequence,
             stop_id, schedule_relationship, arrival_delay, departure_delay,
             departure_time, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trip_id, start_date, stop_sequence) DO UPDATE SET
            schedule_relationship = excluded.schedule_relationship,
            arrival_delay = excluded.arrival_delay,
            departure_delay = excluded.departure_delay,
            departure_time = excluded.departure_time,
            last_seen_at = excluded.last_seen_at,
{_PREDICTION_SET}
"""


def upsert_observation(conn, trip_id, start_date, route_id, direction_id,
                        stop_sequence, stop_id, schedule_relationship,
                        arrival_delay, departure_delay, departure_time,
                        feed_timestamp):
    conn.execute(UPSERT_OBSERVATION_SQL, (
        trip_id, start_date, route_id, direction_id, stop_sequence,
        stop_id, schedule_relationship, arrival_delay, departure_delay,
        departure_time, feed_timestamp,
    ))


def log_trip_status(conn, trip_id, start_date, route_id, trip_schedule_relationship, feed_timestamp):
    conn.execute("""
        INSERT INTO trip_status (trip_id, start_date, route_id, schedule_relationship, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(trip_id, start_date) DO UPDATE SET
            schedule_relationship = excluded.schedule_relationship,
            last_seen_at = excluded.last_seen_at
    """, (trip_id, start_date, route_id, trip_schedule_relationship, feed_timestamp))


def process_feed(conn, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    feed_timestamp = feed.header.timestamp
    n_rows = 0
    

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        start_date = tu.trip.start_date
        route_id = tu.trip.route_id
        direction_id = tu.trip.direction_id
        trip_schedule_relationship = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(
            tu.trip.schedule_relationship
        )

        log_trip_status(conn, trip_id, start_date, route_id, trip_schedule_relationship, feed_timestamp)

        for stu in tu.stop_time_update:
            schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(
                stu.schedule_relationship
            )

            # Le premier arrêt d'un trajet a souvent une arrival.delay
            # incohérente (bus garé avant l'heure de service) -> on l'ignore.
            arrival_delay = None
            if stu.HasField("arrival") and stu.stop_sequence != 1:
                arrival_delay = stu.arrival.delay

            departure_delay = None
            departure_time = None
            if stu.HasField("departure"):
                departure_delay = stu.departure.delay
                departure_time = stu.departure.time  # epoch absolu GTFS-RT

            upsert_observation(
                conn, trip_id, start_date, route_id, direction_id,
                stu.stop_sequence, stu.stop_id, schedule_relationship,
                arrival_delay, departure_delay, departure_time, feed_timestamp,
            )
            n_rows += 1

    conn.commit()
    return n_rows


def record_gap_if_any(conn, last_success_ts, now):
    if last_success_ts is not None and (now - last_success_ts) > GAP_THRESHOLD_SECONDS:
        conn.execute(
            "INSERT INTO collection_gaps (gap_start, gap_end) VALUES (?, ?)",
            (int(last_success_ts), int(now)),
        )
        conn.commit()
        logger.warning("Trou de collecte détecté : %.0f minutes", (now - last_success_ts) / 60)


def record_run(conn, started_at, run):
    try:
        conn.execute(
            "INSERT OR REPLACE INTO collection_runs "
            "(started_at, feed_ts, entities, rows_written, fetch_ms, write_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(started_at), run.get("feed_ts"), run.get("entities"), run.get("rows_written"),
             run.get("fetch_ms"), run.get("write_ms"), run.get("error")),
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("Journal de collecte non écrit : %s", e)


def get_last_known_success(conn):
    row = conn.execute(
        "SELECT MAX(started_at) FROM collection_runs WHERE error IS NULL"
    ).fetchone()
    if row[0] is not None:
        return float(row[0])
    row = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()
    return float(row[0]) if row[0] is not None else None


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS};")
    dbio.init_db(conn)
    logger.info("Démarrage de la collecte Urban Vision (intervalle: %ss)", POLL_INTERVAL_SECONDS)

    last_success_ts = get_last_known_success(conn)

    while True:
        cycle_start = time.monotonic()
        started_at = time.time()
        run = {}
        try:
            feed = fetch_feed()
            run["fetch_ms"] = int((time.monotonic() - cycle_start) * 1000)

            now = time.time()
            record_gap_if_any(conn, last_success_ts, now)
            last_success_ts = now

            write_start = time.monotonic()
            n_rows = process_feed(conn, feed)
            run.update(
                feed_ts=feed.header.timestamp, entities=len(feed.entity), rows_written=n_rows,
                write_ms=int((time.monotonic() - write_start) * 1000),
            )
            logger.info(
                "OK - %d entités, %d observations mises à jour (feed ts=%s)",
                len(feed.entity), n_rows, feed.header.timestamp,
            )
        except requests.RequestException as e:
            run["error"] = f"Flux indisponible : {e}"[:RUN_ERROR_MAX_CHARS]
            logger.warning("Échec de récupération du flux : %s", e)
        except Exception as e:
            conn.rollback()
            run["error"] = f"Erreur de traitement : {e}"[:RUN_ERROR_MAX_CHARS]
            logger.exception("Erreur inattendue pendant le traitement : %s", e)
        record_run(conn, started_at, run)

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0, POLL_INTERVAL_SECONDS - elapsed))

if __name__ == "__main__":
    main()