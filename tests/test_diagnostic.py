"""Tests de la logique de diagnostic (dashboard/diagnostic.py), sans Streamlit ni base."""

import pandas as pd
import pytest

import diagnostic as dg


class TestScore:
    def test_formule_et_bornes(self):
        assert dg.reliability_score(90.0, 5.0) == 80.0
        assert dg.reliability_score(10.0, 20.0) == 0.0

    def test_repartition_des_points_perdus(self):
        b = dg.score_breakdown(88.0, 3.0)
        assert b["score"] == 82.0
        assert b["lost"] == 18.0
        assert (b["lost_delay"], b["lost_skip"]) == (12.0, 6.0)
        assert b["dominant"] == "retards"

    def test_service_non_rendu_dominant(self):
        assert dg.score_breakdown(97.0, 6.0)["dominant"] == "service"

    def test_aucun_probleme_sous_5_points(self):
        assert dg.score_breakdown(97.0, 0.5)["dominant"] == "aucun"


class TestLigneResponsable:
    def test_nombre_absolu_et_non_pourcentage(self):
        lines = pd.DataFrame({
            "route_id": ["A", "B"], "ligne": ["1", "11"],
            "cnt_gt300": [5, 40], "skipped": [10, 0],
            "observations": [20, 800],
        })
        r = dg.responsible_line(lines)
        assert r["ligne"] == "11"
        assert r["impact"] == 40
        assert r["share"] == pytest.approx(40 / 55)

    def test_aucun_passage_problematique(self):
        lines = pd.DataFrame({"route_id": ["A"], "ligne": ["1"], "cnt_gt300": [0], "skipped": [0]})
        assert dg.responsible_line(lines) is None
        assert dg.responsible_line(pd.DataFrame()) is None


class TestLocalisation:
    def test_retard_importe(self):
        assert dg.locate_cause(240.0, 10.0, 250.0)["verdict"] == "amont"

    def test_retard_local(self):
        c = dg.locate_cause(30.0, 90.0, 120.0)
        assert c["verdict"] == "local"
        assert c["share_local"] == pytest.approx(0.75)

    def test_mixte(self):
        assert dg.locate_cause(80.0, 40.0, 120.0)["verdict"] == "mixte"

    def test_pas_de_retard_notable(self):
        assert dg.locate_cause(20.0, 10.0, 30.0)["verdict"] == "aucun"
        assert dg.locate_cause(None, None, None)["verdict"] == "aucun"

    def test_avance_rattrapee_ignoree(self):
        assert dg.locate_cause(150.0, -20.0, 130.0)["verdict"] == "amont"


def _profile():
    return pd.DataFrame({
        "stop_id": ["a", "b", "c", "d", "e"],
        "stop_name": ["A", "B", "C", "D", "E"],
        "prev_stop_name": [None, "A", "B", "C", "D"],
        "commune": ["Bordeaux", "Bordeaux", "Mérignac", "Mérignac", "Pessac"],
        "order": [1.0, 2.0, 3.0, 4.0, 5.0],
        "delay_s": [10.0, 20.0, 120.0, 125.0, 130.0],
        "gain_s": [0.0, 10.0, 100.0, 5.0, 5.0],
        "eligible": [100, 100, 100, 100, 100],
        "skipped": [0, 0, 0, 0, 0],
    })


class TestTronconAmont:
    def test_troncon_le_plus_penalisant_avant_l_arret(self):
        h = dg.upstream_hotspot(_profile(), "e")
        assert (h["from"], h["to"], h["commune"]) == ("B", "C", "Mérignac")
        assert h["gain_s"] == 100.0

    def test_arret_de_tete_sans_amont(self):
        assert dg.upstream_hotspot(_profile(), "a") is None
        assert dg.upstream_hotspot(_profile(), "inconnu") is None


class TestRecurrence:
    def _daily(self, pcts):
        return pd.DataFrame({
            "date_service": pd.date_range("2026-09-01", periods=len(pcts)),
            "obs": [100] * len(pcts),
            "cnt_gt300": pcts,
        })

    def test_chronique(self):
        r = dg.recurrence(self._daily([20, 30, 25, 2, 40, 1]))
        assert (r["bad_days"], r["days"], r["verdict"]) == (4, 6, "chronique")

    def test_ponctuel_avec_dates(self):
        r = dg.recurrence(self._daily([2, 3, 40, 1, 2, 3, 1, 2]))
        assert r["verdict"] == "ponctuel"
        assert r["bad_dates"] == [pd.Timestamp("2026-09-03")]

    def test_jours_trop_peu_observes_ignores(self):
        daily = self._daily([50, 1, 1, 1, 1, 1])
        daily.loc[0, "obs"] = 3
        daily.loc[0, "cnt_gt300"] = 3
        r = dg.recurrence(daily)
        assert (r["days"], r["verdict"]) == (5, "aucun")

    def test_periode_trop_courte_pour_conclure(self):
        r = dg.recurrence(self._daily([40, 40]))
        assert (r["bad_days"], r["verdict"]) == (2, "période courte")
        text = " ".join(dg.stop_summary(None, None, {"verdict": "aucun"}, None, None, None, None, r, None, None))
        assert "Période trop courte pour juger de la récurrence (2 jours observés)" in text

    def test_plusieurs_lignes_par_jour_sommees(self):
        daily = pd.DataFrame({
            "date_service": pd.to_datetime(["2026-09-01", "2026-09-01"]),
            "obs": [50, 50], "cnt_gt300": [20, 0],
        })
        assert dg.recurrence(daily)["bad_days"] == 1

    def test_singulier(self):
        r = {"days": 8, "bad_days": 1, "bad_dates": [pd.Timestamp("2026-09-03")], "verdict": "ponctuel"}
        assert dg._recurrence_sentence(r) == "Problème ponctuel : 1 jour dégradé sur 8 (03/09)."


class TestQuand:
    def test_creneaux_et_concentration(self):
        hourly = pd.DataFrame({
            "date_service": ["2026-09-14"] * 3 + ["2026-09-13"],
            "heure": [8, 17, 12, 17],
            "obs": [100, 100, 100, 100],
            "cnt_gt300": [5, 40, 5, 5],
        })
        t = dg.period_table(hourly)
        assert list(t["période"]) == ["Matin", "Journée", "Pointe du soir", "Week-end"]
        c = dg.concentration(t, "période")
        assert c["label"] == "Pointe du soir"
        assert c["pct"] == 40.0
        assert c["rest_pct"] == 5.0

    def test_pas_de_concentration_si_ecart_faible(self):
        t = pd.DataFrame({"période": ["Matin", "Journée"], "obs": [100, 100],
                          "cnt_gt300": [10, 8], "pct_gt300": [10.0, 8.0]})
        assert dg.concentration(t, "période") is None

    def test_jours_de_la_semaine(self):
        daily = pd.DataFrame({
            "date_service": pd.to_datetime(["2026-09-14", "2026-09-15", "2026-09-21"]),
            "obs": [100, 100, 100], "cnt_gt300": [10, 0, 30],
        })
        t = dg.weekday_table(daily)
        assert list(t["jour"]) == ["Lundi", "Mardi"]
        assert t.loc[0, "pct_gt300"] == 20.0

    def test_tendance_par_moities(self):
        daily = pd.DataFrame({
            "date_service": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]),
            "obs": [100] * 4, "cnt_le300": [90, 90, 70, 70],
            "skipped": [0, 0, 5, 5], "eligible": [100] * 4,
        })
        t = dg.half_trend(daily)
        assert (t["previous"], t["recent"], t["delta"]) == (90.0, 60.0, -30.0)

    def test_tendance_un_seul_jour(self):
        daily = pd.DataFrame({"date_service": ["2026-09-01"], "obs": [10], "cnt_le300": [9],
                              "skipped": [0], "eligible": [10]})
        assert dg.half_trend(daily) is None

    def test_rang(self):
        assert dg.percentile_rank(50.0, pd.Series([10.0, 50.0, 90.0, 95.0])) == 50.0


class TestOrigineDuRetard:
    def test_localise(self):
        o = dg.classify_delay_origin(_profile())
        assert o["verdict"] == "localisé"
        assert o["hotspots"][0]["to"] == "C"
        assert o["hotspot_share"] == pytest.approx(115 / 120)

    def test_depart(self):
        p = _profile().assign(delay_s=[100.0, 105.0, 110.0, 115.0, 120.0])
        assert dg.classify_delay_origin(p)["verdict"] == "départ"

    def test_diffus(self):
        p = pd.DataFrame({
            "stop_name": list("ABCDEFGHIJ"), "prev_stop_name": [None] + list("ABCDEFGHI"),
            "commune": ["X"] * 10, "order": range(10),
            "delay_s": [0, 15, 30, 45, 60, 75, 90, 105, 120, 135],
            "gain_s": [0] + [15] * 9,
        })
        assert dg.classify_delay_origin(p)["verdict"] == "diffus"

    def test_aucun_retard_notable(self):
        p = _profile().assign(delay_s=[0.0, 5.0, 20.0, 25.0, 30.0])
        assert dg.classify_delay_origin(p)["verdict"] == "aucun"


class TestArretsSautes:
    def _p(self, skipped):
        n = len(skipped)
        return pd.DataFrame({"stop_name": [f"S{i}" for i in range(n)], "order": range(n),
                             "eligible": [100] * n, "skipped": skipped})

    def test_aucun(self):
        assert dg.classify_skips(self._p([0] * 10))["verdict"] == "aucun"

    def test_extremites(self):
        assert dg.classify_skips(self._p([20, 0, 0, 0, 0, 0, 0, 0, 0, 20]))["verdict"] == "extrémités"

    def test_bloc_au_milieu(self):
        s = dg.classify_skips(self._p([0, 0, 0, 30, 30, 30, 0, 0, 0, 0]))
        assert s["verdict"] == "bloc"
        assert s["block"] == ("S3", "S5")

    def test_disperse(self):
        assert dg.classify_skips(self._p([0, 5, 0, 5, 0, 5, 0, 5, 0, 0]))["verdict"] == "dispersé"


class TestDirections:
    def test_desequilibre(self):
        d = pd.DataFrame({"terminus": ["vers Aéroport", "vers Gare"], "obs": [100, 100],
                          "cnt_gt300": [70, 20]})
        assert dg.direction_imbalance(d) == {"terminus": "vers Aéroport", "share": pytest.approx(70 / 90)}

    def test_equilibre(self):
        d = pd.DataFrame({"terminus": ["a", "b"], "obs": [100, 100], "cnt_gt300": [50, 40]})
        assert dg.direction_imbalance(d) is None


class TestPhrases:
    def test_fiche_arret_amont(self):
        rec = {"days": 14, "bad_days": 11, "bad_dates": [], "verdict": "chronique"}
        lines = dg.stop_summary(
            {"ligne": "11", "share": 0.64}, "vers Aéroport", {"verdict": "amont", "share_local": 0.1},
            250.0, 10.0, "Capeyron",
            {"from": "Quatre Chemins", "to": "Capeyron", "commune": "Mérignac", "gain_s": 80.0},
            rec, {"label": "Pointe du soir", "pct": 38.0, "rest_pct": 9.0}, "frequents_courts",
        )
        text = " ".join(lines)
        assert "La ligne 11 (vers Aéroport) concentre 64 %" in text
        assert "déjà présent en arrivant (+4 min 10 s en moyenne)" in text
        assert "entre Quatre Chemins et Capeyron (Mérignac)" in text
        assert "11 jours dégradés sur 14" in text
        assert "en Pointe du soir (38 %" in text
        assert text.endswith("retards fréquents mais courts.")

    def test_fiche_arret_jour_de_semaine(self):
        rec = {"days": 0, "bad_days": 0, "bad_dates": [], "verdict": "aucun"}
        lines = dg.stop_summary(None, None, {"verdict": "aucun"}, None, None, None, None, rec,
                                {"label": "Samedi", "pct": 20.0, "rest_pct": 5.0}, None)
        assert lines == ["Il se concentre le samedi (20 % de passages à plus de 5 min, contre 5 % le reste du temps)."]

    def test_pistes_arret(self):
        hints = dg.stop_hints({"verdict": "local"}, None, "Capeyron", "Mérignac Centre",
                              None, {"verdict": "aucun"}, 0.0, False)
        assert hints == ["Tronçon Capeyron → Mérignac Centre : piste d'aménagement de voirie "
                         "(priorité aux feux, voie réservée), de la compétence de la commune ou de "
                         "Bordeaux Métropole."]
        hints = dg.stop_hints({"verdict": "aucun"}, None, None, "X", None,
                              {"verdict": "ponctuel"}, 10.0, True)
        assert len(hints) == 2

    def test_fiche_ligne(self):
        origin = dg.classify_delay_origin(_profile())
        lines = dg.line_summary("11", dg.score_breakdown(74.0, 6.0), 41, origin,
                                {"terminus": "vers Aéroport", "share": 0.7},
                                {"verdict": "extrémités", "rate": 6.0, "block": None},
                                {"days": 0, "bad_days": 0, "bad_dates": [], "verdict": "aucun"}, None)
        text = " ".join(lines)
        assert text.startswith("Sur 38 points perdus, 26 viennent des retards et 12 des arrêts sautés.")
        assert "41 course(s) supprimée(s)" in text
        assert "entre B et C (Mérignac)" in text
        assert "Le sens vers Aéroport concentre 70 %" in text
        assert "extrémités" in text

    def test_pistes_ligne(self):
        origin = dg.classify_delay_origin(_profile())
        hints = dg.line_hints(origin, {"verdict": "bloc", "block": ("S3", "S5")}, 3)
        assert hints[0].startswith("Tronçon B → C : point noir de circulation (commune de Mérignac")
        assert any("Courses supprimées" in h for h in hints)
        assert any("entre S3 et S5" in h for h in hints)


class TestTronconDominant:
    HOT = {"from": "A", "to": "B", "stop_id": "b", "commune": "Pessac", "gain_s": 30.0}

    def test_seuil_du_quart_du_retard_importe(self):
        assert dg.is_dominant_hotspot(self.HOT, 120.0)
        assert not dg.is_dominant_hotspot(self.HOT, 121.0 * 1.1)
        assert not dg.is_dominant_hotspot(None, 120.0)
        assert not dg.is_dominant_hotspot(self.HOT, 0.0)

    def test_accumulation_progressive_sans_troncon_dominant(self):
        rec = {"days": 0, "bad_days": 0, "bad_dates": [], "verdict": "aucun"}
        text = " ".join(dg.stop_summary(None, None, {"verdict": "amont"}, 300.0, 5.0, "X",
                                        self.HOT, rec, None, None))
        assert "s'accumule progressivement le long du parcours en amont" in text
        assert "Pessac" not in text
        hints = dg.stop_hints({"verdict": "amont"}, self.HOT, "X", "Y", {"ligne": "5"},
                              rec, 0.0, False, 300.0)
        assert hints == ["Retard importé et accumulé sur tout le parcours amont : il relève du temps "
                         "de parcours de la ligne plus que d'un aménagement local. Voir la fiche de la ligne 5."]


class TestASurveiller:
    def _prog(self, delta):
        return pd.DataFrame({"route_id": ["A", "B"], "ligne": ["1", "2"], "delta_score": [delta, 3.0],
                             "observations": [1000, 1000],
                             "score_fiabilite_prev": [80.0, 70.0], "score_fiabilite": [80.0 + delta, 73.0]})

    def _lines(self):
        return pd.DataFrame({"route_id": ["A", "C", "D"], "ligne": ["1", "3", "4"],
                             "observations": [1000, 5000, 20], "pct_retard_5min": [30.0, 10.0, 90.0],
                             "score_fiabilite": [60.0, 75.0, 10.0]})

    def _stops(self):
        return pd.DataFrame({"stop_id": ["s1", "s2"], "stop_name": ["Gare", "Parc"],
                             "direction": ["vers Parc", ""], "observations": [400, 900],
                             "pct_retard_5min": [50.0, 5.0], "score_fiabilite": [50.0, 95.0]})

    def test_trois_signaux_distincts(self):
        items = dg.watchlist(self._prog(-12.0), self._lines(), self._stops(), 50)
        assert [(i["kind"], i["id"]) for i in items] == [("ligne", "A"), ("arrêt", "s1"), ("ligne", "C")]
        assert items[0]["reason"] == "score en baisse de 12.0 points (80.0 → 68.0)"
        assert items[1]["title"] == "Gare — vers Parc"
        assert items[1]["reason"] == "200 passages à plus de 5 min, score 50.0/100"

    def test_baisse_ponderee_par_les_passages(self):
        prog = pd.DataFrame({"route_id": ["A", "B"], "ligne": ["Arena", "24"],
                             "delta_score": [-50.0, -6.0], "observations": [60, 9000],
                             "score_fiabilite_prev": [97.0, 85.0], "score_fiabilite": [47.0, 79.0]})
        assert dg.watchlist(prog, None, None, 50)[0]["id"] == "B"

    def test_baisse_faible_ignoree_et_petit_echantillon_exclu(self):
        items = dg.watchlist(self._prog(-2.0), self._lines(), None, 50)
        assert [(i["kind"], i["id"]) for i in items] == [("ligne", "C")]

    def test_rien_a_signaler(self):
        assert dg.watchlist(None, None, None, 50) == []
