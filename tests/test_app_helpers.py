"""Tests des helpers purs du dashboard app.py (sans Streamlit, sans base réelle)."""

import json
import math
from datetime import datetime

import pandas as pd
import pytest

import app as app_mod


def _epoch_local(year, month, day, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


class TestFormatSeconds:
    def test_formats(self):
        assert app_mod.format_seconds(0) == "0 s"
        assert app_mod.format_seconds(59) == "59 s"
        assert app_mod.format_seconds(90) == "1 min 30 s"
        assert app_mod.format_seconds(None) == "—"
        assert app_mod.format_seconds(-90) == "−1 min 30 s"
        assert app_mod.format_seconds(90, signed=True) == "+1 min 30 s"


class TestFormatDate:
    def test_formats(self):
        ts = _epoch_local(2026, 9, 11, 14, 30)
        assert app_mod.format_date(ts) == "11/09/2026 à 14:30"
        assert app_mod.format_date(None) == "inconnue"
        assert app_mod.format_date(0) == "inconnue"


class TestDaysBounds:
    def test_periode_bornee(self):
        since_ts = _epoch_local(2026, 9, 1)
        end_ts = _epoch_local(2026, 9, 12)
        cutoff_ts = _epoch_local(2026, 9, 12, 12)
        assert app_mod._day_bounds(since_ts, end_ts, cutoff_ts) == ("2026-09-01", "2026-09-12")

    def test_end_ts_absent_prend_la_date_de_cutoff_plus_un_jour(self):
        cutoff_ts = _epoch_local(2026, 9, 11, 12)
        assert app_mod._day_bounds(None, None, cutoff_ts) == ("0000-00-00", "2026-09-12")

    def test_since_ts_absent(self):
        end_ts = _epoch_local(2026, 9, 11)
        assert app_mod._day_bounds(None, end_ts, 0) == ("0000-00-00", "2026-09-11")


class TestMedianFromHistograms:
    def test_effectif_impair(self):
        assert app_mod._median_from_hists([{"0": 1, "10": 1, "20": 1}]) == 10

    def test_effectif_pair_moyenne_des_deux_centraux(self):
        assert app_mod._median_from_hists([{"0": 1, "10": 1, "20": 1, "30": 1}]) == 15.0

    def test_cles_string_ok(self):
        assert app_mod._median_from_hists([{"0": 2, "10": 2}]) == 5.0

    def test_vide_renvoie_none(self):
        assert app_mod._median_from_hists([]) is None
        assert app_mod._median_from_hists([{}, {}]) is None


class TestCouleurs:
    def test_score_tier_style(self):
        style = app_mod._score_tier_style(90.0)
        assert "background-color: #7FAAA6" in style
        assert "color: #2B2523" in style
        style = app_mod._score_tier_style(45.0)
        assert f"background-color: {app_mod.score_color(45.0)}" in style
        assert "color: #FFFFFF" in style

    def test_evolution_neutre(self):
        assert app_mod._delta_style(-8.0) == app_mod._delta_style(8.0)
        assert app_mod.evolution_words(-0.8) == "▼ 0,8 point"
        assert app_mod.evolution_words(2.7) == "▲ 2,7 points"
        assert app_mod.evolution_words(0.0) == "stable"


class TestKpiCard:
    def test_pastille_par_polarite(self):
        assert app_mod._kpi_color("positif") == "#7FAAA6"
        assert app_mod._kpi_color("negatif") == "#A55F73"
        assert app_mod._kpi_color("moyen") == "#E1C17E"
        assert app_mod._kpi_color("#123456") == "#123456"
        assert app_mod._kpi_color("neutral") is None

    def test_kpi_card_sans_sublabel(self):
        card = app_mod.kpi_card("Passages", "1 234")
        assert 'class="kpi-label">Passages' in card
        assert 'class="kpi-value">1 234' in card
        assert "kpi-sublabel" not in card

    def test_kpi_card_avec_sublabel(self):
        card = app_mod.kpi_card("Retard moyen", "1 min", "≤ 5 min", polarity="positif")
        assert 'class="kpi-card"' in card
        assert 'class="kpi-dot" style="background:#7FAAA6"' in card
        assert "kpi-sublabel" in card

    def test_carte_neutre_non_coloree(self):
        card = app_mod.kpi_card("Passages", "1 234", polarity="neutral")
        assert "kpi-dot" not in card
        assert "background" not in card


def _ranking_inputs():
    scheduled = pd.DataFrame(
        {
            "route_id": ["A", "B"],
            "ligne": ["Ligne A", "Ligne B"],
            "route_type": [3, 0],
            "observations": [100, 50],
            "retard_moyen_s": [120.0, 60.0],
            "retard_median_s": [90.0, 30.0],
            "pct_a_l_heure": [70.0, 90.0],
            "pct_retard_5min": [20.0, 5.0],
            "pct_avance_1min": [2.0, 1.0],
            "on_time": [70.0, 45.0],
            "expected": [100.0, 50.0],
            "assured": [90.0, 50.0],
            "assured_on_time": [63.0, 45.0],
        }
    )
    skipped = pd.DataFrame(
        {
            "route_id": ["A", "B"],
            "ligne": ["Ligne A", "Ligne B"],
            "route_type": [3, 0],
            "skipped": [10, 0],
            "eligible": [100, 50],
        }
    )
    return scheduled, skipped


class TestMakeRanking:
    def test_score_assures_a_l_heure_sur_attendus(self):
        scheduled, skipped = _ranking_inputs()
        ranking = app_mod.make_ranking(scheduled, skipped)
        row_a = ranking[ranking["route_id"] == "A"].iloc[0]
        row_b = ranking[ranking["route_id"] == "B"].iloc[0]
        assert round(row_a["pct_arrets_sautes"], 6) == 10.0
        assert round(row_a["score_fiabilite"], 6) == 63.0
        assert round(row_a["pct_service"], 6) == 90.0
        assert round(row_b["score_fiabilite"], 6) == 90.0

    def test_trie_par_score_croissant(self):
        scheduled, skipped = _ranking_inputs()
        ranking = app_mod.make_ranking(scheduled, skipped)
        assert ranking["route_id"].tolist() == ["A", "B"]

    def test_mode_et_couleur_mappes(self):
        scheduled, skipped = _ranking_inputs()
        ranking = app_mod.make_ranking(scheduled, skipped)
        assert ranking.set_index("route_id")["mode"].to_dict() == {
            "A": "Bus", "B": "Tramway"
        }

    def test_ligne_sans_passage_attendu_non_classee(self):
        scheduled, skipped = _ranking_inputs()
        scheduled.loc[scheduled["route_id"] == "B", ["expected", "assured", "assured_on_time"]] = 0.0
        ranking = app_mod.make_ranking(scheduled, skipped)
        assert ranking["route_id"].tolist() == ["A"]


def _daily_core():
    return app_mod.ind.add_components(pd.DataFrame(
        {
            "date_service": ["2026-09-10", "2026-09-11", "2026-09-10", "2026-09-11"],
            "route_id": ["A", "A", "B", "B"],
            "ligne": ["1", "1", "2", "2"],
            "route_type": [3, 3, 0, 0],
            "obs": [4, 6, 10, 10],
            "sum_delay": [400, 600, 100, 100],
            "cnt_le300": [3, 4, 10, 9],
            "cnt_gt300": [1, 2, 0, 1],
            "cnt_lt60": [0, 0, 0, 0],
            "skipped": [1, 0, 0, 0],
            "eligible": [5, 6, 10, 10],
            "hist": [
                {"10": 1, "20": 1, "300": 1, "400": 1},
                {"10": 1, "20": 1, "300": 1, "400": 1, "600": 1, "700": 1},
                {"0": 10},
                {"0": 9, "600": 1},
            ],
        }
    ))


class TestDailyToNetwork:
    def test_agregation_par_ligne(self):
        scheduled, skipped = app_mod._daily_to_network(_daily_core())
        sch = scheduled.set_index("route_id")
        assert sch.loc["A", "observations"] == 10
        assert sch.loc["A", "retard_moyen_s"] == 100.0
        assert round(sch.loc["A", "pct_a_l_heure"], 6) == 70.0
        assert round(sch.loc["A", "pct_retard_5min"], 6) == 30.0
        assert sch.loc["A", "retard_median_s"] == 300.0  # médiane exacte des 10 passages
        assert sch.loc["B", "retard_moyen_s"] == 10.0
        assert sch.loc["B", "retard_median_s"] == 0.0
        skp = skipped.set_index("route_id")
        assert skp.loc["A", "skipped"] == 1
        assert skp.loc["A", "eligible"] == 11
        assert sch.loc["A", "expected"] == 11
        assert sch.loc["A", "assured_on_time"] == pytest.approx(3 / 4 * 4 + 4 / 6 * 6)

    def test_core_vide_renvoie_des_frames_vides_bien_formees(self):
        core = pd.DataFrame(columns=[
            "date_service", "route_id", "ligne", "route_type", "obs", "sum_delay",
            "cnt_le300", "cnt_gt300", "cnt_lt60", "skipped", "eligible", "hist",
        ])
        scheduled, skipped = app_mod._daily_to_network(core)
        assert scheduled.empty
        assert skipped.empty
        assert "retard_median_s" in scheduled.columns


class TestGroupDailyStopToRoute:
    def test_fusionne_les_histogrammes_et_les_compteurs(self):
        df = pd.DataFrame(
            {
                "date_service": ["2026-09-11", "2026-09-11", "2026-09-11"],
                "route_id": ["A", "A", "B"],
                "ligne": ["1", "1", "2"],
                "route_type": [3, 3, 0],
                "obs": [3, 1, 5],
                "sum_delay": [430, 10, 0],
                "cnt_le300": [2, 1, 5],
                "cnt_gt300": [1, 0, 0],
                "cnt_lt60": [0, 0, 0],
                "skipped": [1, 0, 0],
                "eligible": [4, 1, 5],
                "scheduled": [10, 10, 20],
                "cancelled": [2, 2, 0],
                "added": [1, 1, 0],
                "histogram": [
                    json.dumps({"10": 1, "20": 1, "400": 1}),
                    json.dumps({"10": 1, "300": 1}),
                    json.dumps({"0": 5}),
                ],
            }
        )
        agg = app_mod._group_daily_stop_to_route(df)
        a = agg[agg["route_id"] == "A"].iloc[0]
        assert a["obs"] == 4
        assert a["sum_delay"] == 440
        assert json.loads(a["histogram"]) == {"10": 2, "20": 1, "300": 1, "400": 1}
        assert (a["scheduled"], a["cancelled"], a["added"]) == (10, 2, 1)
        b = agg[agg["route_id"] == "B"].iloc[0]
        assert b["obs"] == 5

class TestSelectionDesFiches:
    @pytest.fixture(autouse=True)
    def _etat_vierge(self):
        import streamlit as st

        for k in list(st.session_state.keys()):
            del st.session_state[k]
        yield
        for k in list(st.session_state.keys()):
            del st.session_state[k]

    def test_clic_sur_la_carte_ouvre_la_fiche_arret(self):
        import streamlit as st

        st.session_state["carte_arrets"] = {"clicked": "s9"}
        app_mod._on_map_click()
        assert st.session_state["stop_id"] == "s9"

    def test_clic_dans_le_vide_ne_change_rien(self):
        import streamlit as st

        st.session_state["stop_id"] = "s1"
        st.session_state["carte_arrets"] = {"clicked": None}
        app_mod._on_map_click()
        assert st.session_state["stop_id"] == "s1"

    def test_ligne_de_tableau_selectionnee(self):
        import streamlit as st

        st.session_state["_ids"] = ["A", "B", "C"]
        st.session_state["tbl"] = {"selection": {"rows": [1], "columns": []}}
        app_mod._on_table_select("tbl", "_ids", app_mod.open_line)
        assert st.session_state["line_id"] == "B"
        assert st.session_state["sidebar_nav"] == app_mod.PAGE_LINE

    def test_ouvrir_un_arret_bascule_sur_le_territoire(self):
        import streamlit as st

        app_mod.open_stop("s4")
        assert (st.session_state["stop_id"], st.session_state["sidebar_nav"]) == ("s4", app_mod.PAGE_TERRITORY)


class TestRegroupementDesSens:
    def _stops(self):
        return pd.DataFrame({
            "stop_id": ["s1", "s2", "s3", "s4", "s5"],
            "stop_name": ["Blancherie", "Blancherie", "Blancherie", "Gare", "Gare"],
            "direction": ["vers Beaudésert", "vers Blancherie", "vers Parc", "vers A", "vers B"],
            "lat": [44.8400, 44.8402, 44.8400, 44.8500, 44.9000],
            "lon": [-0.5300, -0.5301, -0.5300, -0.5500, -0.5500],
            "route_type": [3, 3, 0, 3, 3],
            "score_fiabilite": [78.3, 67.9, 90.0, 60.0, 95.0],
            "pct_retard_5min": [13.7, 22.6, 5.0, 30.0, 2.0],
            "observations": [1908, 1042, 500, 100, 100],
            "lignes": ["27", "27, 28", "A", "5", "5"],
        })

    def test_deux_sens_proches_regroupes_au_pire_score(self):
        g = app_mod.group_stops(self._stops())
        blanch = g[(g["stop_name"] == "Blancherie") & (g["n_sens"] == 2)].iloc[0]
        assert blanch["stop_id"] == "s2"
        assert blanch["score_fiabilite"] == 67.9
        assert blanch["direction"] == "vers Blancherie"
        assert blanch["observations"] == 2950
        assert sorted(blanch["members"]) == ["s1", "s2"]
        assert blanch["lignes"] == "27, 28"
        assert blanch["pct_retard_5min"] == round((1908 * 13.7 + 1042 * 22.6) / 2950, 1)
        assert blanch["detail"].split("<br/>")[0] == "vers Blancherie : 68/100 · 23 % &gt; 5 min"

    def test_mode_different_ou_arret_eloigne_non_regroupes(self):
        g = app_mod.group_stops(self._stops())
        assert len(g) == 4
        tram = g[g["stop_id"] == "s3"].iloc[0]
        assert tram["n_sens"] == 1
        assert set(g.loc[g["stop_name"] == "Gare", "stop_id"]) == {"s4", "s5"}


class TestGrilleJourHeure:
    def _table(self):
        import diagnostic as dg

        rows = []
        for w in range(3):
            for h in (8, 9):
                rows.append({"date_service": pd.Timestamp("2026-08-07") + pd.Timedelta(days=7 * w),
                             "heure": h, "obs": 10, "cnt_gt300": 6 if h == 9 else 0})
        return dg.week_hour_table(pd.DataFrame(rows))

    def test_cases_colorees_encadrement_et_marges(self):
        html = app_mod.week_hour_grid_html(self._table(), {"weekday": 4, "hour": 9})
        assert html.count("<tr>") == 9
        assert 'style="background:#A55F73;color:#FFFFFF;outline:2px solid #2B2523' in html
        assert "Vendredi 9 h–10 h : 60 % de passages à plus de 5 min · 30 passages · dégradé 3 fois sur 3" in html
        assert '<th>Ven.</th>' in html and "<th>Tous</th>" in html
        assert ">30</td>" in html

    def test_grille_vide(self):
        assert app_mod.week_hour_grid_html(pd.DataFrame()) == ""


class TestNavigation:
    @pytest.fixture(autouse=True)
    def _etat_vierge(self):
        import streamlit as st

        for k in list(st.session_state.keys()):
            del st.session_state[k]
        yield
        for k in list(st.session_state.keys()):
            del st.session_state[k]

    def test_clic_sur_la_carte_demande_le_defilement_vers_la_fiche(self):
        import streamlit as st

        st.session_state["carte_arrets"] = {"clicked": "s9"}
        app_mod._on_map_click()
        assert st.session_state["_scroll_to"] == "fiche-arret"

    def test_fiche_ligne_depuis_un_arret_garde_le_retour(self):
        import streamlit as st

        app_mod.line_from_stop("11", "s9", "Mérignac Centre")
        assert (st.session_state["line_id"], st.session_state["sidebar_nav"]) == ("11", app_mod.PAGE_LINE)
        assert st.session_state["_from_stop"] == ("s9", "Mérignac Centre")
        assert st.session_state["_scroll_to"] == "fiche-ligne"
        app_mod.show_line("12")
        assert "_from_stop" not in st.session_state


class TestArretsRarementDesservis:
    def test_variante_marginale_ecartee_par_direction(self):
        df = pd.DataFrame({"direction_id": [0, 0, 0, 1, 1], "stop_id": list("abcde"),
                           "eligible": [1400, 1380, 2, 30, 3]})
        kept = app_mod.keep_served_stops(df)
        assert list(kept["stop_id"]) == ["a", "b", "d", "e"]
        assert app_mod.keep_served_stops(df.head(0)).empty


class TestPeriode:
    def test_mois_disponibles_puis_autres_periodes(self):
        from datetime import date

        opts = app_mod.period_options(date(2026, 7, 27), date(2026, 9, 30))
        assert opts == ["mois:2026-09", "mois:2026-08", "mois:2026-07", "jours:7", "jours:30", "tout", "dates"]

    def test_mois_par_defaut(self):
        from datetime import date

        assert app_mod.default_period_choice(date(2026, 7, 27), date(2026, 9, 30)) == "mois:2026-09"
        assert app_mod.default_period_choice(date(2026, 7, 27), date(2026, 10, 3)) == "mois:2026-09"
        assert app_mod.default_period_choice(date(2026, 10, 1), date(2026, 10, 3)) == "mois:2026-10"

    def test_libelles(self):
        from datetime import date

        last = date(2026, 9, 28)
        assert app_mod.period_choice_label("mois:2026-08", last) == "Août 2026"
        assert app_mod.period_choice_label("mois:2026-09", last) == "Septembre 2026 (jusqu'au 28/09)"
        assert app_mod.period_choice_label("mois:2026-09", date(2026, 9, 30)) == "Septembre 2026"
        assert app_mod.period_choice_label("jours:7", last) == "7 derniers jours"

    def test_mois_compare_au_mois_precedent(self):
        from datetime import date

        p = app_mod.resolve_period("mois:2026-09", date(2026, 7, 27), date(2026, 9, 30))
        assert (p.start, p.end, p.label) == (date(2026, 9, 1), date(2026, 10, 1), "septembre 2026")
        assert (p.prev_start, p.prev_end, p.prev_label) == (date(2026, 8, 1), date(2026, 9, 1), "août 2026")
        janvier = app_mod.resolve_period("mois:2027-01", date(2026, 1, 1), date(2027, 1, 20))
        assert (janvier.prev_start, janvier.end) == (date(2026, 12, 1), date(2027, 2, 1))

    def test_premier_mois_sans_comparaison(self):
        from datetime import date

        p = app_mod.resolve_period("mois:2026-07", date(2026, 7, 27), date(2026, 9, 30))
        assert p.prev_start is None and p.prev_label is None

    def test_jours_glissants_et_dates(self):
        from datetime import date

        p = app_mod.resolve_period("jours:7", date(2026, 7, 27), date(2026, 9, 30))
        assert (p.start, p.end, p.prev_start, p.prev_end) == (
            date(2026, 9, 24), date(2026, 10, 1), date(2026, 9, 17), date(2026, 9, 24))
        assert p.prev_label == "les 7 jours précédents"
        d = app_mod.resolve_period("dates", date(2026, 7, 27), date(2026, 9, 30), (date(2026, 9, 1), date(2026, 9, 10)))
        assert (d.start, d.end, d.prev_start, d.label) == (
            date(2026, 9, 1), date(2026, 9, 11), date(2026, 8, 22), "du 01/09/2026 au 10/09/2026")
        tout = app_mod.resolve_period("tout", date(2026, 7, 27), date(2026, 9, 30))
        assert (tout.start, tout.end, tout.prev_start) == (date(2026, 7, 27), date(2026, 10, 1), None)


class TestDirectionDepuisUnArret:
    def test_direction_ou_se_trouve_l_arret(self):
        per_dir = {
            0: {"profile": pd.DataFrame({"stop_id": ["a", "b"], "obs": [100, 90]})},
            1: {"profile": pd.DataFrame({"stop_id": ["b", "c"], "obs": [300, 80]})},
        }
        assert app_mod.stop_direction_in(per_dir, "a") == 0
        assert app_mod.stop_direction_in(per_dir, "b") == 1
        assert app_mod.stop_direction_in(per_dir, "z") is None
        assert app_mod.stop_direction_in(per_dir, None) is None


class TestMethodV2Caption:
    def _v2(self, **kw):
        base = {"disponible": True, "score": 72.34, "marge": 2.1, "ponctualite": 80.0, "service": 93.25, "avance": 4.5,
                "attente_excedentaire": 45.0, "lignes_ecartees": {}}
        return {**base, **kw}

    def test_phrase_complete(self):
        assert app_mod.method_v2_caption(self._v2(), "A") == (
            "Détail du score : score 72,3 ± 2,1 / 100 (marge à 95 %), service assuré 93,2 %, "
            "départs de plus d'1 min en avance 4,5 %, attente excédentaire +45 s.")

    def test_sans_marge_ni_regularite(self):
        text = app_mod.method_v2_caption(self._v2(marge=None, attente_excedentaire=None), "A")
        assert "score 72,3 / 100" in text
        assert "attente" not in text

    def test_indisponible(self):
        assert app_mod.method_v2_caption(self._v2(disponible=False), "A") is None
        assert app_mod.method_v2_caption(None) is None


class TestVueRapide:
    def _ranking(self):
        return pd.DataFrame({"route_id": ["A", "B"], "route_type": [0, 3], "ligne": ["A", "12"],
                             "score_fiabilite": [85.0, 40.0], "pct_retard_5min": [5.0, 30.0],
                             "pct_arrets_sautes": [0.5, 2.0], "observations": [1000, 400]})

    def test_tableau_des_lignes_sans_colonne_etat(self):
        table = app_mod.lines_table(self._ranking(), {"B"})
        assert list(table.columns) == ["Ligne", "Score / 100", "Retards > 5 min",
                                       "Arrêts non desservis", "Passages"]
        assert table["Ligne"].tolist() == ["● A", "■ ⚠ 12"]

    def test_bandeau_rapide_en_mots(self):
        cards = app_mod.header_kpis(88.0, 90.2, 136.0, 1.32, 1_277_999, 131, 17_079, 1_294_616,
                                    local=False, detailed=False)
        assert [c[0] for c in cards] == ["État", "À l'heure", "Retard moyen", "Arrêts non desservis"]
        assert cards[0][1] == "Fiable"
        assert "ensemble du réseau" in cards[0][2]
        assert cards[1][1] == "90 %"
        assert all(c[4] for c in cards)

    def test_bandeau_detaille(self):
        cards = app_mod.header_kpis(88.0, 90.2, 136.0, 1.32, 1_277_999, 131, 17_079, 1_294_616,
                                    local=True, detailed=True)
        assert [c[0] for c in cards] == ["Passages analysés", "Ponctualité", "Retard moyen", "Lignes suivies",
                                         "Arrêts non desservis"]
        assert cards[0][1] == "1 277 999"

    def test_carte_etat(self):
        label, value, sub, polarity, help_text = app_mod.status_kpi(47.2, "réseau : 88 / 100")
        assert (label, value, polarity) == ("État", "Problématique", app_mod.score_color(47.2))
        assert sub == "score 47 / 100 · réseau : 88 / 100"
        assert "Fiable à partir de 80" in help_text

    def test_aide_echappee_dans_la_carte(self):
        html_card = app_mod.kpi_card("Score", "80", None, "neutral", 'Part des "passages" < 5 min')
        assert 'role="tooltip">Part des &quot;passages&quot; &lt; 5 min</span>' in html_card
        assert 'tabindex="0"' in html_card and "lexique" not in html_card
        assert "kpi-help" not in app_mod.kpi_card("Score", "80")

    def test_aide_avec_lien_vers_le_lexique(self):
        card = app_mod.kpi_card("État", "Fiable", None, "positif", app_mod.HELP_TEXTS["score"])
        assert '<a href="?lexique=score-de-fiabilite" target="_blank" rel="noopener">Voir dans le lexique ↗</a>' in card
        assert 'class="kpi-dot" style="background:#7FAAA6"' in card

    def test_guide_au_premier_affichage_seulement(self):
        assert app_mod.should_show_guide({}, {}) is True
        assert app_mod.should_show_guide({"guide_seen": True}, {}) is False
        assert app_mod.should_show_guide({}, {"ligne": "59"}) is False
        assert app_mod.should_show_guide({}, {}, {app_mod.GUIDE_COOKIE: "1"}) is False
        assert app_mod.should_show_guide({}, {}, {"autre": "1"}) is True

    def test_lexique_sans_jargon_de_flux_dans_les_termes(self):
        terms = [term for term, _ in app_mod.LEXIQUE]
        assert "Arrêt non desservi" in terms
        assert all("SKIPPED" not in term for term in terms)


class TestRapportsPublies:
    def _pdf(self, root, *parts):
        path = root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        return path

    def test_liste_par_mois_du_plus_recent(self, tmp_path):
        net = self._pdf(tmp_path, "2026-09", "reseau", "bordeaux-metropole", "urban-vision-2026-09-reseau.pdf")
        pessac = self._pdf(tmp_path, "2026-09", "communes", "pessac", "urban-vision-2026-09-mairie-de-pessac.pdf")
        self._pdf(tmp_path, "2026-08", "communes", "bègles", "urban-vision-2026-08-mairie-de-bègles.pdf")
        (tmp_path / "2026-07").mkdir()
        (tmp_path / "corbeille").mkdir()
        reports = app_mod.list_reports(tmp_path)
        assert list(reports) == ["2026-09", "2026-08"]
        assert reports["2026-09"] == {"reseau": net, "communes": {"pessac": pessac}}
        assert reports["2026-08"]["reseau"] is None

    def test_dossier_absent(self, tmp_path):
        assert app_mod.list_reports(tmp_path / "absent") == {}

    def test_nom_du_mois(self):
        assert app_mod.month_name("2026-09") == "Septembre 2026"

    def test_slug_identique_au_lot_de_rapports(self):
        assert app_mod.report_slug("Saint-Médard-en-Jalles") == "saint-médard-en-jalles"


class TestFilDAriane:
    @pytest.fixture(autouse=True)
    def _etat_vierge(self):
        import streamlit as st

        for k in list(st.session_state.keys()):
            del st.session_state[k]
        yield
        for k in list(st.session_state.keys()):
            del st.session_state[k]

    def test_revenir_retrouve_la_ligne_apres_l_ouverture_d_un_arret(self):
        import streamlit as st

        st.session_state.update({"sidebar_nav": app_mod.PAGE_LINE, "commune_idx": 3, "line_id": "11", "stop_id": None})
        app_mod.open_stop("s9")
        assert (st.session_state["sidebar_nav"], st.session_state["stop_id"]) == (app_mod.PAGE_TERRITORY, "s9")
        assert st.session_state["map_view"] == app_mod.VIEW_STOPS
        app_mod.go_back()
        assert st.session_state["sidebar_nav"] == app_mod.PAGE_LINE
        assert (st.session_state["line_id"], st.session_state["stop_id"]) == ("11", None)
        assert st.session_state["commune_idx"] == 3
        assert st.session_state[app_mod.HISTORY_KEY] == []

    def test_recherche_unique_ligne_arret_commune(self):
        import streamlit as st

        st.session_state.update({"sidebar_nav": app_mod.PAGE_TERRITORY, "stop_id": "s1", app_mod.SEARCH_KEY: "l:59"})
        app_mod._on_search()
        assert (st.session_state["line_id"], st.session_state["sidebar_nav"]) == ("59", app_mod.PAGE_LINE)
        assert st.session_state["stop_id"] is None
        assert st.session_state[app_mod.SEARCH_KEY] is None
        st.session_state[app_mod.SEARCH_KEY] = "s:s4"
        app_mod._on_search()
        assert (st.session_state["stop_id"], st.session_state["sidebar_nav"]) == ("s4", app_mod.PAGE_TERRITORY)
        assert st.session_state["map_view"] == app_mod.VIEW_STOPS
        assert st.session_state["line_id"] is None
        st.session_state[app_mod.SEARCH_KEY] = "c:2"
        app_mod._on_search()
        assert (st.session_state["commune_idx"], st.session_state["stop_id"]) == (2, None)
        assert len(st.session_state[app_mod.HISTORY_KEY]) == 3

    def test_fil_d_ariane(self):
        import streamlit as st

        steps = app_mod.breadcrumb("Mérignac", "23", "Quinconces, vers La Boétie")
        assert [label for label, _ in steps] == ["Tout le réseau", "Mérignac", "Ligne 23", "Quinconces, vers La Boétie"]
        assert steps[-1][1] is None and all(cb is not None for _, cb in steps[:-1])
        assert app_mod.breadcrumb(None, None, None) == [("Tout le réseau", None)]
        st.session_state.update({"commune_idx": 2, "line_id": "23", "stop_id": "s1", "sidebar_nav": app_mod.PAGE_TERRITORY})
        steps[2][1]()
        assert (st.session_state["stop_id"], st.session_state["sidebar_nav"]) == (None, app_mod.PAGE_LINE)
        steps[1][1]()
        assert (st.session_state["line_id"], st.session_state["commune_idx"]) == (None, 2)
        steps[0][1]()
        assert st.session_state["commune_idx"] == 0

    def test_carte_des_temps_perdus(self):
        assert app_mod.contention_view({"sidebar_nav": app_mod.PAGE_TERRITORY, "map_view": app_mod.VIEW_CONTENTION})
        assert not app_mod.contention_view({"sidebar_nav": app_mod.PAGE_TERRITORY, "map_view": app_mod.VIEW_STOPS})
        assert not app_mod.contention_view({"sidebar_nav": app_mod.PAGE_HOME, "map_view": app_mod.VIEW_CONTENTION})

    def test_trois_entrees_puis_pied_de_menu(self):
        assert app_mod.MAIN_PAGES == ["Accueil", "Carte", "Lignes"]
        assert app_mod.NAV_ITEMS[3:] == ["Rapports mensuels", "Méthode et lexique", "À propos"]
        assert set(app_mod.PAGES) == set(app_mod.NAV_ITEMS)

    def test_historique_borne_et_sans_doublon(self):
        session = {"sidebar_nav": "x"}
        app_mod.remember(session)
        app_mod.remember(session)
        assert len(session[app_mod.HISTORY_KEY]) == 1
        for i in range(app_mod.HISTORY_MAX + 5):
            session["line_id"] = str(i)
            app_mod.remember(session)
        assert len(session[app_mod.HISTORY_KEY]) == app_mod.HISTORY_MAX

    def test_libelle_du_retour(self):
        names = ({"11": "11"}, {"s9": "Gambetta"})
        assert app_mod.back_label(None, ["Bègles"], *names) is None
        assert app_mod.back_label({"sidebar_nav": app_mod.PAGE_TERRITORY, "stop_id": "s9"}, [], *names) == "l'arrêt Gambetta"
        assert app_mod.back_label({"sidebar_nav": app_mod.PAGE_LINE, "line_id": "11"}, [], *names) == "la ligne 11"
        assert app_mod.back_label({"sidebar_nav": "Quand ?", "commune_idx": 1}, ["Bègles"], *names) == "« Quand ? », Bègles"

    def test_options_de_la_recherche(self):
        ranking = pd.DataFrame({"route_id": ["A", "B"], "route_type": [0, 3], "ligne": ["A", "12"],
                                "score_fiabilite": [85.0, 40.0]})
        stops = pd.DataFrame({"stop_id": ["q1", "q2", "q3"], "stop_name": ["Gambetta", "Gambetta", "Victoire"],
                              "observations": [10, 900, 50], "lignes": ["4", "4, 15", "A, B"]})
        options = app_mod.search_options(["Bègles", "Mérignac"], ranking, None, stops,
                                         {"B": "Gare ↔ Parc"}, {"q1": "Bordeaux", "q2": "Bordeaux", "q3": "Bordeaux"})
        assert list(options.values()) == [
            "Commune : Bègles", "Commune : Mérignac",
            "Ligne 12 : Gare ↔ Parc (problématique)", "Ligne A (fiable)",
            "Arrêt : Gambetta, Bordeaux (lignes 4, 15)", "Arrêt : Victoire, Bordeaux (lignes A, B)"]
        assert "s:q2" in options and "s:q1" not in options
        assert list(options)[:2] == ["c:1", "c:2"]


class TestLexique:
    def test_identifiants_des_termes(self):
        assert app_mod.lexique_slug("À l'heure (ponctualité)") == "a-l-heure-ponctualite"
        assert app_mod.lexique_slug("Retards de plus de 5 min") == "retards-de-plus-de-5-min"
        slugs = [app_mod.lexique_slug(term) for term, _ in app_mod.LEXIQUE]
        assert len(slugs) == len(set(slugs))

    def test_chaque_aide_renvoie_a_un_terme_existant(self):
        terms = {term for term, _ in app_mod.LEXIQUE}
        assert set(app_mod.HELP_TERMS) == set(app_mod.HELP_TEXTS)
        assert set(app_mod.HELP_TERMS.values()) <= terms

    def test_lien_direct_ouvre_le_lexique(self):
        import streamlit as st

        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.query_params.clear()
        st.query_params["lexique"] = "jour-degrade"
        try:
            app_mod.apply_query_params()
            assert st.session_state["sidebar_nav"] == app_mod.PAGE_DATA
            assert st.session_state["data_view"] == "Lexique"
            assert st.session_state["lexique_focus"] == "jour-degrade"
            assert app_mod.should_show_guide({}, {"lexique": "jour-degrade"}) is False
        finally:
            st.query_params.clear()
            for k in list(st.session_state.keys()):
                del st.session_state[k]


class TestNombresALaFrancaise:
    def test_virgule_et_signe(self):
        assert app_mod.format_number(75.06, 1) == "75,1"
        assert app_mod.format_number(-9.94, 1, signed=True) == "−9,9"
        assert app_mod.format_number(3.5, 1, signed=True) == "+3,5"
        assert app_mod.format_number(None) == "—"

    def test_formateur_de_tableau(self):
        fmt = app_mod.number_formatter(2, " %")
        assert fmt(1.234) == "1,23 %"
        assert fmt(float("nan")) == "—"


class TestMouvementEtClavier:
    def test_cle_de_fiche(self):
        assert app_mod.panel_key("9749") == "9749"
        assert app_mod.panel_key("TBM:12 a") == "TBM_12_a"

    def test_defilement_et_apparition_respectent_le_reglage(self, monkeypatch):
        import streamlit as st

        scripts = []
        monkeypatch.setattr(st, "html", lambda body, **kw: scripts.append(body))
        st.session_state["_scroll_to"] = "fiche-arret"
        app_mod.scroll_if_requested("fiche-arret")
        assert "prefers-reduced-motion: reduce" in scripts[0] and "calm?'auto':'smooth'" in scripts[0]
        css = []
        monkeypatch.setattr(st, "markdown", lambda body, **kw: css.append(body))
        app_mod.inject_style()
        style = css[0]
        assert "@media (prefers-reduced-motion: no-preference)" in style and "fiche-in 260ms" in style
        assert '[data-testid="stHeaderActionElements"] {{ display: none; }}'.replace("{{", "{").replace("}}", "}") in style


class TestEchelleTypographique:
    def test_toutes_les_tailles_du_tableau_de_bord_viennent_de_l_echelle(self):
        import re
        from pathlib import Path

        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        sizes = re.findall(r"font-size: ?([^;}\"']+)", source)
        assert sizes
        assert all(size.strip().startswith("var(--fs-") for size in sizes), sorted(set(sizes))
        for token in ("--fs-page", "--fs-figure", "--fs-section", "--fs-block", "--fs-body", "--fs-small",
                      "--fs-label", "--fs-micro"):
            assert f"{token}:" in source

    def test_pas_d_etiquettes_en_capitales(self):
        from pathlib import Path

        assert "text-transform: uppercase" not in Path(app_mod.__file__).read_text(encoding="utf-8")


class TestIndicateursParGroupe:
    def _raw(self):
        return pd.DataFrame({
            "date_service": pd.to_datetime(["2026-09-05", "2026-09-05", "2026-09-07"]),
            "route_type": [0, 3, 3],
            "obs": [100, 100, 200], "sum_delay": [6000, 12000, 20000],
            "cnt_le300": [90, 72, 180], "cnt_gt300": [10, 28, 20], "cnt_lt60": [0, 2, 0],
            "skipped": [0, 10, 0], "eligible": [100, 100, 200]})

    def _core(self):
        return app_mod.ind.add_components(self._raw())

    def test_ensemble_et_modes_par_jour(self):
        g = app_mod.group_metrics(self._core(), "date_service")
        ensemble = g[(g["groupe"] == "Ensemble") & (g["date_service"] == pd.Timestamp("2026-09-05"))].iloc[0]
        assert ensemble["route_type"] == -1
        assert ensemble["observations"] == 200
        assert ensemble["pct_a_l_heure"] == 80.0
        assert ensemble["pct_retard_5min"] == 19.0
        assert ensemble["retard_moyen_s"] == 90.0
        assert ensemble["pct_arrets_sautes"] == 5.0
        assert ensemble["score_fiabilite"] == pytest.approx((90 + 70 / 100 * 90) / 200 * 100)
        assert set(g["groupe"]) == {"Ensemble", "Tramway", "Bus"}

    def test_toute_la_periode_et_score_global(self):
        g = app_mod.group_metrics(self._core(), None)
        assert len(g) == 3
        assert app_mod.period_totals(g) == pytest.approx((90 + 63 + 180) / 400 * 100)
        assert app_mod.period_totals(g.head(0)) == 0.0

    def test_sans_composantes_pas_de_score(self):
        hourly = self._raw().drop(columns=["skipped", "eligible"]).assign(heure=8)
        hourly["on_time"] = hourly["cnt_le300"] - hourly["cnt_lt60"]
        g = app_mod.group_metrics(hourly, "heure")
        assert "score_fiabilite" not in g.columns
        assert g[g["groupe"] == "Ensemble"]["pct_a_l_heure"].iloc[0] == pytest.approx(340 / 400 * 100)
        assert app_mod.group_metrics(hourly.head(0), "heure").empty

    def test_service_et_arrets_non_desservis_semaine_et_week_end(self):
        rates = app_mod.period_day_rates(self._core())
        assert rates == {"semaine": {"service": 100.0, "skip": 0.0}, "week-end": {"service": 95.0, "skip": 5.0}}
        assert app_mod.period_day_rates(self._core().head(0)) == {
            "semaine": {"service": 100.0, "skip": 0.0}, "week-end": {"service": 100.0, "skip": 0.0}}


class TestTerritoireEtFiches:
    def test_phrase_verdict_selon_la_marge(self):
        base = "Sur septembre 2026, le réseau est <b>à surveiller</b> : 75 passages sur 100 ont été assurés et à l'heure"
        drop = {"ecart": -3.2, "marge": 1.7, "significatif": True}
        assert app_mod.verdict_sentence("le réseau", "Septembre 2026", 74.9, drop, "Août 2026") == (
            base + ", <b>en baisse de 3,2 points</b> par rapport à août 2026 (plus que la marge d'incertitude de ± 1,7).")
        stable = {"ecart": -0.4, "marge": 1.7, "significatif": False}
        assert "stable par rapport à août 2026 : l'écart (−0,4) reste dans la marge" in app_mod.verdict_sentence(
            "le réseau", "Septembre 2026", 74.9, stable, "Août 2026")
        no_margin = {"ecart": -2.7, "marge": None, "significatif": None}
        assert app_mod.verdict_sentence("l'arrêt Gare", "Septembre 2026", 85.0, no_margin, "Août 2026").endswith(
            "▼ 2,7 points par rapport à août 2026.")
        assert app_mod.verdict_sentence("le réseau", "Septembre 2026", 74.9, None, None) == base + "."

    def _lines(self, scores, margins, doubtful=None):
        n = len(scores)
        return pd.DataFrame({"route_id": [str(i) for i in range(n)], "score_v2": scores, "marge_v2": margins,
                             "temps_reel_douteux": doubtful or [False] * n})

    def test_trois_choses_fondees_sur_la_marge(self):
        change = {"score": 74.9, "marge": 1.3, "previous": 78.2,
                  "compare": {"ecart": -3.3, "marge": 1.7, "significatif": True}}
        now = self._lines([60.0, 70.0, 40.0, 30.0], [2.0, 2.0, 3.0, 20.0], [False, False, False, True])
        before = self._lines([72.0, 71.0, 41.0, 60.0], [2.0, 2.0, 3.0, 5.0])
        names = {"0": "5", "1": "6", "2": "S35", "3": "X"}
        points = app_mod.key_points("le réseau", "du réseau", change, "Août 2026", now, before, names)
        assert len(points) == 3
        assert "a <b>baissé de 3,3 points</b> par rapport à août 2026" in points[0]["text"]
        assert points[1]["route_id"] == "0" and "La ligne <b>5</b> a le plus baissé" in points[1]["text"]
        assert (points[0]["figure"], points[1]["figure"], points[2]["figure"]) == ("▼ 3,3 pts", "72 → 60", "40/100")
        assert points[2]["route_id"] == "2" and "La ligne <b>S35</b> est la moins fiable" in points[2]["text"]

    def test_trois_choses_sans_ecart_reel(self):
        change = {"score": 74.9, "marge": 1.3, "previous": 75.2,
                  "compare": {"ecart": -0.3, "marge": 1.7, "significatif": False}}
        now = self._lines([74.0, 50.0], [2.0, 30.0])
        before = self._lines([75.0, 70.0], [2.0, 30.0])
        points = app_mod.key_points("Mérignac", "de Mérignac", change, "Août 2026", now, before, {})
        assert "<b>stable</b>" in points[0]["text"]
        assert points[1]["text"] == "Aucune ligne n'a baissé plus que sa marge d'incertitude par rapport à août 2026."
        assert (points[1]["figure"], points[1]["route_id"]) == ("Aucune", None)
        assert points[0]["figure"] == "Stable" and "dans la marge (± 1,7)" in points[0]["detail"]
        assert points[2]["route_id"] is None and "nettement en dessous de Mérignac" in points[2]["text"]
        assert "Le score de Mérignac est <b>stable</b>" in points[0]["text"]
        assert app_mod._scope_words(None) == ("le réseau", "du réseau")
        assert app_mod._scope_words("Eysines")[1] == "d'Eysines"

    def test_arrets_a_traiter_problematiques_par_passages_rates(self):
        territorial = pd.DataFrame({
            "stop_id": ["a1", "a2", "b1", "c1"], "stop_name": ["Gare", "Gare", "Parc", "Port"],
            "observations": [400, 600, 60, 10], "attendus": [400.0, 600.0, 60.0, 10.0],
            "rates": [40.0, 110.0, 50.0, 9.0], "route_type": [3, 3, 3, 3]})
        communes = {"a1": "Bordeaux", "a2": "Bordeaux", "b1": "Pessac"}
        top = app_mod.stops_to_treat(territorial, communes)
        assert top["stop_name"].tolist() == ["Parc"] and round(top.iloc[0]["score"], 1) == 16.7
        everything = app_mod.stops_to_treat(territorial, communes, max_score=None)
        assert everything["stop_name"].tolist() == ["Gare", "Parc"]
        assert everything.iloc[0]["stop_id"] == "a2" and everything.iloc[0]["rates"] == 150.0
        assert round(everything.iloc[0]["score"], 1) == 85.0
        assert app_mod.stops_to_treat(territorial.head(0), {}).empty

    def test_legende_de_la_carte(self):
        legend = app_mod.map_legend_html()
        for text in ("Couleur", "Forme", "80 : fiable", "50 : problématique", "linear-gradient",
                     "●</span>Tramway", "■</span>Bus", "▲</span>Ferry"):
            assert text in legend
        assert "Taille" not in legend

    def test_bandeau_type_de_retard(self):
        banner = app_mod.zone_banner_html(30.0, 22.0)
        assert "Type de retard" in banner
        assert "Retards rares mais longs" in banner
        assert "22,0 %" in banner
        assert "Retards fréquents mais courts" in app_mod.zone_banner_html(400.0, 8.0)
        early = app_mod.zone_banner_html(-201.0, 0.0)
        assert "Écart à l&#x27;horaire publié" in early and "−3 min 21 s" in early
        assert "congestion" not in early and "avance" not in early
        assert app_mod.zone_banner_html(None, 8.0) == ""
        assert app_mod.zone_banner_html(float("nan"), 8.0) == ""


class TestNoteDExclusion:
    def test_jours_et_lignes(self):
        note = app_mod.exclusion_note(app_mod.ind.Exclusions(("2026-09-08", "2026-09-24"), ("102",)))
        assert note == ("Chiffres calculés hors 2 jour(s) à collecte incomplète (08/09, 24/09) et 1 ligne(s) au "
                        "temps réel douteux : détail dans « Méthode et lexique ».")

    def test_rien_a_signaler(self):
        assert app_mod.exclusion_note(app_mod.ind.Exclusions()) is None
