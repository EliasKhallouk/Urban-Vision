"""Tests de analyze.py : stabilisation (buffer 20 min) et métriques par ligne."""

import logging

import pandas as pd

import analyze
import gtfs_static


def _insert_observation(conn, trip_id, last_seen_at, delay, rel="SCHEDULED",
                        departure_time=None, route_id="A", stop_id="s1"):
    conn.execute(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trip_id, "20260911", route_id, 0, 1, stop_id, rel, None, delay,
         departure_time, last_seen_at),
    )


def _seed_static(conn):
    gtfs_static.create_static_tables(conn)
    conn.execute(
        "INSERT INTO routes (route_id, route_short_name) VALUES ('A', '1')"
    )
    conn.execute(
        "INSERT INTO stops (stop_id, stop_name) VALUES ('s1', 'Arret 1')"
    )
    conn.commit()


class TestLoadCompletedObservations:
    def test_base_vide_renvoie_frame_vide(self, conn):
        df = analyze.load_completed_observations(conn)
        assert df.empty

    def test_exclut_les_20_dernieres_minutes(self, conn):
        _seed_static(conn)
        _insert_observation(conn, "T1", last_seen_at=1_789_000_000, delay=10)  # récent
        _insert_observation(conn, "T2", last_seen_at=1_788_900_000, delay=20)  # stabilisé
        _insert_observation(conn, "T3", last_seen_at=1_788_999_000, delay=30)  # récent
        conn.commit()

        df = analyze.load_completed_observations(conn)
        # cutoff = 1_789_000_000 - 1200 = 1_788_998_800 ; seul T2 est en dessous
        assert df["trip_id"].tolist() == ["T2"]

    def test_les_observations_stabilisees_conservees_avec_leurs_joins(self, conn):
        _seed_static(conn)
        _insert_observation(conn, "T2", last_seen_at=1_788_900_000, delay=20)  # stabilisé
        _insert_observation(conn, "T1", last_seen_at=1_789_000_000, delay=10)  # récent -> exclu
        conn.commit()
        df = analyze.load_completed_observations(conn)
        assert df["trip_id"].tolist() == ["T2"]
        assert df.iloc[0]["route_short_name"] == "1"
        assert df.iloc[0]["stop_name"] == "Arret 1"

    def test_trou_de_collecte_exclut_les_observations_suspectes(self, conn, caplog):
        _seed_static(conn)
        _insert_observation(conn, "T1", last_seen_at=1_789_000_000, delay=10)   # max -> recent
        _insert_observation(conn, "T2", last_seen_at=1_788_998_000, delay=20)   # dans la fenêtre
        _insert_observation(conn, "T3", last_seen_at=1_788_900_000, delay=30)   # hors fenêtre
        conn.execute(
            "INSERT INTO collection_gaps (gap_start, gap_end) VALUES (?, ?)",
            (1_788_990_000, 1_788_999_000),
        )
        conn.commit()

        with caplog.at_level(logging.INFO):
            df = analyze.load_completed_observations(conn)
        assert df["trip_id"].tolist() == ["T3"]
        assert any("Trou de collecte exclu" in r.message for r in caplog.records)


class TestComputeLineStats:
    def _df(self):
        return pd.DataFrame(
            {
                "route_id": ["A", "A", "A", "A", "B", "B"],
                "route_short_name": ["1", "1", "1", "1", "2", "2"],
                "schedule_relationship": [
                    "SCHEDULED", "SCHEDULED", "SCHEDULED", "SKIPPED", "SCHEDULED", "SCHEDULED",
                ],
                "departure_delay": [10.0, 20.0, 400.0, None, 0.0, 600.0],
            }
        )

    def test_metriques_par_ligne(self):
        stats = analyze.compute_line_stats(self._df())
        by_route = stats.set_index("route_id")
        assert by_route.loc["A", "n_observations"] == 3
        assert round(by_route.loc["A", "retard_moyen_s"], 6) == round(430 / 3, 6)
        assert by_route.loc["A", "retard_median_s"] == 20.0
        assert round(by_route.loc["A", "pct_retard_5min"], 6) == round(100 / 3, 6)
        assert by_route.loc["A", "pct_avance_1min"] == 0.0
        assert by_route.loc["A", "n_arrets_sautes"] == 1  # SKIPPED compté à part
        assert by_route.loc["B", "pct_retard_5min"] == 50.0

    def test_trie_par_taux_de_retard_decroissant(self):
        stats = analyze.compute_line_stats(self._df())
        assert stats["route_id"].tolist() == ["B", "A"]

    def test_frame_vide(self):
        df = pd.DataFrame(columns=[
            "route_id", "route_short_name", "schedule_relationship", "departure_delay",
        ])
        stats = analyze.compute_line_stats(df)
        assert stats.empty

    def test_pas_de_retards_mais_des_arrets_sautes(self):
        df = pd.DataFrame(
            {
                "route_id": ["A", "A"],
                "route_short_name": ["1", "1"],
                "schedule_relationship": ["SKIPPED", "SKIPPED"],
                "departure_delay": [None, None],
            }
        )
        stats = analyze.compute_line_stats(df)
        # aucune observation complétée mais le compte des sautés survit
        assert stats.empty


class TestSaveStatsToDb:
    def test_sauvegarde_et_upsert(self, conn):
        df = pd.DataFrame(
            {
                "route_id": ["A", "A"],
                "route_short_name": ["1", "1"],
                "schedule_relationship": ["SCHEDULED", "SCHEDULED"],
                "departure_delay": [10.0, 400.0],
            }
        )
        stats = analyze.compute_line_stats(df)
        analyze.save_stats_to_db(conn, stats, "2026-09-11")
        analyze.save_stats_to_db(conn, stats, "2026-09-11")  # idempotent
        rows = conn.execute(
            "SELECT stat_date, route_id, n_observations, retard_moyen_s, "
            "pct_retard_5min, computed_at FROM daily_line_stats"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][:2] == ("2026-09-11", "A")
        assert rows[0][2] == 2
        assert isinstance(rows[0][-1], int)