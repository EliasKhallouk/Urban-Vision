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

    def test_evolution_par_rapport_a_la_periode_precedente(self):
        cols = ["obs", "cnt_le300", "skipped", "eligible"]
        current = pd.DataFrame([[100, 70, 5, 100], [100, 70, 5, 100]], columns=cols)
        previous = pd.DataFrame([[100, 90, 0, 100]], columns=cols)
        assert dg.period_score(current) == 60.0
        assert dg.score_change(current, previous) == {"current": 60.0, "previous": 90.0, "delta": -30.0}

    def test_evolution_sans_periode_precedente(self):
        cols = ["obs", "cnt_le300", "skipped", "eligible"]
        current = pd.DataFrame([[10, 9, 0, 10]], columns=cols)
        assert dg.score_change(current, None) is None
        assert dg.score_change(current, pd.DataFrame(columns=cols)) is None
        assert dg.period_score(pd.DataFrame([[0, 0, 0, 0]], columns=cols)) is None

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
        assert text.startswith("Sur 38 points perdus, 26 viennent des retards et 12 des arrêts non desservis.")
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
        items = dg.watchlist(self._prog(-12.0), self._lines(), self._stops(), 50, prev_label="août 2026")
        assert [(i["kind"], i["id"]) for i in items] == [("ligne", "A"), ("arrêt", "s1"), ("ligne", "C")]
        assert items[0]["reason"] == "score en baisse de 12.0 points par rapport à août 2026 (80.0 → 68.0)"
        assert items[1]["title"] == "Gare — vers Parc"
        assert items[1]["reason"] == "le plus de passages en retard du territoire : 200 à plus de 5 min ; score 50.0/100"
        assert items[2]["reason"].startswith("le plus de passages en retard des lignes : 500 à plus de 5 min")

    def test_arret_regroupe_sur_deux_sens(self):
        stops = pd.DataFrame({"stop_id": ["s1"], "stop_name": ["Blancherie"], "direction": ["vers Blancherie"],
                              "n_sens": [2], "observations": [2950], "pct_retard_5min": [16.8],
                              "score_fiabilite": [67.9]})
        item = dg.watchlist(None, None, stops, 50)[0]
        assert item["title"] == "Blancherie (2 sens)"
        assert item["reason"] == ("le plus de passages en retard du territoire : 496 à plus de 5 min ; "
                                  "le moins fiable : vers Blancherie, 67.9/100")

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


def _fridays_hourly(bad_fridays=4, n_weeks=5, route="A"):
    """Arrêt observé du lundi au vendredi sur n semaines, 8 passages par heure de 7 h à 19 h.

    Les `bad_fridays` premiers vendredis, 6 passages sur 8 ont plus de 5 min entre 13 h et 14 h.
    """
    rows = []
    start = pd.Timestamp("2026-08-03")
    for w in range(n_weeks):
        for d in range(5):
            date = start + pd.Timedelta(days=7 * w + d)
            for h in range(7, 20):
                late = 6 if (d == 4 and h == 13 and w < bad_fridays) else 0
                rows.append({"date_service": date, "route_id": route, "heure": h, "obs": 8, "cnt_gt300": late,
                             "sum_delay": 8 * (400 if late else 60)})
    return pd.DataFrame(rows)


class TestMomentDeLaSemaine:
    def test_grille_jour_heure(self):
        t = dg.week_hour_table(_fridays_hourly())
        cell = t[(t["weekday"] == 4) & (t["heure"] == 13)].iloc[0]
        assert (cell["obs"], cell["cnt_gt300"], cell["days"], cell["bad_days"]) == (40, 24, 5, 4)
        assert cell["pct_gt300"] == 60.0
        assert len(t) == 5 * 13

    def test_pic_recurrent_du_vendredi(self):
        peak = dg.find_peak(_fridays_hourly())
        assert (peak["verdict"], peak["weekday"], peak["hour"]) == ("récurrent", 4, 13)
        assert (peak["bad_days"], peak["days"]) == (4, 5)
        text = dg.peak_sentence(peak, "11")
        assert text.startswith("Pic récurrent le vendredi entre 13 h et 14 h : 60 % de passages à plus de 5 min")
        assert "4 vendredis dégradés sur 5" in text
        assert text.endswith("Ligne la plus touchée à ce moment-là : 11.")

    def test_pic_ponctuel(self):
        peak = dg.find_peak(_fridays_hourly(bad_fridays=1))
        assert peak["verdict"] == "ponctuel"
        assert "seulement 1 vendredi dégradé sur 5 : plutôt un incident ponctuel" in dg.peak_sentence(peak)

    def test_periode_trop_courte(self):
        peak = dg.find_peak(_fridays_hourly(bad_fridays=0, n_weeks=2))
        assert peak["verdict"] == "période courte"
        assert "au moins trois semaines" in dg.peak_sentence(peak)

    def test_a_confirmer_sur_deux_occurrences(self):
        peak = dg.find_peak(_fridays_hourly(bad_fridays=2, n_weeks=2))
        assert peak["verdict"] == "à confirmer"

    def test_rien_ne_ressort(self):
        assert dg.find_peak(_fridays_hourly(bad_fridays=0))["verdict"] == "aucun"

    def test_ligne_la_plus_touchee_au_moment(self):
        h = pd.concat([_fridays_hourly(route="A"), _fridays_hourly(bad_fridays=0, route="B")])
        lines = dg.slot_lines(h, 4, 13)
        assert list(lines["route_id"]) == ["A", "B"]
        assert lines.loc[0, "cnt_gt300"] == 24

    def test_pic_dans_la_fiche_et_les_pistes(self):
        peak = dg.find_peak(_fridays_hourly())
        rec = {"days": 25, "bad_days": 4, "bad_dates": [], "verdict": "ponctuel"}
        text = " ".join(dg.stop_summary(None, None, {"verdict": "local"}, 10.0, 90.0, "X", None, rec,
                                        {"label": "Journée", "pct": 9.0, "rest_pct": 1.0}, None, peak, "11"))
        assert "Pic récurrent le vendredi" in text
        assert "Il se concentre" not in text
        hints = dg.stop_hints({"verdict": "local"}, None, "X", "Y", None, rec, 0.0, False, None, peak)
        assert any(h.startswith("Retards qui reviennent chaque vendredi entre 13 h et 14 h") for h in hints)


def _slot_route_hourly():
    """Ligne a→e, vendredi 13 h : +5 min en plus à partir de c, jusqu'à d ; e revient à la normale."""
    rows = []
    for date in pd.date_range("2026-08-03", periods=28):
        for stop, extra in zip("abcde", (0, 0, 300, 280, 0)):
            for h in (12, 13):
                friday13 = date.dayofweek == 4 and h == 13
                rows.append({"date_service": date, "stop_id": stop, "heure": h, "obs": 4,
                             "sum_delay": 4 * (60 + (extra if friday13 else 0))})
    return pd.DataFrame(rows)


class TestRepercussion:
    def _profile(self):
        return pd.DataFrame({"stop_id": list("abcde"), "stop_name": list("ABCDE"), "order": [1, 2, 3, 4, 5],
                             "commune": ["X", "X", "Y", "Y", "Z"]})

    def test_profil_du_creneau(self):
        sp = dg.slot_profile(_slot_route_hourly(), self._profile(), 4, 13)
        assert list(sp["stop_id"]) == list("abcde")
        c = sp[sp["stop_id"] == "c"].iloc[0]
        assert (c["slot_delay"], c["usual_delay"], c["excess"]) == (360.0, 60.0, 300.0)

    def test_surcroit_ne_ici_et_prolonge(self):
        sp = dg.slot_profile(_slot_route_hourly(), self._profile(), 4, 13)
        prop = dg.propagation(sp, "c")
        assert (prop["verdict"], prop["n_up"], prop["n_down"], prop["until"]) == ("prolongé", 0, 1, "D")
        text = dg.propagation_sentence(prop, "11", "C")
        assert text == ("À ce moment-là, la ligne 11 passe à C avec +6 min 00 s de retard, contre +1 min 00 s "
                        "d'habitude. Ce surcroît naît sur le tronçon qui mène à l'arrêt et se prolonge sur les "
                        "1 arrêt(s) suivant(s), jusqu'à D.")

    def test_surcroit_venu_d_avant_et_resorbe(self):
        sp = dg.slot_profile(_slot_route_hourly(), self._profile(), 4, 13)
        prop = dg.propagation(sp, "d")
        assert (prop["verdict"], prop["origin"], prop["n_up"]) == ("résorbé", "C", 1)
        assert "déjà là dès C (1 arrêt(s) avant) et se résorbe dès l'arrêt suivant" in dg.propagation_sentence(prop, "11", "D")

    def test_pas_de_surcroit(self):
        sp = dg.slot_profile(_slot_route_hourly(), self._profile(), 0, 12)
        assert dg.propagation(sp, "c")["verdict"] == "aucun"
        assert dg.propagation(sp, "zz")["verdict"] == "inconnu"

    def test_troncon_qui_s_aggrave_au_creneau(self):
        sp = dg.slot_profile(_slot_route_hourly(), self._profile(), 4, 13)
        hs = dg.slot_hotspot(sp)
        assert (hs["from"], hs["to"], hs["commune"], hs["gain_s"]) == ("B", "C", "Y", 300.0)
        assert dg.slot_hotspot_sentence(hs) == ("À ce moment-là, le retard s'aggrave surtout entre B et C (Y) : "
                                                "+5 min 00 s de plus que d'habitude sur ce tronçon.")
        assert dg.slot_hotspot(dg.slot_profile(_slot_route_hourly(), self._profile(), 0, 12)) is None

    def test_libelles(self):
        assert dg.slot_label(4, 13) == "Vendredi 13 h–14 h"
        assert dg.slot_label(6, 23) == "Dimanche 23 h–0 h"
        assert dg.hour_range(23) == "entre 23 h et minuit"


class TestCommune:
    def _seg(self):
        return pd.DataFrame({
            "stop_id": list("abcd"), "stop_name": list("ABCD"), "prev_stop_name": [None, "A", "B", "C"],
            "commune": ["Bordeaux", "Pessac", "Pessac", "Talence"],
            "gain_s": [0.0, 30.0, 10.0, 20.0], "sum_gain": [0, 3000, 1000, -500],
        })

    def test_part_du_retard_prise_dans_la_commune(self):
        info = dg.commune_share(self._seg(), "Pessac")
        assert (info["n_stops"], info["share"]) == (2, 1.0)
        assert (info["hotspot"]["from"], info["hotspot"]["to"]) == ("A", "B")
        assert dg.commune_sentence(info) == ("Sur Pessac (2 arrêt(s) de la ligne), la ligne prend 100 % de son "
                                             "retard ; tronçon le plus pénalisant de la commune : A → B (+30 s en moyenne).")

    def test_commune_non_desservie_ou_absente(self):
        assert dg.commune_share(self._seg(), None) is None
        info = dg.commune_share(self._seg(), "Mérignac")
        assert dg.commune_sentence(info) == "La ligne ne dessert pas Mérignac sur la période."
        low = dg.commune_share(self._seg(), "Talence")
        assert dg.commune_sentence(low).endswith(": l'essentiel se forme ailleurs sur le parcours.")


class TestTronconsMemeNom:
    def test_deux_quais_du_meme_arret(self):
        assert dg.segment_path("Allende", "Allende") == "Allende"
        assert dg.segment_title("Allende", "Allende") == "Arrêt Allende"
        assert dg._segment_label({"from": "Allende", "to": "Allende", "commune": "Lormont"}) == \
            "à l'arrêt Allende (Lormont)"

    def test_troncon_ordinaire(self):
        assert dg.segment_path("Avenue de Paris", "La Ramade") == "Avenue de Paris → La Ramade"
        assert dg.segment_title("Avenue de Paris", "La Ramade") == "Tronçon Avenue de Paris → La Ramade"
        assert dg._segment_label({"from": "A", "to": "B", "commune": None}) == "entre A et B"
