"""Tests du module de rapports mensuels : helpers LaTeX, requêtes et compilation."""

import argparse
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import generate_monthly_report as report
import gtfs_static
from assign_stop_municipalities import initialize_tables


# ---------------------------------------------------------------------------
# Helpers purs
# ---------------------------------------------------------------------------

class TestLatexEscape:
    def test_chaine_vide(self):
        assert report.latex("") == ""

    def test_none_renvoie_tiret(self):
        assert report.latex(None) == "—"

    def test_caracteres_speciaux(self):
        assert report.latex("a&b\\c%d$e") == r"a\&b\textbackslash{}c\%d\$e"
        assert report.latex("A_B{x}#t") == r"A\_B\{x\}\#t"
        assert report.latex("~^!") == r"\textasciitilde{}\textasciicircum{}!"

    def test_chaine_sans_special(self):
        assert report.latex("Hello World") == "Hello World"


class TestPct:
    def test_valeur_normale(self):
        assert report.pct(90.123) == "90.1\\%"

    def test_valeur_zero(self):
        assert report.pct(0) == "0.0\\%"

    def test_none_et_nan(self):
        assert report.pct(None) == "—"
        assert report.pct(float("nan")) == "—"


class TestNumber:
    def test_sep_milliers_et_decimal(self):
        assert report.number(1234.5, 1) == "1\\,234,5"
        assert report.number(1000000, 0) == "1\\,000\\,000"

    def test_none_et_nan(self):
        assert report.number(None) == "—"
        assert report.number(float("nan")) == "—"


class TestDuration:
    def test_secondes(self):
        assert report.duration(59) == "+59 s"

    def test_minutes_et_secondes(self):
        assert report.duration(125) == "+2 min 05 s"

    def test_exactement_60(self):
        assert report.duration(60) == "+1 min 00 s"

    def test_negatif(self):
        assert report.duration(-125) == "-2 min 05 s"

    def test_sans_signe_positif(self):
        assert report.duration(125, signed=False) == "2 min 05 s"

    def test_avec_signe_positif(self):
        assert report.duration(125, signed=True) == "+2 min 05 s"

    def test_none_et_nan(self):
        assert report.duration(None) == "—"
        assert report.duration(float("nan")) == "—"


class TestSafeSlug:
    def test_noms_alphanumeriques(self):
        assert report.safe_slug("Ligne 12") == "ligne-12"

    def test_caracteres_speciaux_supprimes(self):
        assert report.safe_slug("!!!") == "rapport"

    def test_chaines_egales(self):
        assert report.safe_slug("Lormont") == "lormont"


class TestPreviousMonth:
    def test_debut_dannee(self):
        assert report.previous_month("2026-01") == "2025-12"

    def test_autre_mois(self):
        assert report.previous_month("2026-05") == "2026-04"


class TestResolveMonth:
    def test_explicite_valide(self, conn):
        assert report.resolve_month(conn, "2026-09") == "2026-09"

    def test_explicite_invalide(self, conn):
        with pytest.raises(ValueError, match="format AAAA-MM"):
            report.resolve_month(conn, "202609")

    def test_auto_depuis_la_base(self, conn):
        departure_time = int(datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc).timestamp())
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t1', '20260911', 'A', 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
            (departure_time, departure_time),
        )
        conn.commit()
        month = report.resolve_month(conn, None)
        # datetime converti en epoch puis strftime localtime => "2026-09"
        assert month == datetime.fromtimestamp(departure_time).strftime("%Y-%m")


class TestLoadScope:
    def _base_args(self, **overrides):
        defaults = dict(
            routes="", communes="", recipient="", profile=None,
            recipients_file="/nonexistent.json",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_paranet_reseau(self):
        scope = report.load_scope(self._base_args(routes="A,B", recipient="Métro"))
        assert scope.routes == ["A", "B"]
        assert scope.recipient == "Métro"

    def test_commune_dans_la_description(self):
        scope = report.load_scope(self._base_args(communes="Lormont"))
        assert scope.communes == ["Lormont"]
        assert "Lormont" in scope.description

    def test_profil_depuis_fichier(self, tmp_path):
        profile_path = tmp_path / "profiles.json"
        profile_path.write_text(
            json.dumps({"bdx": {"recipient": "Maire", "routes": ["A"], "description": "desc"}}),
            encoding="utf-8",
        )
        scope = report.load_scope(self._base_args(profile="bdx", recipients_file=str(profile_path)))
        assert scope.recipient == "Maire"
        assert scope.description == "desc"

    def test_profil_inconnu_erreur(self, tmp_path):
        profile_path = tmp_path / "profiles.json"
        profile_path.write_text(
            json.dumps({"bdx": {"recipient": "Maire", "routes": ["A"], "description": "desc"}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="inconnu"):
            report.load_scope(self._base_args(profile="zzz", recipients_file=str(profile_path)))

    def test_profil_sans_routes_ni_communes_erreur(self, tmp_path):
        profile_path = tmp_path / "empty.json"
        profile_path.write_text(json.dumps({"e": {}}), encoding="utf-8")
        with pytest.raises(ValueError, match="ni commune ni ligne"):
            report.load_scope(self._base_args(profile="e", recipients_file=str(profile_path)))


# ---------------------------------------------------------------------------
# KPI et métriques
# ---------------------------------------------------------------------------

def _scheduled_delays(delays):
    return pd.DataFrame(
        {
            "route_id": ["A"] * len(delays),
            "ligne": ["1"] * len(delays),
            "departure_delay": delays,
        }
    )


def _skipped_frame(eligible=10, skipped=2):
    return pd.DataFrame(
        {
            "route_id": ["A"],
            "ligne": ["1"],
            "skipped": [skipped],
            "eligible": [eligible],
        }
    )


class TestKpis:
    def test_formule(self):
        scheduled = _scheduled_delays([0, 200, 500])
        skipped = _skipped_frame()
        k = report.kpis(scheduled, skipped)
        assert k["passages"] == 3
        assert round(k["ponctualite"], 6) == 66.666667
        assert round(k["retard"], 6) == 233.333333
        assert k["retard_median"] == 200
        assert round(k["skip_rate"], 6) == 20.0
        assert round(k["fiability"], 6) == round(66.666667 - 40.0, 6)

    def test_sans_arrets_sautes(self):
        k = report.kpis(_scheduled_delays([10, 20]), _skipped_frame(eligible=0, skipped=0))
        assert k["skip_rate"] == 0
        assert k["fiability"] == round(k["ponctualite"], 6)


class TestComparison:
    def test_sans_precedent(self):
        c = report.comparison({"fiability": 80}, None)
        assert "Historique" in c["fiability"]

    def test_avec_precedent(self):
        c = report.comparison(
            {"fiability": 80, "ponctualite": 90, "retard": 20, "retard_median": 15, "skip_rate": 5},
            {"fiability": 75, "ponctualite": 85, "retard": 30, "retard_median": 25, "skip_rate": 3},
        )
        assert c["fiability"] == "+5.0 / 100"
        assert c["ponctualite"] == "+5.0 %"
        assert "moy." in c["retard"]
        assert c["skip_rate"] == "+2.00 %"


class TestMakeLineStats:
    def test_score_et_tri(self):
        scheduled = pd.DataFrame(
            {
                "route_id": ["A", "A", "A", "B", "B"],
                "ligne": ["1", "1", "1", "2", "2"],
                "departure_delay": [300, 400, 500, 0, 100],
            }
        )
        lines = report.make_line_stats(scheduled, _skipped_frame())
        row_a = lines[lines["route_id"] == "A"].iloc[0]
        row_b = lines[lines["route_id"] == "B"].iloc[0]
        assert row_a["passages"] == 3
        assert row_b["passages"] == 2
        assert row_b["score"] > row_a["score"]

    def test_score_borne_a_zero(self):
        scheduled = _scheduled_delays([1000, 1000])
        lines = report.make_line_stats(scheduled, _skipped_frame(eligible=10, skipped=5))
        assert lines.iloc[0]["score"] == 0.0


class TestRankingLines:
    def _lines(self):
        return pd.DataFrame(
            {
                "route_id": ["L10", "L90", "L95"],
                "ligne": ["10", "90", "95"],
                "score": [10.0, 90.0, 95.0],
                "passages": [5, 90, 95],
            }
        )

    def test_exclut_les_lignes_a_faible_volume(self):
        kept = report.ranking_lines(self._lines())
        assert list(kept["route_id"]) == ["L90", "L95"]

    def test_exclut_la_ligne_164_dans_une_commune(self):
        lines = pd.DataFrame(
            {
                "route_id": ["07", "164", "59"],
                "ligne": ["7", "F", "59"],
                "score": [88.0, 66.6, 92.0],
                "passages": [26106, 5, 34530],
            }
        )
        kept = report.ranking_lines(lines)
        assert "164" not in set(kept["route_id"])
        assert set(kept["route_id"]) == {"07", "59"}

    def test_tout_le_perimetre_reste_si_rien_n_atteint_le_seuil(self):
        small = self._lines()
        small["passages"] = [5, 40, 12]
        kept = report.ranking_lines(small)
        assert set(kept["route_id"]) == {"L10", "L90", "L95"}

    def test_dataframe_vide(self):
        assert report.ranking_lines(pd.DataFrame()).empty


class TestExecutiveMessage:
    def test_bon_et_pire_ligne(self):
        scheduled = _scheduled_delays([10, 400])
        lines = report.make_line_stats(scheduled, _skipped_frame())
        msg = report.executive_message(
            report.kpis(scheduled, _skipped_frame()), lines
        )
        assert "1" in msg  # ligne A
        assert "score de fiabilité" in msg

    def test_grand_rate_bas(self):
        kpi = {"ponctualite": 70, "skip_rate": 0}
        lines = pd.DataFrame([{"score": 40, "retard_5": 30, "ligne": "3"}])
        msg = report.executive_message(kpi, lines)
        assert "insuffisante" in msg
        assert "ligne 3" in msg


class TestOperationalViews:
    def test_bords_24_heures_et_distribution(self):
        scheduled = pd.DataFrame(
            {
                "departure_time": [
                    int(datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc).timestamp()),  # Paris ~10h
                    int(datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc).timestamp()),
                ],
                "departure_delay": [30, 400],
                "route_id": ["A", "A"],
            }
        )
        hourly, distribution = report.operational_views(scheduled)
        assert len(hourly) == 24
        assert hourly["passages"].sum() == 2
        assert len(distribution) == 11
        assert distribution["passages"].sum() == 2
        assert 0 <= distribution["passages"].min()


# ---------------------------------------------------------------------------
# Requêtes sur base temporaire
# ---------------------------------------------------------------------------

def _epoch_utc(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())


def _epoch_local(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi).timestamp())


class TestQueryServiceAlerts:
    def test_filtre_par_route_et_periode(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS service_alerts ("
            "alert_id TEXT, route_id TEXT, active_period_start INTEGER, "
            "active_period_end INTEGER, header_text TEXT, description_text TEXT, "
            "cause INTEGER, last_seen_at INTEGER, PRIMARY KEY (alert_id, route_id, active_period_start))"
        )
        conn.execute(
            "INSERT INTO service_alerts VALUES ('a1', 'A', ?, ?, 'Travaux', 'd', 8, 1)",
            (_epoch_utc(2026, 9, 5), _epoch_utc(2026, 9, 20)),
        )
        conn.execute(
            "INSERT INTO service_alerts VALUES ('a2', 'B', ?, ?, 'Avertissement', 'd', 2, 1)",
            (_epoch_utc(2026, 9, 10), _epoch_utc(2026, 9, 12)),
        )
        conn.commit()
        alerts = report.query_service_alerts(conn, "2026-09", {"A"})
        assert len(alerts) == 1
        assert alerts[0]["route_id"] == "A"

    def test_route_vide_ou_hors_periode_retourne_vide(self, conn):
        assert report.query_service_alerts(conn, "2026-09", set()) == []


class TestQueryCollectionGaps:
    def test_sans_table_retourne_zero(self, conn):
        gtfs_static.create_static_tables(conn)
        result = report.query_collection_gaps(conn, "2026-09")
        assert result["gap_seconds"] == 0

    def test_avec_interruption(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS collection_gaps (gap_start INTEGER, gap_end INTEGER)"
        )
        gap_start = _epoch_utc(2026, 9, 10, 0, 0)
        gap_end = _epoch_utc(2026, 9, 10, 1, 0)
        conn.execute("INSERT INTO collection_gaps VALUES (?, ?)", (gap_start, gap_end))
        conn.commit()
        result = report.query_collection_gaps(conn, "2026-09")
        assert result["gap_seconds"] == 3600


class TestQueryMonthlyEvolution:
    def test_regroupement_mensuel(self, conn):
        for month in ["2026-07-11", "2026-08-11"]:
            departure = _epoch_local(2026, int(month[5:7]), 11, 8, 0)
            conn.execute(
                "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
                "stop_sequence, stop_id, schedule_relationship, departure_delay, "
                "departure_time, last_seen_at) "
                "VALUES (?, '20260711', 'A', 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
                (f"t_{month}", departure, departure),
            )
        # une observation plus récente décale le buffer (cutoff = max - 20 min)
        deltat = _epoch_local(2026, 9, 11, 8, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t_sep', '20260911', 'A', 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
            (deltat, deltat),
        )
        conn.commit()
        scope = report.Scope("test", [], [], "test")
        evo = report.query_monthly_evolution(conn, "2026-08", scope)
        assert len(evo) >= 2
        assert set(evo.columns) >= {"mois", "ponctualite", "retard_moyen"}


class TestQueryStopStats:
    def test_direction_ajoutee_si_disponible(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'Ligne 1', 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret 1', 44.8, -0.5)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stop_direction ("
            "route_id TEXT, stop_id TEXT, direction_id INTEGER, terminus TEXT, "
            "PRIMARY KEY (route_id, stop_id))"
        )
        conn.execute("INSERT INTO stop_direction VALUES ('A', 's1', 0, 'Terminus Nord')")
        departure = _epoch_local(2026, 9, 11, 8, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t1', '20260911', 'A', 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
            (departure, departure),
        )
        # observation plus récente (sans heure de départ) qui décale le buffer
        recent = _epoch_local(2026, 9, 12, 12, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t_recent', '20260912', 'A', 0, 1, 's1', 'SCHEDULED', 10, NULL, ?)",
            (recent,),
        )
        conn.commit()
        scope = report.Scope("test", [], [], "test")
        stats = report.query_stop_stats(conn, "2026-09", scope)
        assert len(stats) == 1
        assert stats.iloc[0]["direction"] == "vers Terminus Nord"

    def test_seuil_passages_par_arret(self, conn, monkeypatch):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'Ligne 1', 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret 1', 44.8, -0.5)")
        conn.execute("INSERT INTO stops VALUES ('s2', 'Arret 2', 44.8, -0.5)")
        recent = _epoch_local(2026, 9, 12, 12, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t_recent', '20260912', 'A', 0, 1, 's1', 'SCHEDULED', NULL, NULL, ?)",
            (recent,),
        )

        def seed(stop, k):
            for i in range(k):
                t = _epoch_local(2026, 9, 11, 8, 0) + i
                conn.execute(
                    "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
                    "stop_sequence, stop_id, schedule_relationship, departure_delay, "
                    "departure_time, last_seen_at) "
                    "VALUES (?, '20260911', 'A', 0, 1, ?, 'SCHEDULED', 10, ?, ?)",
                    (f"t_{stop}_{i}", stop, t, recent - 3600),
                )

        seed("s1", 3)
        seed("s2", 1)
        conn.commit()
        monkeypatch.setattr(report, "MIN_PASSAGES_FOR_RANKING", 2)
        scope = report.Scope("test", [], [], "test")
        stats = report.query_stop_stats(conn, "2026-09", scope)
        assert list(stats["stop_id"]) == ["s1"]

    def test_flex_exclue_des_statistiques(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('568', 'Artigues', \"Flex' Artigues\", 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Buttinière', 44.8, -0.5)")
        recent = _epoch_local(2026, 9, 12, 12, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t_recent', '20260912', '568', 0, 1, 's1', 'SCHEDULED', NULL, NULL, ?)",
            (recent,),
        )
        for i in range(3):
            t = _epoch_local(2026, 9, 11, 8, 0) + i
            conn.execute(
                "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
                "stop_sequence, stop_id, schedule_relationship, departure_delay, "
                "departure_time, last_seen_at) "
                "VALUES (?, '20260911', '568', 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
                (f"tflex{i}", t, recent - 3600),
            )
        conn.commit()
        scope = report.Scope("test", [], [], "test")
        stats = report.query_stop_stats(conn, "2026-09", scope)
        assert stats.empty


class TestQueryObservations:
    def _seed(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'L1', 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret 1', 44.8, -0.5)")
        # observation récente (hors mois analysé) qui pousse le buffer de 20 min
        recent = _epoch_local(2026, 10, 5, 12, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t_recent', '20261005', 'A', 0, 1, 's1', 'SCHEDULED', NULL, NULL, ?)",
            (recent,),
        )
        departure = _epoch_local(2026, 9, 11, 8, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t1', '20260911', 'A', 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
            (departure, recent - 3600),  # stabilisé (last_seen < cutoff)
        )
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t2', '20260911', 'A', 0, 1, 's2', 'SKIPPED', NULL, ?, ?)",
            (_epoch_local(2026, 9, 11, 8, 5), recent - 3600),
        )
        conn.commit()

    def test_retourne_scheduled_et_skipped(self, conn):
        self._seed(conn)
        scope = report.Scope("test", [], [], "test")
        scheduled, skipped, collected_at = report.query_observations(conn, "2026-09", scope)
        assert len(scheduled) == 1
        assert len(skipped) == 1
        assert "/" in collected_at

    def test_commune_inconnue_leve(self, conn):
        self._seed(conn)
        # le rattachement existe (une commune connue), mais pas celle du scope
        initialize_tables(conn)
        conn.execute(
            "INSERT INTO stop_municipalities VALUES "
            "('s1', '33063', 'Cenon', 'point-in-polygon', 1)"
        )
        conn.commit()
        scope = report.Scope("test", [], ["Lormont"], "test")
        with pytest.raises(ValueError, match="inconnue"):
            report.query_observations(conn, "2026-09", scope)

    def test_flex_exclue_des_observations(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('A', '1', 'L1', 3)")
        conn.execute("INSERT INTO routes VALUES ('568', 'Flex', \"Flex' Artigues\", 3)")
        conn.execute("INSERT INTO stops VALUES ('s1', 'Arret 1', 44.8, -0.5)")
        recent = _epoch_local(2026, 9, 12, 12, 0)
        conn.execute(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
            "stop_sequence, stop_id, schedule_relationship, departure_delay, "
            "departure_time, last_seen_at) "
            "VALUES ('t_recent', '20260912', 'A', 0, 1, 's1', 'SCHEDULED', NULL, NULL, ?)",
            (recent,),
        )

        def obs(route, i):
            t = _epoch_local(2026, 9, 11, 8, 0) + i
            conn.execute(
                "INSERT INTO observations (trip_id, start_date, route_id, direction_id, "
                "stop_sequence, stop_id, schedule_relationship, departure_delay, "
                "departure_time, last_seen_at) "
                "VALUES (?, '20260911', ?, 0, 1, 's1', 'SCHEDULED', 10, ?, ?)",
                (f"t_{route}_{i}", route, t, recent - 3600),
            )

        obs("A", 0)
        obs("568", 1)
        conn.commit()
        scope = report.Scope("test", [], [], "test")
        scheduled, _, _ = report.query_observations(conn, "2026-09", scope)
        assert set(scheduled["route_id"]) == {"A"}


# ---------------------------------------------------------------------------
# Graphiques matplotlib et compilateur
# ---------------------------------------------------------------------------

class TestBuildNoDataLatex:
    def test_contient_section_et_mois(self, tmp_path):
        scope = report.Scope("Destinataire", [], ["Lormont"],
                             "Arrêts géolocalisés dans la commune de Lormont")
        content = report.build_no_data_latex("2026-09", scope, "01/10/2026 à 08:00")
        assert "Absence de données exploitables" in content
        assert "Septembre 2026" in content
        assert "Lormont" in content  # via le périmètre affiché sur la couverture


class TestGraphicalAnnex:
    def test_genere_des_png_et_du_latex(self, tmp_path):
        lines = pd.DataFrame([
            {"route_id": "A", "ligne": "1", "score": 65, "retard_median": 120,
             "retard_5": 10, "ponctualite": 90, "arrets_sautes": 2, "passages": 100},
        ])
        scheduled = pd.DataFrame({
            "departure_delay": [10, 200],
            "departure_time": [
                _epoch_local(2026, 9, 11, 8, 0),
                _epoch_local(2026, 9, 11, 9, 0),
            ],
        })
        tex = report.graphical_annex(lines, scheduled, output_dir=tmp_path)
        assert "Annexe" in tex
        assert len(list(tmp_path.glob("*.png"))) >= 1


class TestBuildLatexAlertsEscaping:
    def test_route_id_et_header_hostiles_echappes(self, tmp_path):
        lines = pd.DataFrame([
            {"route_id": "A", "ligne": "1", "score": 65.0, "retard_moyen": 90.0,
             "retard_median": 120, "retard_5": 10, "ponctualite": 90,
             "arrets_sautes": 2, "passages": 100},
        ])
        scheduled = pd.DataFrame({
            "departure_delay": [10, 200],
            "departure_time": [
                _epoch_local(2026, 9, 11, 8, 0),
                _epoch_local(2026, 9, 11, 9, 0),
            ],
        })
        metrics = {"fiability": 80, "passages": 100, "ponctualite": 85.0,
                   "retard": 120.0, "retard_median": 90.0, "skip_rate": 2.0}
        change = {"fiability": "+2", "ponctualite": "+1", "retard": "-10", "skip_rate": "0"}
        alerts = [
            {"route_id": "9&1", "header_text": "Colis & découverte %",
             "active_period_start": _epoch_local(2026, 9, 1, 8, 0),
             "active_period_end": _epoch_local(2026, 9, 3, 0, 0)},
            {"route_id": "A", "header_text": "Travaux &_%",
             "active_period_start": _epoch_local(2026, 9, 5, 8, 0),
             "active_period_end": _epoch_local(2026, 9, 7, 0, 0)},
        ]
        scope = report.Scope("test", [], [], "test scope")
        tex = report.build_latex(
            "2026-09", scope, metrics, change, lines, scheduled,
            "01/10/2026 à 08:00", tmp_path, alerts=alerts,
        )
        assert "Autres lignes concernées" in tex
        assert r"9\&1 (1)" in tex
        assert r"Travaux \&\_\%" in tex
        assert "9&1 (1)" not in tex
        assert "Travaux &_%" not in tex

    def test_priority_alerts_sans_glyphe_pour_ligne_sans_infos_trafic(self, tmp_path):
        lines = pd.DataFrame([
            {"route_id": "A", "ligne": "A", "score": 40.0, "retard_moyen": 90.0,
             "retard_median": 120, "retard_5": 20, "ponctualite": 80,
             "arrets_sautes": 1, "passages": 100},
            {"route_id": "6", "ligne": "6", "score": 50.0, "retard_moyen": 90.0,
             "retard_median": 120, "retard_5": 15, "ponctualite": 85,
             "arrets_sautes": 0, "passages": 100},
            {"route_id": "7", "ligne": "7", "score": 55.0, "retard_moyen": 90.0,
             "retard_median": 120, "retard_5": 10, "ponctualite": 90,
             "arrets_sautes": 0, "passages": 100},
        ])
        scheduled = pd.DataFrame({
            "departure_delay": [10],
            "departure_time": [_epoch_local(2026, 9, 11, 8, 0)],
        })
        metrics = {"fiability": 80, "passages": 100, "ponctualite": 85.0,
                   "retard": 120.0, "retard_median": 90.0, "skip_rate": 2.0}
        change = {"fiability": "+2", "ponctualite": "+1", "retard": "-10", "skip_rate": "0"}
        alerts = [
            {"route_id": "A", "header_text": "Travaux",
             "active_period_start": _epoch_local(2026, 9, 5, 8, 0),
             "active_period_end": _epoch_local(2026, 9, 7, 0, 0)},
        ]
        scope = report.Scope("test", [], [], "test scope")
        tex = report.build_latex(
            "2026-09", scope, metrics, change, lines, scheduled,
            "01/10/2026 à 08:00", tmp_path, alerts=alerts,
        )
        assert r"\item \alertmark{} \textbf{Ligne A}" in tex
        assert r"\item \alertmark{} \textbf{Ligne 6}" not in tex
        assert r"\item \alertmark{} \textbf{Ligne 7}" not in tex
        assert r"\item \textbf{Ligne 6}" in tex
        assert r"\item \textbf{Ligne 7}" in tex


class TestCompilePdf:
    def test_sans_latex_lance_runtime_error(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError, match="introuvable"):
            report.compile_pdf(__import__("pathlib").Path("dummy.tex"))