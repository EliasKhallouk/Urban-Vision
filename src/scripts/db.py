import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "vigie_tbm.db"
conn = sqlite3.connect(DB_PATH)
conn.executescript("""
CREATE TABLE IF NOT EXISTS observations (
    trip_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id INTEGER,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    schedule_relationship TEXT,
    arrival_delay INTEGER,
    departure_delay INTEGER,
    departure_time INTEGER,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, start_date, stop_sequence)
);

CREATE TABLE IF NOT EXISTS daily_line_stats (
    stat_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    route_short_name TEXT,
    n_observations INTEGER,
    retard_moyen_s REAL,
    retard_median_s REAL,
    pct_retard_5min REAL,
    pct_avance_1min REAL,
    n_arrets_sautes INTEGER,
    computed_at INTEGER NOT NULL,
    PRIMARY KEY (stat_date, route_id)
);

CREATE TABLE IF NOT EXISTS collection_gaps (
    gap_start INTEGER NOT NULL,
    gap_end INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_status (
    trip_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    schedule_relationship TEXT NOT NULL,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, start_date)
);

CREATE INDEX IF NOT EXISTS idx_observations_last_seen_at
    ON observations(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_observations_route
    ON observations(route_id);
CREATE INDEX IF NOT EXISTS idx_observations_sched_delay
    ON observations(schedule_relationship, departure_delay, last_seen_at, route_id);
CREATE INDEX IF NOT EXISTS idx_service_alerts_period
    ON service_alerts(active_period_start, active_period_end);
CREATE INDEX IF NOT EXISTS idx_observations_departure_time
    ON observations(departure_time, schedule_relationship, departure_delay, route_id);

CREATE TABLE IF NOT EXISTS agg_daily (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    obs INTEGER NOT NULL,
    sum_delay INTEGER NOT NULL,
    cnt_le300 INTEGER NOT NULL,
    cnt_gt300 INTEGER NOT NULL,
    cnt_lt60 INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    histogram TEXT NOT NULL,
    PRIMARY KEY (date_service, route_id)
);
CREATE INDEX IF NOT EXISTS idx_agg_daily_service ON agg_daily(date_service);

CREATE TABLE IF NOT EXISTS agg_hourly (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    heure INTEGER NOT NULL,
    obs INTEGER NOT NULL,
    sum_delay INTEGER NOT NULL,
    cnt_le300 INTEGER NOT NULL,
    cnt_gt300 INTEGER NOT NULL,
    PRIMARY KEY (date_service, route_id, heure)
);
CREATE INDEX IF NOT EXISTS idx_agg_hourly_service ON agg_hourly(date_service);
""")
conn.commit()


columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(observations)")
}
if "departure_time" not in columns:
    conn.execute("ALTER TABLE observations ADD COLUMN departure_time INTEGER")


from datetime import datetime, timedelta

AGG_DDL = """
CREATE TABLE IF NOT EXISTS agg_daily (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    obs INTEGER NOT NULL,
    sum_delay INTEGER NOT NULL,
    cnt_le300 INTEGER NOT NULL,
    cnt_gt300 INTEGER NOT NULL,
    cnt_lt60 INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    histogram TEXT NOT NULL,
    PRIMARY KEY (date_service, route_id)
);
CREATE INDEX IF NOT EXISTS idx_agg_daily_service ON agg_daily(date_service);
CREATE TABLE IF NOT EXISTS agg_hourly (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    heure INTEGER NOT NULL,
    obs INTEGER NOT NULL,
    sum_delay INTEGER NOT NULL,
    cnt_le300 INTEGER NOT NULL,
    cnt_gt300 INTEGER NOT NULL,
    PRIMARY KEY (date_service, route_id, heure)
);
CREATE INDEX IF NOT EXISTS idx_agg_hourly_service ON agg_hourly(date_service);
"""

# ::SCHED_BOUNDS:: borne le scan aux jours traités (incrémental) ; chaîne vide = tout
# (re)calculer. `hist` est un objet JSON {secondes_de_retard: effectif} qui permet
# de reconstruire une médiane exacte sur n'importe quelle période de jours.
_DAILY_SQL = """
INSERT OR REPLACE INTO agg_daily
    (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
     skipped, eligible, histogram)
WITH sched AS (
    SELECT o.route_id, date(datetime(o.departure_time, 'unixepoch', 'localtime')) ds,
           o.departure_delay
    FROM observations o
    WHERE o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL ::SCHED_BOUNDS::
),
metrics AS (
    SELECT route_id, ds, COUNT(*) obs, COALESCE(SUM(departure_delay), 0) sum_delay,
           COALESCE(SUM(CASE WHEN departure_delay <= 300 THEN 1 ELSE 0 END), 0) cnt_le300,
           COALESCE(SUM(CASE WHEN departure_delay > 300 THEN 1 ELSE 0 END), 0) cnt_gt300,
           COALESCE(SUM(CASE WHEN departure_delay < -60 THEN 1 ELSE 0 END), 0) cnt_lt60
    FROM sched GROUP BY route_id, ds
),
hist AS (
    SELECT route_id, ds, json_group_object(departure_delay, cnt) h
    FROM (
        SELECT route_id, ds, departure_delay, COUNT(*) cnt
        FROM sched GROUP BY route_id, ds, departure_delay
    ) GROUP BY route_id, ds
),
skp AS (
    SELECT o.route_id,
           substr(o.start_date, 1, 4) || '-' || substr(o.start_date, 5, 2)
               || '-' || substr(o.start_date, 7, 2) ds,
           CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END skipped
    FROM observations o
    WHERE o.schedule_relationship IN ('SCHEDULED', 'SKIPPED')
          AND o.start_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
          ::SKP_BOUNDS::
),
skpagg AS (
    SELECT route_id, ds, SUM(skipped) skipped, COUNT(*) eligible
    FROM skp GROUP BY route_id, ds
),
base AS (
    SELECT ds, route_id FROM metrics
    UNION
    SELECT ds, route_id FROM skpagg
)
SELECT b.ds,
       b.route_id,
       COALESCE(m.obs, 0) obs,
       COALESCE(m.sum_delay, 0) sum_delay,
       COALESCE(m.cnt_le300, 0) cnt_le300,
       COALESCE(m.cnt_gt300, 0) cnt_gt300,
       COALESCE(m.cnt_lt60, 0) cnt_lt60,
       COALESCE(k.skipped, 0) skipped,
       COALESCE(k.eligible, 0) eligible,
       COALESCE(h.h, '{}') histogram
FROM base b
LEFT JOIN metrics m ON m.route_id = b.route_id AND m.ds = b.ds
LEFT JOIN skpagg k ON k.route_id = b.route_id AND k.ds = b.ds
LEFT JOIN hist h ON h.route_id = b.route_id AND h.ds = b.ds
"""

_HOURLY_SQL = """
INSERT OR REPLACE INTO agg_hourly
    (date_service, route_id, heure, obs, sum_delay, cnt_le300, cnt_gt300)
SELECT ds,
       route_id,
       heure,
       COUNT(*) obs,
       COALESCE(SUM(departure_delay), 0) sum_delay,
       SUM(CASE WHEN departure_delay <= 300 THEN 1 ELSE 0 END) cnt_le300,
       SUM(CASE WHEN departure_delay > 300 THEN 1 ELSE 0 END) cnt_gt300
FROM (
    SELECT o.route_id,
           date(datetime(o.departure_time, 'unixepoch', 'localtime')) ds,
           CAST(strftime('%H', datetime(o.departure_time, 'unixepoch', 'localtime')) AS INTEGER) heure,
           o.departure_delay
    FROM observations o
    WHERE o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL ::SCHED_BOUNDS::
)
GROUP BY route_id, ds, heure
"""


def refresh_aggregates(c, days: list[str] | None = None) -> None:
    """Met à jour agg_daily / agg_hourly depuis les observations.

    days=None  -> (re)calcul complet (coûteux, à faire une fois).
    days=[...] -> ne recalcule que les dates-service listées (cheap, pour le
                  collecteur : les jours d'hier et d'aujourd'hui suffisent).
    """
    c.executescript(AGG_DDL)
    if days is None:
        c.execute(_DAILY_SQL.replace("::SCHED_BOUNDS::", "").replace("::SKP_BOUNDS::", ""))
        c.execute(_HOURLY_SQL.replace("::SCHED_BOUNDS::", ""))
    else:
        start = min(days)
        end_dt = datetime.strptime(max(days), "%Y-%m-%d") + timedelta(days=1)
        d0_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp())
        d1_ts = int(end_dt.timestamp())
        day_ints = "', '".join(d.replace("-", "") for d in days)
        day_strs = "', '".join(days)
        c.execute(f"DELETE FROM agg_daily WHERE date_service IN ('{day_strs}')")
        c.execute(f"DELETE FROM agg_hourly WHERE date_service IN ('{day_strs}')")
        c.execute(
            _DAILY_SQL.replace("::SCHED_BOUNDS::", "AND o.departure_time >= ? AND o.departure_time < ?")
                        .replace("::SKP_BOUNDS::", f"AND o.start_date IN ('{day_ints}')"),
            (d0_ts, d1_ts),
        )
        c.execute(_HOURLY_SQL.replace("::SCHED_BOUNDS::", "AND o.departure_time >= ? AND o.departure_time < ?"), (d0_ts, d1_ts))
    c.commit()