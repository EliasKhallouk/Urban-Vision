import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS agg_daily_stop (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    obs INTEGER NOT NULL,
    sum_delay INTEGER NOT NULL,
    cnt_le300 INTEGER NOT NULL,
    cnt_gt300 INTEGER NOT NULL,
    cnt_lt60 INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    histogram TEXT NOT NULL,
    PRIMARY KEY (date_service, route_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_agg_daily_stop_service ON agg_daily_stop(date_service);
CREATE INDEX IF NOT EXISTS idx_agg_daily_stop_stop ON agg_daily_stop(stop_id);
CREATE TABLE IF NOT EXISTS agg_hourly_stop (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    heure INTEGER NOT NULL,
    obs INTEGER NOT NULL,
    sum_delay INTEGER NOT NULL,
    cnt_le300 INTEGER NOT NULL,
    cnt_gt300 INTEGER NOT NULL,
    PRIMARY KEY (date_service, route_id, stop_id, heure)
);
CREATE INDEX IF NOT EXISTS idx_agg_hourly_stop_service ON agg_hourly_stop(date_service);
CREATE INDEX IF NOT EXISTS idx_agg_hourly_stop_stop ON agg_hourly_stop(stop_id);
CREATE TABLE IF NOT EXISTS agg_daily_trips (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    scheduled INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    added INTEGER NOT NULL,
    PRIMARY KEY (date_service, route_id)
);
CREATE TABLE IF NOT EXISTS agg_hourly_regularity (
    date_service TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    heure INTEGER NOT NULL,
    n_act INTEGER NOT NULL,
    sum_h_act INTEGER NOT NULL,
    sum_h2_act INTEGER NOT NULL,
    n_sch INTEGER NOT NULL,
    sum_h_sch INTEGER NOT NULL,
    sum_h2_sch INTEGER NOT NULL,
    PRIMARY KEY (date_service, route_id, direction_id, stop_id, heure)
);
CREATE INDEX IF NOT EXISTS idx_agg_hourly_regularity_route ON agg_hourly_regularity(route_id, date_service);
CREATE TABLE IF NOT EXISTS quality_days (
    date_service TEXT PRIMARY KEY,
    passages INTEGER NOT NULL,
    lacunar_hours TEXT NOT NULL,
    flag TEXT NOT NULL,
    computed_at INTEGER NOT NULL
);
"""

# Schéma SQLite : source unique de vérité, réutilisé par init_db() à l'import et
# par les tests sur une base temporaire. Idempotent (CREATE IF NOT EXISTS).
# service_alerts est défini ici (et non seulement dans collect_alerts.py) pour
# que le schéma soit auto-porteur : l'index idx_service_alerts_period en dépend.
SCHEMA_DDL = """
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
    pred_dep_10 INTEGER,
    pred_dep_5 INTEGER,
    pred_dep_2 INTEGER,
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

CREATE TABLE IF NOT EXISTS collection_runs (
    started_at INTEGER PRIMARY KEY,
    feed_ts INTEGER,
    entities INTEGER,
    rows_written INTEGER,
    fetch_ms INTEGER,
    write_ms INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS trip_status (
    trip_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    schedule_relationship TEXT NOT NULL,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, start_date)
);

CREATE TABLE IF NOT EXISTS service_alerts (
    alert_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    active_period_start INTEGER NOT NULL,
    active_period_end INTEGER,
    header_text TEXT,
    description_text TEXT,
    cause INTEGER,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (alert_id, route_id, active_period_start)
);

CREATE INDEX IF NOT EXISTS idx_observations_last_seen_at
    ON observations(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_observations_route
    ON observations(route_id);
DROP INDEX IF EXISTS idx_observations_sched_delay;
CREATE INDEX IF NOT EXISTS idx_service_alerts_period
    ON service_alerts(active_period_start, active_period_end);
CREATE INDEX IF NOT EXISTS idx_observations_departure_time
    ON observations(departure_time, schedule_relationship, departure_delay, route_id);
""" + AGG_DDL


OBSERVATION_COLUMNS_ADDED = (
    ("departure_time", "INTEGER"),
    ("pred_dep_10", "INTEGER"),
    ("pred_dep_5", "INTEGER"),
    ("pred_dep_2", "INTEGER"),
)


def init_db(conn) -> None:
    """Migre les colonnes ajoutées depuis, applique le schéma complet (idempotent), puis valide."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    if columns:
        for name, sql_type in OBSERVATION_COLUMNS_ADDED:
            if name not in columns:
                conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {sql_type}")
    conn.executescript(SCHEMA_DDL)
    conn.commit()


DB_PATH = Path(__file__).resolve().parents[2] / "data" / "urban_vision.db"
conn = sqlite3.connect(DB_PATH)
init_db(conn)


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
    WHERE +o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
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
    WHERE +o.schedule_relationship IN ('SCHEDULED', 'SKIPPED')
          AND o.start_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
          ::SKP_BOUNDS::
),
skpagg AS (
    SELECT route_id, ds, SUM(skipped) skipped, COUNT(*) eligible
    FROM skp GROUP BY route_id, ds
),
parts AS (
    SELECT ds, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
           0 skipped, 0 eligible, NULL h
    FROM metrics
    UNION ALL
    SELECT ds, route_id, 0, 0, 0, 0, 0, skipped, eligible, NULL FROM skpagg
    UNION ALL
    SELECT ds, route_id, 0, 0, 0, 0, 0, 0, 0, h FROM hist
)
SELECT ds, route_id, SUM(obs), SUM(sum_delay), SUM(cnt_le300), SUM(cnt_gt300),
       SUM(cnt_lt60), SUM(skipped), SUM(eligible), COALESCE(MAX(h), '{}')
FROM parts
GROUP BY ds, route_id
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
    WHERE +o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL ::SCHED_BOUNDS::
)
GROUP BY route_id, ds, heure
"""


_DAILY_STOP_SQL = """
INSERT OR REPLACE INTO agg_daily_stop
    (date_service, route_id, stop_id, obs, sum_delay, cnt_le300, cnt_gt300,
     cnt_lt60, skipped, eligible, histogram)
WITH sched AS (
    SELECT o.route_id, o.stop_id,
           date(datetime(o.departure_time, 'unixepoch', 'localtime')) ds,
           o.departure_delay
    FROM observations o
    WHERE +o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL ::SCHED_BOUNDS::
),
metrics AS (
    SELECT route_id, stop_id, ds, COUNT(*) obs, COALESCE(SUM(departure_delay), 0) sum_delay,
           COALESCE(SUM(CASE WHEN departure_delay <= 300 THEN 1 ELSE 0 END), 0) cnt_le300,
           COALESCE(SUM(CASE WHEN departure_delay > 300 THEN 1 ELSE 0 END), 0) cnt_gt300,
           COALESCE(SUM(CASE WHEN departure_delay < -60 THEN 1 ELSE 0 END), 0) cnt_lt60
    FROM sched GROUP BY route_id, stop_id, ds
),
hist AS (
    SELECT route_id, stop_id, ds, json_group_object(departure_delay, cnt) h
    FROM (
        SELECT route_id, stop_id, ds, departure_delay, COUNT(*) cnt
        FROM sched GROUP BY route_id, stop_id, ds, departure_delay
    ) GROUP BY route_id, stop_id, ds
),
skp AS (
    SELECT o.route_id, o.stop_id,
           substr(o.start_date, 1, 4) || '-' || substr(o.start_date, 5, 2)
               || '-' || substr(o.start_date, 7, 2) ds,
           CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END skipped
    FROM observations o
    WHERE +o.schedule_relationship IN ('SCHEDULED', 'SKIPPED')
          AND o.start_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
          ::SKP_BOUNDS::
),
skpagg AS (
    SELECT route_id, stop_id, ds, SUM(skipped) skipped, COUNT(*) eligible
    FROM skp GROUP BY route_id, stop_id, ds
),
parts AS (
    SELECT ds, route_id, stop_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
           0 skipped, 0 eligible, NULL h
    FROM metrics
    UNION ALL
    SELECT ds, route_id, stop_id, 0, 0, 0, 0, 0, skipped, eligible, NULL FROM skpagg
    UNION ALL
    SELECT ds, route_id, stop_id, 0, 0, 0, 0, 0, 0, 0, h FROM hist
)
SELECT ds, route_id, stop_id, SUM(obs), SUM(sum_delay), SUM(cnt_le300), SUM(cnt_gt300),
       SUM(cnt_lt60), SUM(skipped), SUM(eligible), COALESCE(MAX(h), '{}')
FROM parts
GROUP BY ds, route_id, stop_id
"""

_HOURLY_STOP_SQL = """
INSERT OR REPLACE INTO agg_hourly_stop
    (date_service, route_id, stop_id, heure, obs, sum_delay, cnt_le300, cnt_gt300)
SELECT ds,
       route_id,
       stop_id,
       heure,
       COUNT(*) obs,
       COALESCE(SUM(departure_delay), 0) sum_delay,
       SUM(CASE WHEN departure_delay <= 300 THEN 1 ELSE 0 END) cnt_le300,
       SUM(CASE WHEN departure_delay > 300 THEN 1 ELSE 0 END) cnt_gt300
FROM (
    SELECT o.route_id, o.stop_id,
           date(datetime(o.departure_time, 'unixepoch', 'localtime')) ds,
           CAST(strftime('%H', datetime(o.departure_time, 'unixepoch', 'localtime')) AS INTEGER) heure,
           o.departure_delay
    FROM observations o
    WHERE +o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL ::SCHED_BOUNDS::
)
GROUP BY route_id, stop_id, ds, heure
"""


SKP_LAST_SEEN_MARGIN_SECONDS = 86400
AGG_TABLES = ("agg_daily", "agg_hourly", "agg_daily_stop", "agg_hourly_stop")


def incremental_statements(days: list[str]) -> list[tuple[str, tuple]]:
    d0_ts = int(datetime.strptime(min(days), "%Y-%m-%d").timestamp())
    d1_ts = int((datetime.strptime(max(days), "%Y-%m-%d") + timedelta(days=1)).timestamp())
    day_ints = "', '".join(d.replace("-", "") for d in days)
    sched_bounds = "AND o.departure_time >= ? AND o.departure_time < ?"
    skp_bounds = f"AND o.start_date IN ('{day_ints}') AND o.last_seen_at >= ? AND o.last_seen_at < ?"
    sched_params = (d0_ts, d1_ts)
    daily_params = sched_params + (
        d0_ts - SKP_LAST_SEEN_MARGIN_SECONDS,
        d1_ts + SKP_LAST_SEEN_MARGIN_SECONDS,
    )
    return [
        (_DAILY_SQL.replace("::SCHED_BOUNDS::", sched_bounds).replace("::SKP_BOUNDS::", skp_bounds), daily_params),
        (_HOURLY_SQL.replace("::SCHED_BOUNDS::", sched_bounds), sched_params),
        (_DAILY_STOP_SQL.replace("::SCHED_BOUNDS::", sched_bounds).replace("::SKP_BOUNDS::", skp_bounds), daily_params),
        (_HOURLY_STOP_SQL.replace("::SCHED_BOUNDS::", sched_bounds), sched_params),
    ]


def refresh_aggregates(c, days: list[str] | None = None) -> None:
    """Met à jour les tables d'agrégation depuis les observations.

    days=None  -> (re)calcul complet (coûteux, à faire une fois).
    days=[...] -> ne recalcule que les dates-service listées (cheap, pour le
                  collecteur : les jours d'hier et d'aujourd'hui suffisent).
    """
    c.executescript(AGG_DDL)
    if days is None:
        c.execute(_DAILY_SQL.replace("::SCHED_BOUNDS::", "").replace("::SKP_BOUNDS::", ""))
        c.execute(_HOURLY_SQL.replace("::SCHED_BOUNDS::", ""))
        c.execute(_DAILY_STOP_SQL.replace("::SCHED_BOUNDS::", "").replace("::SKP_BOUNDS::", ""))
        c.execute(_HOURLY_STOP_SQL.replace("::SCHED_BOUNDS::", ""))
    else:
        day_strs = "', '".join(days)
        for table in AGG_TABLES:
            c.execute(f"DELETE FROM {table} WHERE date_service IN ('{day_strs}')")
        for sql, params in incremental_statements(days):
            c.execute(sql, params)
    refresh_v2(c, days)
    c.commit()


TRIP_CANCELLED = ("CANCELED", "DELETED")
TRIP_ADDED = ("ADDED", "NEW", "DUPLICATED", "REPLACEMENT")
FREQUENT_MIN_DEPARTURES_PER_HOUR = 5
HEADWAY_MAX_SECONDS = 3600
V2_TABLES = ("agg_daily_trips", "agg_hourly_regularity")

_TRIPS_SQL = f"""
INSERT OR REPLACE INTO agg_daily_trips (date_service, route_id, scheduled, cancelled, added)
SELECT substr(start_date, 1, 4) || '-' || substr(start_date, 5, 2) || '-' || substr(start_date, 7, 2) ds,
       route_id,
       SUM(schedule_relationship NOT IN {TRIP_CANCELLED + TRIP_ADDED}),
       SUM(schedule_relationship IN {TRIP_CANCELLED}),
       SUM(schedule_relationship IN {TRIP_ADDED})
FROM trip_status
WHERE start_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' ::DAYS::
GROUP BY ds, route_id
"""

_REGULARITY_SQL = f"""
INSERT OR REPLACE INTO agg_hourly_regularity
    (date_service, route_id, direction_id, stop_id, heure,
     n_act, sum_h_act, sum_h2_act, n_sch, sum_h_sch, sum_h2_sch)
WITH p AS (
    SELECT o.route_id, COALESCE(o.direction_id, -1) dir, o.stop_id,
           o.departure_time act, o.departure_time - o.departure_delay sch
    FROM observations o
    WHERE +o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL ::WINDOW_BOUNDS::
),
heads AS (
    SELECT route_id, dir, stop_id, act t, 1 kind,
           act - LAG(act) OVER (PARTITION BY route_id, dir, stop_id ORDER BY act) h
    FROM p
    UNION ALL
    SELECT route_id, dir, stop_id, sch t, 2 kind,
           sch - LAG(sch) OVER (PARTITION BY route_id, dir, stop_id ORDER BY sch) h
    FROM p
),
g AS (
    SELECT date(datetime(t, 'unixepoch', 'localtime')) ds, route_id, dir, stop_id,
           CAST(strftime('%H', datetime(t, 'unixepoch', 'localtime')) AS INTEGER) heure,
           SUM(kind = 1) n_act,
           COALESCE(SUM(CASE WHEN kind = 1 THEN h END), 0) sum_h_act,
           COALESCE(SUM(CASE WHEN kind = 1 THEN h * h END), 0) sum_h2_act,
           SUM(kind = 2) n_sch,
           COALESCE(SUM(CASE WHEN kind = 2 THEN h END), 0) sum_h_sch,
           COALESCE(SUM(CASE WHEN kind = 2 THEN h * h END), 0) sum_h2_sch
    FROM heads
    WHERE h > 0 AND h <= {HEADWAY_MAX_SECONDS}
    GROUP BY ds, route_id, dir, stop_id, heure
)
SELECT ds, route_id, dir, stop_id, heure, n_act, sum_h_act, sum_h2_act, n_sch, sum_h_sch, sum_h2_sch
FROM g
WHERE n_sch >= {FREQUENT_MIN_DEPARTURES_PER_HOUR} ::DAY_FILTER::
"""


def v2_statements(days: list[str]) -> list[tuple[str, tuple]]:
    d0_ts = int(datetime.strptime(min(days), "%Y-%m-%d").timestamp())
    d1_ts = int((datetime.strptime(max(days), "%Y-%m-%d") + timedelta(days=1)).timestamp())
    day_ints = "', '".join(d.replace("-", "") for d in days)
    day_strs = "', '".join(days)
    return [
        (_TRIPS_SQL.replace("::DAYS::", f"AND start_date IN ('{day_ints}')"), ()),
        (_REGULARITY_SQL.replace("::WINDOW_BOUNDS::", "AND o.departure_time >= ? AND o.departure_time < ?")
                        .replace("::DAY_FILTER::", f"AND ds IN ('{day_strs}')"),
         (d0_ts - HEADWAY_MAX_SECONDS, d1_ts)),
    ]


def refresh_v2(c, days: list[str] | None = None) -> None:
    c.executescript(AGG_DDL)
    if days is None:
        for table in V2_TABLES:
            c.execute(f"DELETE FROM {table}")
        c.execute(_TRIPS_SQL.replace("::DAYS::", ""))
        c.execute(_REGULARITY_SQL.replace("::WINDOW_BOUNDS::", "").replace("::DAY_FILTER::", ""))
    else:
        day_strs = "', '".join(days)
        for table in V2_TABLES:
            c.execute(f"DELETE FROM {table} WHERE date_service IN ('{day_strs}')")
        for sql, params in v2_statements(days):
            c.execute(sql, params)
    refresh_quality_days(c, days)
    c.commit()


QUALITY_HOURS = range(5, 24)
QUALITY_MIN_BASELINE = 500
QUALITY_LACUNAR_RATIO = 0.5
QUALITY_INCOMPLETE_HOURS = 3
QUALITY_REFERENCE_WEEKS = (1, 2, 3)


def day_quality(hourly: dict, day: str) -> tuple[str, list[int]]:
    d = datetime.strptime(day, "%Y-%m-%d")
    refs = [hourly[r] for r in ((d - timedelta(days=7 * k)).strftime("%Y-%m-%d") for k in QUALITY_REFERENCE_WEEKS)
            if r in hourly]
    if len(refs) < 2:
        return "non_evalue", []
    current = hourly.get(day, {})
    lacunar = []
    for hour in QUALITY_HOURS:
        values = sorted(ref.get(hour, 0) for ref in refs)
        mid = len(values) // 2
        baseline = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        if baseline >= QUALITY_MIN_BASELINE and current.get(hour, 0) < QUALITY_LACUNAR_RATIO * baseline:
            lacunar.append(hour)
    if len(lacunar) >= QUALITY_INCOMPLETE_HOURS:
        return "incomplet", lacunar
    return ("degrade" if lacunar else "ok"), lacunar


def refresh_quality_days(c, days: list[str] | None = None, today: str | None = None) -> None:
    today = today or datetime.now().strftime("%Y-%m-%d")
    hourly: dict = {}
    for ds, hour, n in c.execute("SELECT date_service, heure, SUM(obs) FROM agg_hourly GROUP BY date_service, heure"):
        hourly.setdefault(ds, {})[hour] = n
    targets = sorted(hourly) if days is None else days
    for day in targets:
        if day >= today:
            continue
        flag, lacunar = day_quality(hourly, day)
        c.execute(
            "INSERT OR REPLACE INTO quality_days (date_service, passages, lacunar_hours, flag, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (day, sum(hourly.get(day, {}).values()), json.dumps(lacunar), flag, int(time.time())),
        )


# Direction dominante par (ligne, arrêt), étiquetée par le terminus de la ligne
# dans cette direction. Beaucoup de noms d'arrêts existent en double (deux
# sens, plusieurs quais) : cette table permet au dashboard de les distinguer.
STOP_DIRECTION_DDL = """
CREATE TABLE IF NOT EXISTS stop_direction (
    route_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    direction_id INTEGER,
    terminus TEXT,
    PRIMARY KEY (route_id, stop_id)
)
"""


def refresh_stop_directions(c) -> None:
    """Calcule la direction dominante de chaque arrêt par ligne (backfill one-shot).

    Pour chaque (ligne, arrêt), on retient la direction_id la plus fréquente dans
    observations, puis on l'étiquette par le terminus de la ligne dans cette
    direction (arrêt au stop_sequence maximal). Les directions reflètent la
    géométrie statique de la ligne : le calcul ne change quasiment jamais, il
    n'est donc lancé que lorsque la table est vide.

    Le terminus est matérialisé dans une table temporaire : la jointure directe
    de deux sous-requêtes fenêtrées fait exploser le plan SQLite (réévaluation
    corrélée), alors que chaque morceau pris seul est rapide.
    """
    c.executescript(STOP_DIRECTION_DDL)
    c.execute("DROP TABLE IF EXISTS _termini")
    c.execute("""
        CREATE TEMP TABLE _termini AS
        SELECT route_id, direction_id, stop_name
        FROM (
            SELECT o.route_id, o.direction_id, o.stop_id, s.stop_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY o.route_id, o.direction_id
                       ORDER BY o.stop_sequence DESC
                   ) AS rn
            FROM observations o LEFT JOIN stops s ON s.stop_id = o.stop_id
            WHERE o.direction_id IS NOT NULL
        ) WHERE rn = 1
    """)
    c.execute("DELETE FROM stop_direction")
    c.execute("""
        INSERT INTO stop_direction (route_id, stop_id, direction_id, terminus)
        SELECT d.route_id, d.stop_id, d.direction_id, t.stop_name AS terminus
        FROM (
            SELECT * FROM (
                SELECT o.route_id, o.stop_id, o.direction_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY o.route_id, o.stop_id
                           ORDER BY COUNT(*) DESC
                       ) AS rn
                FROM observations o
                WHERE o.direction_id IS NOT NULL
                GROUP BY o.route_id, o.stop_id, o.direction_id
            ) WHERE rn = 1
        ) d
        LEFT JOIN _termini t ON t.route_id = d.route_id AND t.direction_id = d.direction_id
    """)
    c.execute("DROP TABLE IF EXISTS _termini")
    c.commit()