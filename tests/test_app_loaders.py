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


def _seed_agg_hourly(conn):
    conn.executemany(
        """INSERT INTO agg_hourly
           (date_service, route_id, heure, obs, sum_delay, cnt_le300, cnt_gt300)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            ("2026-09-01", "A", 8, 10, 2000, 9, 1),    # mardi 8h -> Matin
            ("2026-09-01", "A", 13, 8, 1200, 8, 0),    # Journée
            ("2026-09-01", "A", 18, 60, 30000, 30, 30),  # Pointe du soir
            ("2026-09-01", "A", 22, 6, 3000, 3, 3),    # Soirée & nuit
            ("2026-09-01", "A", 3, 2, 1000, 1, 1),     # nuit -> Soirée & nuit
            ("2026-09-05", "A", 11, 20, 8000, 16, 4),  # samedi -> Week-end
        ],
    )
    conn.commit()


class TestLoadPeriodStats:
    def _seed(self, conn):
        _seed_routes(conn)
        _seed_agg_hourly(conn)

    def test_creneaux_combines_jour_et_heure(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        df = app_mod.load_period_stats(conn, cutoff, since, end)
        assert df["période"].tolist() == app_mod.PERIOD_ORDER
        by_period = {r["période"]: r for _, r in df.iterrows()}
        assert by_period["Matin"]["observations"] == 10
        assert round(by_period["Matin"]["pct_retard_5min"], 6) == 10.0
        assert round(by_period["Journée"]["pct_a_l_heure"], 6) == 100.0
        assert by_period["Pointe du soir"]["observations"] == 60
        assert by_period["Soirée & nuit"]["observations"] == 8       # 22h + 3h
        assert by_period["Week-end"]["observations"] == 20

    def test_lignes_classees_par_creneau(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        lines = app_mod.load_period_lines(conn, cutoff, since, end_ts=end, periode="Pointe du soir")
        assert list(lines["ligne"]) == ["1"]
        assert round(lines.iloc[0]["pct_a_l_heure"], 6) == 50.0

    def test_vide_sans_donnees(self, conn):
        _seed_routes(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        assert app_mod.load_period_stats(conn, cutoff, since, end).empty
        assert app_mod.load_period_mode(conn, cutoff, since, end).empty


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


class TestLoadCommuneStats:
    def _seed(self, conn):
        _seed_routes(conn)
        initialize_tables(conn)
        conn.executemany(
            "INSERT INTO stop_municipalities (stop_id, insee_code, commune_name, "
            "assignment_method, assigned_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", "33200", "Lormont", "point-in-polygon", 1),
                ("s2", "33063", "Ambarès", "point-in-polygon", 1),
            ],
        )
        conn.executemany(
            """INSERT INTO agg_daily_stop
               (date_service, route_id, stop_id, obs, sum_delay, cnt_le300, cnt_gt300,
                cnt_lt60, skipped, eligible, histogram)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("2026-09-11", "A", "s1", 100, 3000, 90, 10, 0, 0, 100, '{}'),
                ("2026-09-11", "A", "s2", 100, 20000, 70, 30, 0, 10, 100, '{}'),
            ],
        )
        conn.commit()

    def test_classement_et_score_pondere_par_saute(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        df = app_mod.load_commune_stats(conn, cutoff, since, end)
        assert list(df["commune"]) == ["Ambarès", "Lormont"]
        assert [round(v, 6) for v in df["score_fiabilite"]] == [50.0, 90.0]
        row = df[df["commune"] == "Ambarès"].iloc[0]
        assert round(row["pct_arrets_sautes"], 6) == 10.0
        assert round(row["retard_moyen_s"], 6) == 200.0
        assert int(row["n_lignes"]) == 1

    def test_vide_sans_donnees(self, conn):
        _seed_routes(conn)
        initialize_tables(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        assert app_mod.load_commune_stats(conn, cutoff, since, end).empty


class TestLoadTerritorial:
    def _seed(self, conn):
        import db as dbio

        _seed_routes(conn)
        conn.execute(
            "INSERT INTO routes (route_id, route_short_name, route_type) VALUES ('T','A',0)"
        )
        conn.executemany(
            "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES (?, ?, ?, ?)",
            [("s1", "Mairie", 44.84, -0.57), ("s2", "Gare", 44.83, -0.56)],
        )
        conn.executescript(dbio.STOP_DIRECTION_DDL)
        conn.execute(
            "INSERT INTO stop_direction (route_id, stop_id, direction_id, terminus) "
            "VALUES ('T', 's1', 0, 'Aéroport')"
        )
        conn.executemany(
            """INSERT INTO agg_daily_stop
               (date_service, route_id, stop_id, obs, sum_delay, cnt_le300, cnt_gt300,
                cnt_lt60, skipped, eligible, histogram)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("2026-09-11", "T", "s1", 300, 0, 285, 15, 0, 15, 300, '{}'),
                ("2026-09-11", "A", "s1", 100, 0, 75, 25, 0, 5, 100, '{}'),
                ("2026-09-11", "A", "s2", 80, 0, 80, 0, 0, 0, 80, '{}'),
            ],
        )
        conn.commit()

    def test_score_de_l_arret_integre_les_arrets_sautes(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        df = app_mod.load_territorial(conn, cutoff, _epoch_local(2026, 9, 1), _epoch_local(2026, 9, 12))
        s1 = df[df["stop_id"] == "s1"].iloc[0]
        assert s1["observations"] == 400
        assert s1["pct_a_l_heure"] == 90.0
        assert s1["pct_arrets_sautes"] == 5.0
        assert s1["score_fiabilite"] == 80.0
        assert s1["lignes"] == "1, A"

    def test_ligne_principale_donne_le_mode_et_la_direction(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        df = app_mod.load_territorial(conn, cutoff, _epoch_local(2026, 9, 1), _epoch_local(2026, 9, 12))
        s1 = df[df["stop_id"] == "s1"].iloc[0]
        assert s1["route_id"] == "T"
        assert int(s1["route_type"]) == 0
        assert s1["direction"] == "vers Aéroport"
        s2 = df[df["stop_id"] == "s2"].iloc[0]
        assert int(s2["route_type"]) == 3
        assert s2["score_fiabilite"] == 100.0


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


class TestLoadEngagement:
    def _seed(self, conn):
        _seed_routes(conn)
        conn.execute(
            "INSERT INTO routes (route_id, route_short_name, route_type) VALUES ('B','2',3), ('C','3',3)"
        )
        conn.executemany(
            """INSERT INTO agg_daily
               (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60,
                skipped, eligible, histogram)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("2026-09-01", "A", 100, 20000, 80, 20, 0, 0, 100, '{}'),
                ("2026-09-02", "A", 100, 20000, 80, 20, 0, 0, 100, '{}'),
                ("2026-09-03", "A", 100, 40000, 60, 40, 0, 0, 100, '{}'),
                ("2026-09-04", "A", 100, 40000, 60, 40, 0, 0, 100, '{}'),
                ("2026-09-01", "B", 100, 50000, 50, 50, 0, 0, 100, '{}'),
                ("2026-09-02", "B", 100, 50000, 50, 50, 0, 0, 100, '{}'),
                ("2026-09-03", "B", 100, 10000, 90, 10, 0, 0, 100, '{}'),
                ("2026-09-04", "B", 100, 10000, 90, 10, 0, 0, 100, '{}'),
                ("2026-09-03", "C", 100, 30000, 70, 30, 0, 0, 100, '{}'),
                ("2026-09-04", "C", 100, 30000, 70, 30, 0, 0, 100, '{}'),
            ],
        )
        conn.commit()

    def test_tendance_quotidienne_du_reseau(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        df = app_mod.load_engagement_trend(conn, cutoff, since, end_ts=end)
        assert len(df) == 4
        row = df[df["date_service"] == pd.Timestamp("2026-09-01")].iloc[0]
        assert row["observations"] == 200
        assert round(row["pct_a_l_heure"], 6) == 65.0
        assert round(row["pct_retard_5min"], 6) == 35.0
        assert round(row["retard_moyen_s"], 6) == 350.0

    def test_progression_par_rapport_a_la_periode_precedente(self, conn):
        self._seed(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since, end = _epoch_local(2026, 9, 3), _epoch_local(2026, 9, 5)
        prev_since, prev_end = _epoch_local(2026, 9, 1), _epoch_local(2026, 9, 3)
        df = app_mod.load_engagement_progression(conn, cutoff, since, end, prev_since, prev_end)
        assert list(df["ligne"]) == ["1", "2"]  # déclin d'abord, puis progression
        a = df[df["ligne"] == "1"].iloc[0]
        assert a["score_fiabilite_prev"] == 80.0
        assert a["score_fiabilite"] == 60.0
        assert a["delta_score"] == -20.0
        b = df[df["ligne"] == "2"].iloc[0]
        assert b["score_fiabilite_prev"] == 50.0
        assert b["score_fiabilite"] == 90.0
        assert b["delta_score"] == 40.0
        assert "C" not in df["route_id"].tolist()

    def test_vide_sans_donnees(self, conn):
        _seed_routes(conn)
        cutoff = _epoch_local(2026, 9, 12)
        since = _epoch_local(2026, 9, 1)
        end = _epoch_local(2026, 9, 12)
        assert app_mod.load_engagement_trend(conn, cutoff, since, end).empty
        assert app_mod.load_engagement_progression(conn, cutoff, since, end, since, end).empty
        assert app_mod.load_engagement_progression(conn, cutoff, since, end, None, None).empty

class TestLoadersDiagnostic:
    def _seed(self, conn):
        import db as dbio

        _seed_routes(conn)
        initialize_tables(conn)
        conn.executemany(
            "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES (?, ?, 44.8, -0.6)",
            [("s1", "Gare"), ("s2", "Mairie"), ("s3", "Parc")],
        )
        conn.executemany(
            "INSERT INTO stop_municipalities (stop_id, insee_code, commune_name, assignment_method, "
            "assigned_at) VALUES (?, ?, ?, 'point-in-polygon', 1)",
            [("s1", "33063", "Bordeaux"), ("s2", "33281", "Mérignac"), ("s3", "33281", "Mérignac")],
        )
        conn.executescript(dbio.STOP_DIRECTION_DDL)
        conn.executemany(
            "INSERT INTO stop_direction (route_id, stop_id, direction_id, terminus) VALUES ('A', ?, 0, 'Parc')",
            [("s1",), ("s2",), ("s3",)],
        )
        conn.executemany(
            """INSERT INTO observations
               (trip_id, start_date, route_id, direction_id, stop_sequence, stop_id,
                schedule_relationship, arrival_delay, departure_delay, departure_time, last_seen_at)
               VALUES (?, '20260911', 'A', 0, ?, ?, ?, NULL, ?, ?, 0)""",
            [
                ("t1", 1, "s1", "SCHEDULED", 30, _epoch_local(2026, 9, 11, 8, 0)),
                ("t1", 2, "s2", "SCHEDULED", 150, _epoch_local(2026, 9, 11, 8, 5)),
                ("t1", 3, "s3", "SCHEDULED", 400, _epoch_local(2026, 9, 11, 8, 10)),
                ("t2", 1, "s1", "SCHEDULED", 0, _epoch_local(2026, 9, 11, 9, 0)),
                ("t2", 2, "s2", "SKIPPED", None, None),
                ("t2", 3, "s3", "SCHEDULED", 60, _epoch_local(2026, 9, 11, 9, 10)),
            ],
        )
        conn.executemany(
            "INSERT INTO trip_status (trip_id, start_date, route_id, schedule_relationship, last_seen_at) "
            "VALUES (?, ?, 'A', ?, ?)",
            [("t1", "20260911", "SCHEDULED", 10), ("t2", "20260911", "SCHEDULED", 10),
             ("t3", "20260911", "CANCELED", 10), ("t4", "20260911", "CANCELED", 2_000_000_000),
             ("t5", "20260820", "CANCELED", 10)],
        )
        conn.commit()
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        dbio.refresh_segments(conn, days=["2026-09-11"])

    def _bounds(self):
        return _epoch_local(2026, 9, 12), _epoch_local(2026, 9, 1), _epoch_local(2026, 9, 12)

    def test_profil_de_ligne_ordonne_avec_troncons(self, conn):
        self._seed(conn)
        cutoff, since, end = self._bounds()
        prof = app_mod.load_route_segments(conn, cutoff, since, end, "A")
        assert list(prof["stop_name"]) == ["Gare", "Mairie", "Parc"]
        parc = prof[prof["stop_id"] == "s3"].iloc[0]
        assert parc["prev_stop_name"] == "Gare"
        assert parc["pairs"] == 2
        assert parc["carried_s"] == pytest.approx((150 + 0) / 2)
        assert parc["gain_s"] == pytest.approx((250 + 60) / 2)
        assert parc["commune"] == "Mérignac"
        assert parc["terminus"] == "vers Parc"
        mairie = prof[prof["stop_id"] == "s2"].iloc[0]
        assert (mairie["skipped"], mairie["eligible"]) == (1, 2)

    def test_segments_disponibles(self, conn):
        assert app_mod.segments_available(conn) is False
        self._seed(conn)
        import streamlit as st
        st.cache_data.clear()
        assert app_mod.segments_available(conn) is True

    def test_courses_supprimees_bornees_a_la_periode_et_stabilisees(self, conn):
        self._seed(conn)
        cutoff, since, end = self._bounds()
        canc = app_mod.load_line_cancellations(conn, cutoff, since, end, "A")
        assert len(canc) == 1
        assert (int(canc["cancelled"].iloc[0]), int(canc["trips"].iloc[0])) == (1, 3)

    def test_arrets_de_la_ligne_classes_par_impact(self, conn):
        self._seed(conn)
        cutoff, since, end = self._bounds()
        stops = app_mod.load_line_stops(conn, cutoff, since, end, "A")
        assert stops.iloc[0]["stop_id"] in {"s2", "s3"}
        s3 = stops[stops["stop_id"] == "s3"].iloc[0]
        assert (s3["cnt_gt300"], s3["direction"]) == (1, "vers Parc")
        assert s3["score_fiabilite"] == 50.0

    def test_compteurs_d_un_arret_et_table_des_lignes(self, conn):
        self._seed(conn)
        cutoff, since, end = self._bounds()
        daily = app_mod.load_stop_daily(conn, cutoff, since, end, "s3")
        lines = app_mod.stop_lines_table(daily)
        row = lines.iloc[0]
        assert (row["observations"], row["cnt_gt300"]) == (2, 1)
        assert row["retard_median_s"] == 230.0
        assert row["mode"] == "Bus"
        hourly = app_mod.load_stop_hourly(conn, cutoff, since, end, "s3")
        assert sorted(hourly["heure"]) == [8, 9]


class TestAidesFiche:
    def test_score_reseau_pondere(self):
        ranking = pd.DataFrame({"observations": [100, 300], "pct_a_l_heure": [80.0, 100.0],
                                "skipped": [5, 5], "eligible": [100, 400]})
        assert app_mod.network_score(ranking) == pytest.approx(95.0 - 2 * 2.0)

    def test_libelles_de_recherche(self):
        df = pd.DataFrame({"stop_id": ["s1", "s2"], "stop_name": ["Gare", "Parc"],
                           "direction": ["vers Parc", ""], "lignes": ["1, 11", "A"]})
        assert app_mod.stop_labels(df) == {"s1": "Gare — vers Parc (1, 11)", "s2": "Parc (A)"}

    def test_alertes_qui_recoupent_un_jour_degrade(self):
        history = pd.DataFrame({
            "route_id": ["A", "A", "B"], "ligne": ["1", "1", "2"],
            "header_text": ["Travaux", "Grève", "Autre"],
            "debut_effectif": ["01/09/2026 06:00", "10/09/2026 06:00", "01/09/2026 06:00"],
            "fin_effective": ["03/09/2026 20:00", "11/09/2026 20:00", "30/09/2026 20:00"],
        })
        out = app_mod._alerts_overlapping(history, {"A"}, [pd.Timestamp("2026-09-10")])
        assert list(out["header_text"]) == ["Grève", "Travaux"]
        assert list(out["jour_degrade"]) == [True, False]

    def test_repartition_en_classes(self):
        dist = app_mod.distribution_from_hists([{"30": 2, "400": 1}, {"-700": 1}])
        counts = dict(zip(dist["plage"], dist["observations"]))
        assert counts["0 à +1"] == 2
        assert counts["+5 à +10"] == 1
        assert counts["< −10 min"] == 1


class TestLoadRouteHourlyStops:
    def test_agregats_horaires_de_la_ligne_par_arret(self, conn):
        conn.executemany(
            "INSERT INTO agg_hourly_stop (date_service, route_id, stop_id, heure, obs, sum_delay, cnt_le300, "
            "cnt_gt300) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [("2026-09-11", "A", "s1", 13, 4, 800, 2, 2), ("2026-09-11", "B", "s1", 13, 3, 30, 3, 0),
             ("2026-08-01", "A", "s1", 13, 9, 0, 9, 0)],
        )
        conn.commit()
        df = app_mod.load_route_hourly_stops(conn, _epoch_local(2026, 9, 12), _epoch_local(2026, 9, 1),
                                             _epoch_local(2026, 9, 12), "A")
        assert df[["stop_id", "heure", "obs", "sum_delay", "cnt_gt300"]].values.tolist() == [["s1", 13, 4, 800, 2]]
