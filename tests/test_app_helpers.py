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
        assert "background-color: #606c38" in style
        assert "color: #FEFAE0" in style
        style = app_mod._score_tier_style(60.0)
        assert "background-color: #DDA15E" in style
        assert "color: #283618" in style


class TestKpiCard:
    def test_bordure_par_polarite(self):
        assert app_mod._kpi_border("positif") == "#606c38"
        assert app_mod._kpi_border("negatif") == "#bc6c25"
        assert app_mod._kpi_border("moyen") == "#DDA15E"
        assert app_mod._kpi_border("autre") == "rgba(221, 161, 94, 0.50)"

    def test_kpi_card_sans_sublabel(self):
        card = app_mod.kpi_card("Passages", "1 234")
        assert 'class="kpi-label">Passages' in card
        assert 'class="kpi-value">1 234' in card
        assert "kpi-sublabel" not in card

    def test_kpi_card_avec_sublabel(self):
        card = app_mod.kpi_card("Retard moyen", "1 min", "≤ 5 min", polarity="positif")
        assert 'style="border-left-color:#606c38"' in card
        assert "kpi-sublabel" in card


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
    def test_score_combine_ponctualite_et_arrets_sautes(self):
        scheduled, skipped = _ranking_inputs()
        ranking = app_mod.make_ranking(scheduled, skipped)
        row_a = ranking[ranking["route_id"] == "A"].iloc[0]
        row_b = ranking[ranking["route_id"] == "B"].iloc[0]
        assert round(row_a["pct_arrets_sautes"], 6) == 10.0
        assert round(row_a["score_fiabilite"], 6) == 50.0
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

    def test_score_borne_a_zero(self):
        scheduled = pd.DataFrame(
            {
                "route_id": ["A"], "ligne": ["L"], "route_type": [3],
                "observations": [10], "retard_moyen_s": [1.0], "retard_median_s": [1.0],
                "pct_a_l_heure": [10.0], "pct_retard_5min": [90.0], "pct_avance_1min": [0.0],
            }
        )
        skipped = pd.DataFrame(
            {
                "route_id": ["A"], "ligne": ["L"], "route_type": [3],
                "skipped": [50], "eligible": [100],
            }
        )
        ranking = app_mod.make_ranking(scheduled, skipped)
        assert ranking.iloc[0]["score_fiabilite"] == 0.0


def _daily_core():
    return pd.DataFrame(
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
    )


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
        assert 'style="background:#bc6c25;color:#FEFAE0;outline:2px solid #283618' in html
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
        base = {"disponible": True, "score": 72.34, "marge": 2.1, "ponctualite": 80.0, "service": 93.25,
                "attente_excedentaire": 45.0, "lignes_ecartees": {}}
        return {**base, **kw}

    def test_phrase_complete(self):
        assert app_mod.method_v2_caption(self._v2(), "A") == (
            "Méthode 2.0 (en test) : score 72.3 ± 2.1 / 100 · ponctualité stricte (de −1 à +5 min) 80.0 % · "
            "service assuré 93.2 % · attente excédentaire +45 s.")

    def test_sans_marge_ni_regularite(self):
        text = app_mod.method_v2_caption(self._v2(marge=None, attente_excedentaire=None), "A")
        assert "score 72.3 / 100" in text
        assert "attente" not in text

    def test_ligne_ecartee_et_indisponible(self):
        assert "non évaluée" in app_mod.method_v2_caption(self._v2(disponible=False, lignes_ecartees={"A": 31.0}), "A")
        assert app_mod.method_v2_caption(self._v2(disponible=False), "A") is None
        assert app_mod.method_v2_caption(None) is None


class TestVueRapide:
    def _ranking(self):
        return pd.DataFrame({"route_id": ["A", "B"], "route_type": [0, 3], "ligne": ["A", "12"],
                             "score_fiabilite": [85.0, 40.0], "pct_retard_5min": [5.0, 30.0],
                             "pct_arrets_sautes": [0.5, 2.0], "observations": [1000, 400]})

    def test_tableau_des_lignes_rapide_puis_detaille(self):
        quick = app_mod.lines_table(self._ranking(), {"B"}, detailed=False)
        assert list(quick.columns) == ["Ligne", "État", "Score / 100"]
        assert quick["État"].tolist() == ["Fiable", "Problématique"]
        assert quick["Ligne"].tolist() == ["● A", "■ ⚠ 12"]
        full = app_mod.lines_table(self._ranking(), set(), detailed=True)
        assert list(full.columns) == ["Ligne", "État", "Score / 100", "Retards > 5 min",
                                      "Arrêts non desservis", "Passages"]

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
        assert (label, value, polarity) == ("État", "Problématique", "negatif")
        assert sub == "score 47 / 100 · réseau : 88 / 100"
        assert "Fiable à partir de 80" in help_text

    def test_aide_echappee_dans_la_carte(self):
        html_card = app_mod.kpi_card("Score", "80", None, "neutral", 'Part des "passages" < 5 min')
        assert 'title="Part des &quot;passages&quot; &lt; 5 min"' in html_card
        assert "kpi-help" not in app_mod.kpi_card("Score", "80")

    def test_guide_au_premier_affichage_seulement(self):
        assert app_mod.should_show_guide({}, {}) is True
        assert app_mod.should_show_guide({"guide_seen": True}, {}) is False
        assert app_mod.should_show_guide({}, {"ligne": "59"}) is False

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
