"""Tests de la méthode 2.0 : agrégats (db.py), indicateurs, historique et rapport."""

import json
import math
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

import gtfs_static
import indicateurs as ind


def _epoch_local(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


def _insert_observations(conn, rows):
    conn.executemany(
        """INSERT INTO observations
           (trip_id, start_date, route_id, direction_id, stop_sequence,
            stop_id, schedule_relationship, arrival_delay, departure_delay,
            departure_time, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def _passages(day, stop, scheduled_minutes, delays, route="R", hour=8):
    start_date = f"202609{day:02d}"
    rows = []
    for i, (minute, delay) in enumerate(zip(scheduled_minutes, delays)):
        sched = _epoch_local(2026, 9, day, hour, minute)
        rows.append((f"{route}{stop}{day}_{i}", start_date, route, 0, 1, stop, "SCHEDULED",
                     delay, delay, sched + delay, sched + delay + 60))
    return rows


def _trip_status(conn, rows):
    conn.executemany(
        "INSERT INTO trip_status (trip_id, start_date, route_id, schedule_relationship, last_seen_at) "
        "VALUES (?, ?, ?, ?, 0)",
        rows,
    )
    conn.commit()


def _seed_regularity(conn):
    _insert_observations(conn, _passages(14, "s1", [0, 10, 20, 30, 40, 50], [0, 0, 300, 0, 0, 0]))
    _insert_observations(conn, _passages(14, "s2", [0, 15, 30, 45], [0, 0, 0, 0]))
    _insert_observations(conn, _passages(15, "s1", [0, 10, 20, 30, 40, 50], [60, 60, 60, 60, 60, 60]))


def _seed_trips(conn):
    _trip_status(conn, [
        ("a1", "20260914", "R", "SCHEDULED"),
        ("a2", "20260914", "R", "SCHEDULED"),
        ("a3", "20260914", "R", "CANCELED"),
        ("a4", "20260914", "R", "DELETED"),
        ("a5", "20260914", "R", "NEW"),
        ("a6", "20260914", "R", "DUPLICATED"),
        ("b1", "20260915", "R", "SCHEDULED"),
        ("c1", "20260914", "Q", "SCHEDULED"),
    ])


def _v2_dump(conn, days):
    import db as dbio

    in_days = "', '".join(days)
    return {
        table: conn.execute(
            f"SELECT * FROM {table} WHERE date_service IN ('{in_days}') ORDER BY 1, 2, 3, 4"
        ).fetchall()
        for table in dbio.V2_TABLES
    }


class TestAgregatsV2:
    def test_index_piege_supprime_a_l_import(self, conn):
        conn.execute("CREATE INDEX idx_observations_sched_delay ON observations(schedule_relationship, departure_delay)")
        import db as dbio

        dbio.init_db(conn)
        names = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "idx_observations_sched_delay" not in names
        assert "idx_observations_departure_time" in names

    def test_courses_prevues_supprimees_ajoutees(self, conn):
        import db as dbio

        _seed_trips(conn)
        dbio.refresh_v2(conn, days=["2026-09-14"])
        rows = conn.execute(
            "SELECT date_service, route_id, scheduled, cancelled, added FROM agg_daily_trips ORDER BY 1, 2"
        ).fetchall()
        assert rows == [("2026-09-14", "Q", 1, 0, 0), ("2026-09-14", "R", 2, 2, 2)]

    def test_intervalles_reels_et_prevus(self, conn):
        import db as dbio

        _seed_regularity(conn)
        dbio.refresh_v2(conn, days=["2026-09-14"])
        rows = conn.execute(
            "SELECT stop_id, heure, n_act, sum_h_act, sum_h2_act, n_sch, sum_h_sch, sum_h2_sch "
            "FROM agg_hourly_regularity WHERE date_service = '2026-09-14'"
        ).fetchall()
        assert rows == [("s1", 8, 5, 3000, 600**2 + 900**2 + 300**2 + 600**2 + 600**2, 5, 3000, 5 * 600**2)]

    def test_intervalle_avec_la_veille_au_soir_pris_en_compte(self, conn):
        import db as dbio

        _insert_observations(conn, _passages(14, "s1", [55], [0], hour=23)
                             + _passages(15, "s1", [5, 15, 25, 35, 45], [0] * 5, hour=0))
        dbio.refresh_v2(conn, days=["2026-09-15"])
        n_sch, sum_h_sch = conn.execute(
            "SELECT n_sch, sum_h_sch FROM agg_hourly_regularity WHERE date_service = '2026-09-15'"
        ).fetchone()
        assert (n_sch, sum_h_sch) == (5, 600 + 4 * 600)

    def test_plan_ne_lit_observations_que_par_l_index_des_departs(self, conn):
        import db as dbio

        for sql, params in dbio.v2_statements(["2026-09-14", "2026-09-15"]):
            accesses = [
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                if row[3].startswith(("SCAN o", "SEARCH o"))
            ]
            for access in accesses:
                assert "idx_observations_departure_time" in access, access

    def test_incremental_identique_au_calcul_complet(self, conn):
        import db as dbio

        days = ["2026-09-14", "2026-09-15"]
        _seed_regularity(conn)
        _seed_trips(conn)
        dbio.refresh_v2(conn, days=None)
        full = _v2_dump(conn, days)
        for table in dbio.V2_TABLES:
            conn.execute(f"DELETE FROM {table}")
        dbio.refresh_v2(conn, days=days)
        assert _v2_dump(conn, days) == full
        assert all(full[table] for table in dbio.V2_TABLES)

    def test_refresh_aggregates_calcule_aussi_la_methode_2(self, conn):
        import db as dbio

        _seed_regularity(conn)
        _seed_trips(conn)
        dbio.refresh_aggregates(conn, days=["2026-09-14"])
        assert conn.execute("SELECT COUNT(*) FROM agg_daily_trips").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM agg_hourly_regularity").fetchone()[0] == 1
        assert conn.execute("SELECT flag FROM quality_days WHERE date_service = '2026-09-14'").fetchone() == ("non_evalue",)


def _hourly(value, hours=range(5, 24)):
    return {h: value for h in hours}


class TestQualiteDesJours:
    def test_non_evalue_sans_deux_semaines_de_reference(self):
        hourly = {"2026-09-07": _hourly(1000), "2026-09-14": _hourly(1000)}
        assert __import__("db").day_quality(hourly, "2026-09-14") == ("non_evalue", [])

    def test_ok_degrade_incomplet(self):
        import db as dbio

        refs = {"2026-09-07": _hourly(1000), "2026-08-31": _hourly(1200), "2026-08-24": _hourly(800)}
        assert dbio.day_quality({**refs, "2026-09-14": _hourly(900)}, "2026-09-14") == ("ok", [])
        one_hole = {**_hourly(900), 8: 100}
        assert dbio.day_quality({**refs, "2026-09-14": one_hole}, "2026-09-14") == ("degrade", [8])
        three_holes = {**_hourly(900), 8: 100, 9: 0, 17: 400}
        assert dbio.day_quality({**refs, "2026-09-14": three_holes}, "2026-09-14") == ("incomplet", [8, 9, 17])

    def test_heures_creuses_ignorees(self):
        import db as dbio

        refs = {"2026-09-07": {**_hourly(1000), 5: 300}, "2026-08-31": {**_hourly(1000), 5: 300}}
        current = {**_hourly(1000), 5: 0}
        assert dbio.day_quality({**refs, "2026-09-14": current}, "2026-09-14") == ("ok", [])

    def test_mediane_de_deux_references(self):
        import db as dbio

        refs = {"2026-09-07": _hourly(1000), "2026-08-31": _hourly(600)}
        assert dbio.day_quality({**refs, "2026-09-14": _hourly(399)}, "2026-09-14")[0] == "incomplet"
        assert dbio.day_quality({**refs, "2026-09-14": _hourly(400)}, "2026-09-14")[0] == "ok"

    def test_jour_courant_ignore_et_jour_vide_incomplet(self, conn):
        import db as dbio

        for day in ("2026-08-31", "2026-09-07", "2026-09-15"):
            conn.executemany(
                "INSERT INTO agg_hourly (date_service, route_id, heure, obs, sum_delay, cnt_le300, cnt_gt300) "
                "VALUES (?, 'R', ?, 1000, 0, 1000, 0)",
                [(day, h) for h in range(5, 24)],
            )
        dbio.refresh_quality_days(conn, ["2026-09-14", "2026-09-15"], today="2026-09-15")
        rows = conn.execute("SELECT date_service, passages, flag, lacunar_hours FROM quality_days").fetchall()
        assert rows == [("2026-09-14", 0, "incomplet", json.dumps(list(range(5, 24))))]


class TestFonctionsPures:
    def test_ratio_constant_marge_nulle(self):
        score, margin = ind.ratio_estimate([50, 100, 25], [100, 200, 50])
        assert score == pytest.approx(50.0)
        assert margin == pytest.approx(0.0)

    def test_ratio_marge_student(self):
        ys, xs = [80, 60], [100, 100]
        score, margin = ind.ratio_estimate(ys, xs)
        variance = 2 / 1 * ((80 - 70) ** 2 + (60 - 70) ** 2) / 200 ** 2
        assert score == pytest.approx(70.0)
        assert margin == pytest.approx(100 * 12.706 * math.sqrt(variance))

    def test_ratio_un_jour_ou_vide(self):
        assert ind.ratio_estimate([5], [10]) == (50.0, None)
        assert ind.ratio_estimate([], []) == (None, None)

    def test_quantile_de_student(self):
        assert ind.student_quantile(2) == 12.706
        assert ind.student_quantile(31) == 2.042
        assert ind.student_quantile(60) == ind.Z95

    def test_courses_supprimees_nettes_des_ajouts(self):
        comp = ind.route_day_components(obs=90, on_time=72, eligible=100, skipped=10,
                                        cancelled=3, added=1, scheduled=9, early=5)
        assert comp["expected"] == pytest.approx(100 + 2 * 100 / 10)
        assert comp["assured"] == 90
        assert comp["assured_on_time"] == pytest.approx(72)
        assert comp["early"] == 5

    def test_suppression_compensee_non_comptee(self):
        comp = ind.route_day_components(obs=50, on_time=50, eligible=50, skipped=0, cancelled=4, added=4, scheduled=0)
        assert comp["expected"] == 50

    def test_combine_exclut_les_jours(self):
        days = {
            "2026-09-14": ind.route_day_components(100, 80, 110, 10, early=10),
            "2026-09-15": ind.route_day_components(100, 0, 100, 0),
        }
        result = ind.combine(days, excluded=["2026-09-15"], discarded={"D": 100.0})
        assert result.jours == 1
        assert result.jours_exclus == ["2026-09-15"]
        assert result.score == pytest.approx(100 * 80 / 110)
        assert result.ponctualite == pytest.approx(80.0)
        assert result.service == pytest.approx(100 * 100 / 110)
        assert result.avance == pytest.approx(10.0)
        assert result.lignes_ecartees == {"D": 100.0}
        assert result.marge is None

    def test_combine_sans_jour(self):
        assert not ind.combine({}).disponible

    def test_comparaison_significative(self):
        a = ind.Indicateurs(score=80.0, marge=3.0)
        assert ind.compare(a, ind.Indicateurs(score=75.0, marge=4.0))["significatif"] is False
        change = ind.compare(a, ind.Indicateurs(score=74.0, marge=4.0))
        assert change["significatif"] is True
        assert change["ecart"] == pytest.approx(6.0)
        assert change["marge"] == pytest.approx(5.0)
        assert ind.compare(a, ind.Indicateurs(score=74.0))["significatif"] is None
        assert ind.compare(a, ind.Indicateurs()) is None

    def test_attente_moyenne(self):
        reg = ind.regularity(sum_h2_act=600**2 + 900**2 + 300**2, sum_h_act=1800,
                             sum_h2_sch=3 * 600**2, sum_h_sch=1800)
        assert reg["attente_prevue"] == pytest.approx(300.0)
        assert reg["attente_reelle"] == pytest.approx((600**2 + 900**2 + 300**2) / 3600)
        assert reg["attente_excedentaire"] == pytest.approx(reg["attente_reelle"] - 300.0)
        assert ind.regularity(0, 0, 10, 10) is None

    def test_type_de_jour(self):
        assert [ind.day_type(d) for d in ("2026-09-14", "2026-09-19", "2026-09-20")] == ["semaine", "samedi", "dimanche"]


def _daily(conn, day, route, obs, le300, lt60, eligible, skipped, histogram=None):
    conn.execute(
        "INSERT INTO agg_daily (date_service, route_id, obs, sum_delay, cnt_le300, cnt_gt300, cnt_lt60, "
        "skipped, eligible, histogram) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
        (day, route, obs, le300, obs - le300, lt60, skipped, eligible, json.dumps(histogram or {"30": obs})),
    )


@pytest.fixture
def v2_db(conn):
    gtfs_static.create_static_tables(conn)
    conn.executemany("INSERT INTO routes VALUES (?, ?, ?, 3)", [
        ("A", "1", "Ligne 1"), ("B", "2", "Ligne 2"), ("F", "Flex", "Flex'Night Nord"), ("D", "9", "Ligne 9"),
    ])
    _daily(conn, "2026-09-14", "A", 100, 90, 10, 110, 10)
    _daily(conn, "2026-09-15", "A", 100, 80, 0, 100, 0)
    _daily(conn, "2026-09-19", "A", 40, 40, 0, 40, 0)
    _daily(conn, "2026-09-14", "B", 50, 50, 0, 50, 0)
    _daily(conn, "2026-09-14", "F", 500, 0, 0, 500, 0)
    _daily(conn, "2026-09-14", "D", 300, 300, 0, 300, 0, {"0": 300})
    conn.execute("INSERT INTO agg_daily_trips VALUES ('2026-09-15', 'A', 9, 1, 0)")
    conn.execute("INSERT INTO quality_days VALUES ('2026-09-15', 0, '[8, 9, 10]', 'incomplet', 0)")
    conn.execute("INSERT INTO quality_days VALUES ('2026-09-19', 0, '[8]', 'degrade', 0)")
    conn.executemany(
        "INSERT INTO agg_hourly_regularity VALUES (?, 'A', 0, 's1', 8, 5, ?, ?, 5, 3000, ?)",
        [("2026-09-14", 3000, 2_000_000, 1_800_000), ("2026-09-15", 3000, 9_000_000, 1_800_000)],
    )
    conn.commit()
    return conn


class TestIndicateursSurLaBase:
    def test_reseau_hors_flex_hors_jour_incomplet_hors_temps_reel_douteux(self, v2_db):
        result = ind.indicators(v2_db, "2026-09-14", "2026-09-16")
        assert result.jours == 1
        assert result.jours_exclus == ["2026-09-15"]
        assert result.lignes_ecartees == {"D": 100.0}
        assert result.score == pytest.approx(100 * 130 / 160)
        assert result.ponctualite == pytest.approx(100 * 130 / 150)
        assert result.service == pytest.approx(100 * 150 / 160)
        assert result.passages == 150

    def test_courses_supprimees_dans_les_attendus(self, v2_db):
        v2_db.execute("DELETE FROM quality_days")
        result = ind.indicators(v2_db, "2026-09-14", "2026-09-16", routes=["A"])
        assert result.jours == 2
        assert result.attendus == pytest.approx(110 + 100 + 100 / 9)
        assert result.score == pytest.approx(100 * (80 + 80) / (110 + 100 + 100 / 9))
        assert result.lignes_ecartees == {}

    def test_par_type_de_jour(self, v2_db):
        result = ind.indicators(v2_db, "2026-09-01", "2026-10-01", day_types=["samedi"])
        assert result.jours == 1
        assert result.score == pytest.approx(100.0)

    def test_par_ligne(self, v2_db):
        lines = ind.route_indicators(v2_db, "2026-09-14", "2026-09-16", min_passages=0).set_index("route_id")
        assert sorted(lines.index) == ["A", "B", "D"]
        assert bool(lines.loc["D", "temps_reel_douteux"]) is True
        assert lines.loc["A", "part_avance"] == pytest.approx(10.0)
        assert lines.loc["A", "jours"] == 1
        assert lines.loc["A", "attente_excedentaire_s"] == pytest.approx(2_000_000 / 6000 - 300)
        assert ind.route_indicators(v2_db, "2026-09-14", "2026-09-16").empty is False
        assert "B" not in set(ind.route_indicators(v2_db, "2026-09-14", "2026-09-16", min_passages=60)["route_id"])

    def test_regularite_hors_jour_incomplet(self, v2_db):
        reg = ind.route_regularity(v2_db, "2026-09-14", "2026-09-16")
        assert reg["A"]["jours"] == 1
        assert reg["A"]["attente_prevue"] == pytest.approx(300.0)

    def test_drapeaux_de_qualite(self, v2_db):
        assert ind.quality_flags(v2_db, "2026-09-01", "2026-10-01") == {
            "2026-09-15": "incomplet", "2026-09-19": "degrade"}

    def test_base_sans_tables_v2(self, tmp_path):
        bare = sqlite3.connect(tmp_path / "vide.db")
        assert ind.excluded_days(bare, "2026-09-01", "2026-10-01") == []
        assert ind.quality_flags(bare, "2026-09-01", "2026-10-01") == {}
        assert ind.route_regularity(bare, "2026-09-01", "2026-10-01") == {}


class TestHistoriqueV2:
    def test_calcule_les_jours_manquants_une_seule_fois(self, conn):
        import db as dbio
        import rafraichir_agregats as ra

        _seed_regularity(conn)
        _seed_trips(conn)
        dbio.refresh_aggregates(conn, days=None)
        conn.execute("DELETE FROM agg_daily_trips")
        conn.execute("DELETE FROM agg_hourly_regularity")
        conn.execute("DELETE FROM quality_days")
        conn.commit()
        assert ra.ensure_v2_history(conn, "2026-10-01") == 2
        assert conn.execute("SELECT COUNT(*) FROM agg_daily_trips").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM quality_days").fetchone()[0] == 2
        assert ra.ensure_v2_history(conn, "2026-10-01") == 0

    def test_jour_sans_course_connue_non_recalcule(self, conn):
        import db as dbio
        import rafraichir_agregats as ra

        _seed_regularity(conn)
        dbio.refresh_aggregates(conn, days=None)
        conn.execute("DELETE FROM quality_days")
        conn.commit()
        assert ra.ensure_v2_history(conn, "2026-10-01") == 2
        assert conn.execute("SELECT COUNT(*) FROM agg_daily_trips").fetchone()[0] == 0
        assert ra.ensure_v2_history(conn, "2026-10-01") == 0

    def test_jour_courant_jamais_marque(self, conn):
        import db as dbio
        import rafraichir_agregats as ra

        _seed_regularity(conn)
        dbio.refresh_aggregates(conn, days=None)
        conn.execute("DELETE FROM quality_days")
        conn.commit()
        assert ra.ensure_v2_history(conn, "2026-09-15") == 1
        assert [d for (d,) in conn.execute("SELECT date_service FROM quality_days")] == ["2026-09-14"]


class TestRapportV2:
    def _v2(self):
        current = ind.Indicateurs(score=75.1, marge=1.2, ponctualite=78.4, marge_ponctualite=0.9, service=95.9,
                                  avance=6.1, passages=1000, attendus=1100.0, jours=28,
                                  jours_exclus=["2026-09-08", "2026-09-24"], lignes_ecartees={"102": 100.0})
        before = ind.Indicateurs(score=78.1, marge=1.2, jours=21)
        return {
            "month": "2026-09",
            "current": current,
            "by_day_type": [
                {"label": "Jours de semaine", "current": current, "previous": before, "change": ind.compare(current, before)},
                {"label": "Samedis", "current": ind.Indicateurs(), "previous": before, "change": None},
            ],
            "lines": pd.DataFrame([
                {"route_id": "S35", "score_v2": 18.6, "marge_v2": 11.0, "ponctualite_stricte": 18.9,
                 "part_avance": 70.2, "service_assure": 100.0, "passages": 371, "jours": 20,
                 "attente_excedentaire_s": None, "temps_reel_douteux": False, "part_retards_nuls": None},
                {"route_id": "102", "score_v2": 99.0, "marge_v2": 0.1, "ponctualite_stricte": 99.0,
                 "part_avance": 0.0, "service_assure": 100.0, "passages": 900, "jours": 20,
                 "attente_excedentaire_s": None, "temps_reel_douteux": True, "part_retards_nuls": 100.0},
            ]),
            "regularity": {
                "59": {"attente_reelle": 330.0, "attente_prevue": 300.0, "attente_excedentaire": 30.0, "jours": 28},
                "19": {"attente_reelle": 400.0, "attente_prevue": 300.0, "attente_excedentaire": 100.0, "jours": 2},
            },
            "degraded_days": ["2026-08-21"],
            "names": {"59": "A", "S35": "S35", "102": "Navette_102"},
        }

    def test_section_complete(self):
        import generate_monthly_report as report

        tex = report.method_v2_section(self._v2())
        assert r"\label{v2page}" in tex
        assert r"75,1 $\pm$ 1,2" in tex
        assert "baisse significative" in tex
        assert "comparaison impossible" in tex
        assert "(collecte incomplète) : 08/09, 24/09." in tex
        assert "(conservés) : 21/08." in tex
        assert r"Navette\_102 (100\,\% de retards nuls)" in tex
        assert r"S35 & 18,6 $\pm$ 11,0 & 18,9\,\% & 70,2\,\% & 100,0\,\% & 371" in tex
        assert "\nA & 5 min 00 s & 5 min 30 s & +30 s" in tex
        assert "Navette" not in tex.split("Lignes les moins bien placées")[1].split("Qualité des données")[0]
        assert "\n19 &" not in tex

    def test_synthese_et_absence(self):
        import generate_monthly_report as report

        assert r"\pageref{v2page}" in report.method_v2_summary(self._v2())
        assert report.method_v2_section(None) == ""
        assert report.method_v2_summary(None) == ""

    def test_requete_sur_la_base(self, v2_db):
        import generate_monthly_report as report

        v2 = report.query_method_v2(v2_db, "2026-09", report.Scope("t", [], [], "t"))
        assert v2["current"].jours == 2
        assert v2["degraded_days"] == ["2026-09-19"]
        assert v2["names"]["A"] == "1"
        assert [item["label"] for item in v2["by_day_type"]] == ["Jours de semaine", "Samedis", "Dimanches"]
        assert v2["by_day_type"][0]["change"] is None
        assert report.method_v2_section(v2).startswith(r"\newpage")

    def test_requete_sans_donnees(self, v2_db):
        import generate_monthly_report as report

        assert report.query_method_v2(v2_db, "2026-06", report.Scope("t", [], [], "t")) is None
