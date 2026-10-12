"""Tests de la règle des trajets sans temps réel (au moins deux départs, tous exactement à 0 s)."""

import json
from datetime import datetime

import gtfs_static
import indicateurs as ind
import rafraichir_agregats as ra


def _epoch_local(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


def _insert(conn, rows):
    conn.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
        [(trip, start, route, seq, stop, rel, delay, delay, dep, dep or _epoch_local(2026, 9, 11, 12))
         for trip, start, route, seq, stop, rel, delay, dep in rows],
    )
    conn.commit()


def _seed(conn):
    """Ligne A, 2026-09-11, entre 8 h et 9 h.

    avec_tr : s1 (0) → s2 (+60) → s3 (+120) : zéro isolé, gardé
    sans_tr : s1 (0) → s2 (0) → s3 SKIPPED → s4 (0) : trajet sans temps réel
    seul    : s1 (0) : un seul départ, gardé
    """
    t = lambda m: _epoch_local(2026, 9, 11, 8, m)  # noqa: E731
    _insert(conn, [
        ("avec_tr", "20260911", "A", 1, "s1", "SCHEDULED", 0, t(0)),
        ("avec_tr", "20260911", "A", 2, "s2", "SCHEDULED", 60, t(5)),
        ("avec_tr", "20260911", "A", 3, "s3", "SCHEDULED", 120, t(10)),
        ("sans_tr", "20260911", "A", 1, "s1", "SCHEDULED", 0, t(20)),
        ("sans_tr", "20260911", "A", 2, "s2", "SCHEDULED", 0, t(25)),
        ("sans_tr", "20260911", "A", 3, "s3", "SKIPPED", None, None),
        ("sans_tr", "20260911", "A", 4, "s4", "SCHEDULED", 0, t(35)),
        ("seul", "20260911", "A", 1, "s1", "SCHEDULED", 0, t(40)),
    ])


class TestAgregats:
    def test_trajet_sans_temps_reel_ecarte_des_retards(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        obs, le300, sans_tr, skipped, eligible, hist = conn.execute(
            "SELECT obs, cnt_le300, cnt_sans_tr, skipped, eligible, histogram FROM agg_daily "
            "WHERE date_service = '2026-09-11' AND route_id = 'A'").fetchone()
        assert (obs, le300, sans_tr) == (4, 4, 3)
        assert (skipped, eligible) == (1, 8)
        assert json.loads(hist) == {"0": 2, "60": 1, "120": 1}

    def test_zeros_et_trajets_sans_temps_reel_par_heure(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        assert conn.execute(
            "SELECT heure, obs, cnt_zero, cnt_sans_tr FROM agg_hourly WHERE route_id = 'A'"
        ).fetchone() == (8, 4, 2, 3)
        assert conn.execute(
            "SELECT obs, cnt_zero, cnt_sans_tr FROM agg_hourly_stop WHERE route_id = 'A' AND stop_id = 's1'"
        ).fetchone() == (2, 2, 1)
        assert conn.execute(
            "SELECT obs, cnt_sans_tr, eligible FROM agg_daily_stop WHERE route_id = 'A' AND stop_id = 's4'"
        ).fetchone() == (0, 1, 1)

    def test_trajet_a_cheval_sur_minuit_juge_en_entier(self, conn):
        import db as dbio

        _insert(conn, [
            ("nuit", "20260911", "A", 1, "s1", "SCHEDULED", 0, _epoch_local(2026, 9, 11, 23, 50)),
            ("nuit", "20260911", "A", 2, "s2", "SCHEDULED", 0, _epoch_local(2026, 9, 12, 0, 5)),
            ("nuit", "20260911", "A", 3, "s3", "SCHEDULED", 90, _epoch_local(2026, 9, 12, 0, 15)),
        ])
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        assert conn.execute(
            "SELECT obs, cnt_sans_tr FROM agg_daily WHERE date_service = '2026-09-11'").fetchone() == (1, 0)
        full = conn.execute("SELECT * FROM agg_daily ORDER BY 1").fetchall()
        dbio.refresh_aggregates(conn, days=None)
        assert conn.execute("SELECT * FROM agg_daily WHERE date_service = '2026-09-11'").fetchall() == full

    def test_troncons_sans_le_trajet_sans_temps_reel(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_segments(conn, days=["2026-09-11"])
        obs, pairs, eligible = conn.execute(
            "SELECT obs, pairs, eligible FROM agg_daily_segment WHERE route_id = 'A' AND stop_id = 's2'"
        ).fetchone()
        assert (obs, pairs, eligible) == (1, 1, 2)
        assert conn.execute(
            "SELECT obs, eligible FROM agg_daily_segment WHERE route_id = 'A' AND stop_id = 's4'"
        ).fetchone() == (0, 1)

    def test_qualite_du_jour_compte_tous_les_passages(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        dbio.refresh_quality_days(conn, ["2026-09-11"], "2026-09-12")
        assert conn.execute(
            "SELECT passages FROM quality_days WHERE date_service = '2026-09-11'").fetchone() == (7,)

    def test_ligne_douteuse_compte_les_trajets_sans_temps_reel(self, conn):
        import db as dbio

        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        conn.execute("UPDATE agg_daily SET obs = obs * 100, cnt_sans_tr = cnt_sans_tr * 100, "
                     "histogram = '{\"0\": 200, \"60\": 100, \"120\": 100}'")
        assert ind.doubtful_routes(conn, "2026-09-11", "2026-09-12") == {"A": round(100 * 500 / 700, 1)}

    def test_meme_seuil_que_le_moteur_des_rapports(self):
        import db as dbio

        assert dbio.SANS_TR_MIN_PASSAGES == ind.SANS_TR_MIN_PASSAGES
        assert dbio.TRIP_MARGIN_SECONDS == ind.TRIP_MARGIN_SECONDS


class TestRattrapage:
    def test_jours_passes_recalcules_une_fois(self, conn, monkeypatch):
        import db as dbio

        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        conn.execute("UPDATE agg_daily SET obs = 7, cnt_sans_tr = 0")
        conn.commit()
        monkeypatch.setattr(ra.time, "sleep", lambda s: None)
        assert dbio.sans_tr_pending_days(conn, "2026-09-11") == []
        assert ra.ensure_sans_temps_reel(conn, "2026-09-12") == 1
        assert conn.execute("SELECT obs, cnt_sans_tr FROM agg_daily").fetchone() == (4, 3)
        assert dbio.sans_tr_pending_days(conn, "2026-09-12") == []
        assert ra.ensure_sans_temps_reel(conn, "2026-09-12") == 0


class TestRapports:
    def test_passages_sans_temps_reel_hors_du_rapport(self, conn):
        import generate_monthly_report as report

        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'Ligne 1', 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret 1', 44.8, -0.5)")
        _seed(conn)
        _insert(conn, [("recent", "20260912", "A", 1, "s1", "SCHEDULED", None, None)])
        conn.execute("UPDATE observations SET last_seen_at = ? WHERE trip_id = 'recent'",
                     (_epoch_local(2026, 9, 12, 12),))
        conn.commit()
        scope = report.Scope("test", [], [], "test")
        scheduled, skipped, _ = report.query_observations(conn, "2026-09", scope)
        assert sorted(scheduled["departure_delay"]) == [0, 0, 60, 120]
        assert int(skipped["eligible"].sum()) == 8
        assert conn.execute("SELECT COUNT(*) FROM temp.trajets_sans_tr").fetchone() == (1,)
        report.query_stop_stats(conn, "2026-09", scope)
        assert conn.execute("SELECT COUNT(*) FROM temp.trajets_sans_tr_bornes").fetchone() == (1,)


class TestScore:
    def test_jour_sans_aucune_mesure_hors_du_score(self):
        assert ind.route_day_components(0, 0, 12, 0, sans_tr=12)["expected"] == 0.0
        assert ind.route_day_components(0, 0, 12, 0)["expected"] == 12.0

    def test_composantes_par_arret(self):
        import pandas as pd

        rows = pd.DataFrame({"obs": [0, 10], "cnt_le300": [0, 8], "cnt_lt60": [0, 0], "eligible": [3, 10],
                             "skipped": [0, 0], "cnt_sans_tr": [3, 0]})
        out = ind.add_components(rows)
        assert list(out["expected"]) == [0.0, 10.0]
        assert ind.score_of(out["assured_on_time"].sum(), out["expected"].sum()) == 80.0

    def test_passages_sans_temps_reel_comptent_au_prorata(self):
        comp = ind.route_day_components(8, 6, 12, 0, sans_tr=4)
        assert comp["expected"] == 12
        assert comp["assured_on_time"] == 9.0


class TestLignesSansMesure:
    def test_ligne_desservie_sans_mesure(self, conn):
        import db as dbio
        import app as app_mod

        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('Z', '123', 'Ligne 123', 3)")
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'Ligne 1', 3)")
        t = lambda m: _epoch_local(2026, 9, 11, 9, m)  # noqa: E731
        _insert(conn, [("z1", "20260911", "Z", 1, "s1", "SCHEDULED", 0, t(0)),
                       ("z1", "20260911", "Z", 2, "s2", "SCHEDULED", 0, t(5))])
        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        since, end = _epoch_local(2026, 9, 11, 0), _epoch_local(2026, 9, 12, 0)
        out = app_mod.load_unmeasured_lines.__wrapped__(conn, end, since, end)
        assert list(out["route_id"]) == ["Z"] and list(out["ligne"]) == ["123"]
        at_stop = app_mod.load_unmeasured_lines.__wrapped__(conn, end, since, end, stop_id="s2")
        assert list(at_stop["route_id"]) == ["Z"]

    def test_barre_trace_et_fiche(self):
        import pandas as pd
        import app as app_mod

        ranking = pd.DataFrame({"route_id": ["A"], "ligne": ["1"], "route_type": [3], "score_fiabilite": [82.0]})
        unmeasured = pd.DataFrame({"route_id": ["Z", "A"], "ligne": ["123", "1"], "route_type": [3, 3]})
        options = app_mod.search_options([], ranking, unmeasured, pd.DataFrame(), {}, {})
        assert options["l:Z"] == "Ligne 123 (sans temps réel, non mesurable)"
        assert options["l:A"] == "Ligne 1 (fiable)"
        paths = app_mod.stop_route_paths(ranking, {"Z": {0: [[0, 0], [1, 1]]}, "A": {0: [[0, 0], [2, 2]]}},
                                         {}, unmeasured)
        assert [(p["r"], p["s"], p["e"]) for p in paths] == [
            ("Z", None, "sans temps réel, non mesurable"), ("A", 82, "fiable")]
        assert "ni classée ni notée" in app_mod.unmeasured_line_message("123")


class TestRattrapageParPassage:
    def test_dix_jours_au_plus_puis_reprise(self, conn, monkeypatch):
        import db as dbio

        _seed(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-11"])
        for day in ("2026-09-12", "2026-09-13", "2026-09-14"):
            conn.execute("INSERT INTO agg_daily (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, "
                         "cnt_lt60, skipped, eligible, histogram) VALUES (?, 'A', 0, 0, 0, 0, 0, 0, 0, '{}')", (day,))
        conn.commit()
        monkeypatch.setattr(ra.time, "sleep", lambda s: None)
        assert ra.ensure_sans_temps_reel(conn, "2026-09-20", max_days=3) == 3
        assert dbio.sans_tr_pending_days(conn, "2026-09-20") == ["2026-09-14"]
        assert ra.ensure_sans_temps_reel(conn, "2026-09-20", max_days=3) == 1
        assert ra.ensure_sans_temps_reel(conn, "2026-09-20", max_days=0) == 0

    def test_option_de_la_commande(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(ra, "ensure_sans_temps_reel", lambda conn, today, n: seen.setdefault("n", n))
        for name in ("refresh", "refresh_segments"):
            monkeypatch.setattr(ra, name, lambda conn, now: 0.0)
        for name in ("ensure_segments", "ensure_hourly_early", "ensure_v2_history"):
            monkeypatch.setattr(ra, name, lambda *a: 0)
        ra.main(["--db", str(tmp_path / "x.db"), "--log", str(tmp_path / "x.log"), "--rattrapage-jours", "0"])
        assert seen["n"] == 0
