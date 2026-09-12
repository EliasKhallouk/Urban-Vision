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
    def test_hex_rgb(self):
        assert app_mod._hex_rgb("#283618") == (40, 54, 24)
        assert app_mod._hex_rgb("#bc6c25") == (188, 108, 37)

    def test_score_rgb_suit_les_paliers(self):
        assert app_mod._score_rgb(90.0) == (96, 108, 56)
        assert app_mod._score_rgb(60.0) == (221, 161, 94)
        assert app_mod._score_rgb(20.0) == (188, 108, 37)

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