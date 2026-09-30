# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Maintenance rules (documentation contract, audit section 26, no code comments, DB path conventions) live in `AGENTS.md` and apply here in full:

@AGENTS.md

The single source of truth for architecture, operations and production is `docs/DOCUMENTATION_TECHNIQUE.md` (French, ~1600 lines). Read the relevant section before changing behaviour, and update it in the same change. The project language (docs, UI, reports, test docstrings) is French.

## Commands

```bash
.venv/bin/python -m pytest                                  # full suite (pytest.ini: testpaths=tests, -q)
.venv/bin/python -m pytest tests/test_analyze.py            # one file
.venv/bin/python -m pytest tests/test_app_helpers.py::TestFormatSeconds::test_formats  # one test
.venv/bin/python -m pytest -k ranking                       # by keyword

.venv/bin/streamlit run dashboard/app.py                    # dashboard on 127.0.0.1:8501
.venv/bin/python src/scripts/db.py                          # create/migrate data/urban_vision.db
.venv/bin/python reports/generate_single_report.py --month 2026-08 --network --compile
.venv/bin/python reports/generate_single_report.py --month 2026-08 --commune "Mérignac" --compile
.venv/bin/python reports/generate_all_reports.py --month 2026-08 --compile
```

No linter, formatter, build step, CI, Docker or Makefile. `--compile` needs `xelatex` (see `apt-requirement.txt`); without it only `.tex` is written. Full script/argument reference: doc § 13.

## Architecture

Pipeline (doc § 2, § 8): TBM GTFS-RT feeds → collectors → one SQLite DB (WAL) → aggregate tables → Streamlit dashboard and LaTeX/PDF monthly reports.

- **Collectors** (`src/scripts/`, long-running systemd services in prod): `collect.py` polls TripUpdates every 60 s and upserts `observations` / `trip_status` (idempotent on `(trip_id, start_date, stop_sequence)`), logs `collection_gaps`, and calls `db.refresh_aggregates(days=[yesterday, today])` every 300 s. `collect_alerts.py` polls ServiceAlerts every 120 s into `service_alerts` (alert × route × period cartesian product).
- **Reference data**: `gtfs_static.py` fills `routes`/`stops`; `assign_stop_municipalities.py` maps stops to the 28 communes via pure-Python point-in-polygon (no geo library) with an API Adresse fallback.
- **`src/scripts/db.py`** is the only place for schema DDL and aggregate SQL (`agg_daily`, `agg_hourly`, `agg_daily_stop`, `agg_hourly_stop` with JSON delay histograms for exact medians; `agg_daily_segment` = delay gained between consecutive observed stops of the same trip, via `refresh_segments`, backfilled by the collector at startup and never by the dashboard; `stop_direction` backfill). Importing it has a side effect: it opens the real DB and runs `init_db` + the `departure_time` migration.
- **Dashboard** (`dashboard/app.py`, `dashboard/highcharts.py`, `dashboard/diagnostic.py`): reads almost exclusively `agg_*`; loaders are `@st.cache_data(ttl=60)` with an unhashed `_conn` first param; `_ensure_aggregates` rebuilds aggregates if empty/incomplete. Only the active page computes its charts, and panel sub-views use `st.segmented_control` (not `st.tabs`, which renders every tab). `diagnostic.py` holds the pure stop/line diagnostic rules and French sentences (doc § 11.7); the stop and line panels are opened from the map, tables or `?arret=` / `?ligne=` URLs through `st.session_state["stop_id"]` / `["line_id"]` (never use those keys as widget keys: the search widgets `stop_search` / `line_pick` are resynced each run, because a widget whose options change with the period is reset by Streamlit); navigation helpers (`show_stop`, `open_line`, `request_scroll`) also scroll the page to the opened panel.
- **Reports** (`reports/`): `generate_monthly_report.py` is the engine (queries observations directly, not `agg_*`; excludes on-demand Flex' lines; `latex()` escapes all external text); `generate_single_report.py` / `generate_all_reports.py` orchestrate. Output goes to gitignored `reports/output/<YYYY-MM>/`.
- **`reports/palette.py`** is the shared source for colours and KPI thresholds, used by dashboard, Highcharts configs and reports.
- `analyze.py` / `daily_line_stats` is a legacy chain that neither the dashboard nor the reports read.
- `veille_visiteurs.py` is stdlib-only and runs from root cron on the VM over nginx logs (doc § 17.2); unrelated to the transit pipeline.

pydeck gotcha: every string passed to `pdk.Layer(...)` is turned into a JavaScript expression (`"@@=..."`), so string constants such as `size_units` or `icon_atlas` must be wrapped in single quotes (`"'meters'"`); `tests/test_app_helpers.py` checks the serialized layer.

Modules are not a package: `dashboard/`, `reports/` and `src/scripts/` import each other by inserting their directories into `sys.path` (same in `tests/conftest.py`), so imports look like `import db`, `import app`, `from palette import ...`.

### Cross-cutting conventions

- **Stabilisation cutoff**: analyses only count rows with `last_seen_at < MAX(last_seen_at) - 20 min` (`FRESHNESS_BUFFER_SECONDS`), consistently in `analyze.py`, `app.py` and the report engine. Collection-gap handling differs per consumer (doc § 7.3).
- **Reliability score**: `max(0, % on time (delay ≤ 300 s) − 2 × % skipped stops)`, skipped rate = `SKIPPED / (SCHEDULED + SKIPPED)`. Reading thresholds ≥ 80 / 50–80 / < 50. Rankings sort by ascending score (most problematic first).
- Service day is derived from local `departure_time` for delays and from `start_date` (`YYYYMMDD`) for skipped stops.

## Tests

Tests never touch `data/urban_vision.db`: the `conn` fixture in `tests/conftest.py` builds a temp SQLite DB with the real schema via `db.init_db`, and `tests/gtfs_factory.py` generates synthetic protobuf feeds. After changing tests, re-run the suite and update the test count in both `docs/DOCUMENTATION_TECHNIQUE.md` and `README.md`.

## Production

VM `ek-hub` (SSH alias, treat as read-only), repo at `/home/ubuntu/Urban-Vision`, three systemd units (`urban-vision-collect`, `urban-vision-collect-alerts`, `urban-vision-dashboard`) behind nginx HTTPS. Deployment is manual `git pull` + service restarts (doc § 14–20).
