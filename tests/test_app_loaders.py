"""Tests des loaders cache_data du dashboard (base SQLite temporaire).

les agrégats sont recalculés par le collecteur ; ici on les insère directement
(agg_daily, agg_hourly, service_alerts, stop_municipalities…) pour tester les
requêtes SQL du dashboard sans grosse base réelle.
"""

from datetime import datetime

import pandas as pd
import pytest

import app as app_mod
from assign_stop_municipalities import initialize_tables
import gtfs_static


def _epoch_local(year, month, day, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


@pytest.fixture(autouse=True)
def _purge_streamlit_cache():
    import streamlit as st

    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _seed_routes(conn):
    gtfs_static.create_static_tables(conn)
    conn.execute(
        "INSERT INTO routes (route_id, route_short_name, route_type) VALUES ('A','1',3)"
    )
    conn.commit()


def _seed_agg_daily(conn):
    conn.executemany(
        """INSERT INTO agg_daily
           (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
            skipped, eligible, histogram)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("2026-09-10", "A", 4, 400, 3, 1, 0, 1, 5,
             '{"10":1,"20":1,"300":1,"400":1}'),
            ("2026-09-11", "A", 6, 600, 4, 2, 0, 0, 6,
             '{"10":1,"20":1,"300":1,"400":1,"600":1,"700":1}'),
        ],
    )
    conn.commit()


class TestLoadNetworkData:
    def test_agrege_par_ligne_et_mediane_exacte(self, conn):
        _seed_routes(conn)
        _seed_agg_daily(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 10)
        end = _epoch_local(2026, 9, 12)

        scheduled, skipped = app_mod.load_network_data(conn, cutoff, since, end)
        assert len(scheduled) == 1
        row = scheduled.iloc[0]
        assert row["route_id"] == "A"
        assert row["observations"] == 10
        assert row["retard_moyen_s"] == 100.0
        assert round(row["pct_a_l_heure"], 6) == 70.0
        assert row["retard_median_s"] == 300.0
        assert skipped.iloc[0]["skipped"] == 1

    def test_pas_de_ligne_dans_la_periode_renvoie_vide(self, conn):
        _seed_routes(conn)
        _seed_agg_daily(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 5)
        scheduled, skipped = app_mod.load_network_data(conn, cutoff, since, end)
        assert scheduled.empty
        assert skipped.empty


class TestLoadDistribution:
    def test_histogrammes_repartis_en_classes(self, conn):
        _seed_routes(conn)
        conn.execute(
            """INSERT INTO agg_daily
               (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
                skipped, eligible, histogram)
               VALUES ('2026-09-11', 'A', 6, 0, 3, 2, 0, 0, 6,
                       '{"-700":1,"-30":1,"30":1,"400":2,"1500":1}')"""
        )
        conn.commit()
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)

        dist = app_mod.load_distribution(conn, cutoff, since, end_ts=end)
        assert dist["observations"].sum() == 6
        buckets = dict(zip(dist["plage"], dist["observations"]))
        assert buckets["< −10 min"] == 1      # -700 s
        assert buckets["−1 à 0"] == 1          # -30 s
        assert buckets["0 à +1"] == 1          # +30 s
        assert buckets["+5 à +10"] == 2        # 400 s
        assert buckets["> +20 min"] == 1       # 1500 s
        assert len(dist) == 11  # toutes les classes, même vides


class TestLoadActiveAlerts:
    def test_filtre_les_alertes_actives_a_instant_donne(self, conn):
        _seed_routes(conn)
        conn.executemany(
            """INSERT INTO service_alerts
               (alert_id, route_id, active_period_start, active_period_end,
                header_text, description_text, cause, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("al1", "A", 1_700_000_000, 1_800_000_000, "Travaux ligne 1", "desc", 8, 1_800_000_000),
                ("al2", "A", 1_700_000_000, 1_750_000_000, "Ancienne", "desc", 8, 1_800_000_000),
            ],
        )
        conn.commit()
        now_ts = 1_800_000_000
        df = app_mod.load_active_alerts(conn, now_ts)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["route_id"] == "A"
        assert row["ligne"] == "1"
        assert row["header_text"] == "Travaux ligne 1"
        assert "debut" in df.columns and "fin" in df.columns


class TestLoadCollectionStats:
    def _seed(self, conn):
        conn.executemany(
            """INSERT INTO observations
               (trip_id, start_date, route_id, direction_id, stop_sequence,
                stop_id, schedule_relationship, arrival_delay, departure_delay,
                departure_time, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("T1", "20260911", "A", 0, 1, "s1", "SCHEDULED", None, 30, 1_799_000_000, 1_800_000_000),
                ("T2", "20260911", "A", 0, 1, "s1", "SCHEDULED", None, 400, 1_799_000_000, 1_799_000_000),
                ("T3", "20260911", "A", 0, 1, "s1", "SKIPPED", None, None, None, 1_799_000_000),
            ],
        )
        conn.commit()

    def test_totaux_et_observations_stabilisees(self, conn):
        self._seed(conn)
        stats = app_mod.load_collection_stats(conn)
        assert stats["total"] == 3
        assert stats["trajets"] == 3
        assert stats["lignes"] == 1
        assert stats["analysed"] == 1  # T2 seulement (delay 400, stabilisé)
        assert int(stats["hourly"]["observations"].sum()) == 3


class TestLoadCommunes:
    def test_liste_triee_des_communes(self, conn):
        initialize_tables(conn)
        conn.executemany(
            "INSERT INTO stop_municipalities (stop_id, insee_code, commune_name, "
            "assignment_method, assigned_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", "33200", "Lormont", "point-in-polygon", 1),
                ("s2", "33063", "Ambarès", "point-in-polygon", 1),
            ],
        )
        conn.commit()
        assert app_mod.load_communes(conn) == ["Ambarès", "Lormont"]


class TestLoadPerturbations:
    def _seed(self, conn):
        _seed_routes(conn)
        initialize_tables(conn)
        conn.executemany(
            """INSERT INTO service_alerts
               (alert_id, route_id, active_period_start, active_period_end,
                header_text, description_text, cause, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("a1", "A", 1_785_000_000, 1_788_900_000, "Travaux A", "d", 8, 1_789_000_000),
                ("a2", "", 1_785_000_000, None, "Réseau", "d", 2, 1_789_000_000),
            ],
        )
        # un arrêt de la commune pour le test de filtre
        conn.execute(
            "INSERT INTO stop_municipalities (stop_id, insee_code, commune_name, "
            "assignment_method, assigned_at) VALUES ('s1', '33200', 'Lormont', 'point-in-polygon', 1)"
        )
        conn.execute(
            """INSERT INTO agg_daily_stop
               (date_service, route_id, stop_id, obs, sum_delay, cnt_le300, cnt_gt300,
                cnt_lt60, skipped, eligible, histogram)
               VALUES ('2026-09-11', 'A', 's1', 5, 0, 5, 0, 0, 0, 5, '{}')"""
        )
        conn.commit()

    def test_lignes_perturbees_sur_la_periode(self, conn):
        self._seed(conn)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        cutoff = _epoch_local(2026, 9, 12)
        routes = app_mod.load_disturbed_route_ids(conn, since, end, cutoff)
        assert routes == {"A"}  # la route vide (réseau entier) est exclue

    def test_perturbation_vide_hors_periode(self, conn):
        self._seed(conn)
        since = _epoch_local(2026, 10, 1)
        end = _epoch_local(2026, 10, 5)
        cutoff = _epoch_local(2026, 12, 31)
        routes = app_mod.load_disturbed_route_ids(conn, since, end, cutoff)
        assert routes == set()

    def test_historique_tronque_a_la_periode(self, conn):
        self._seed(conn)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        cutoff = _epoch_local(2026, 9, 12)
        history = app_mod.load_perturbation_history(conn, since, end, cutoff)
        assert {"A", ""} <= set(history["route_id"])
        row = history[history["route_id"] == "A"].iloc[0]
        assert row["ligne"] == "1"
        assert row["jours_couverts"] >= 1
        assert "debut_effectif" in history.columns


class TestEnsureAggregates:
    def test_reconstruit_depuis_observations_quand_vide(self, conn):
        _seed_routes(conn)
        conn.executemany(
            """INSERT INTO observations
               (trip_id, start_date, route_id, direction_id, stop_sequence,
                stop_id, schedule_relationship, arrival_delay, departure_delay,
                departure_time, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("T1", "20260911", "A", 0, 1, "s1", "SCHEDULED", None, 10,
                 _epoch_local(2026, 9, 11, 8, 0), 1_789_000_000),
                ("T1", "20260911", "A", 0, 2, "s2", "SCHEDULED", None, 400,
                 _epoch_local(2026, 9, 11, 8, 5), 1_789_000_000),
            ],
        )
        conn.commit()
        app_mod._ensure_aggregates(conn)
        daily = conn.execute(
            "SELECT obs, sum_delay FROM agg_daily WHERE route_id = 'A'"
        ).fetchall()
        assert len(daily) == 1
        assert daily[0][0] == 2
        directions = conn.execute(
            "SELECT route_id, terminus FROM stop_direction"
        ).fetchall()
        assert any(route == "A" for route, _ in directions)