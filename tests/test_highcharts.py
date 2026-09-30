"""Tests des configurations Highcharts : payloads JSON sérialisables, structures valides."""

import json

import pandas as pd
import pytest

import highcharts as hc
from palette import BLACK_FOREST, COPPERWOOD, OLIVE_LEAF, SUNLIT_CLAY, hex as palette_hex


def _assert_json_serializable(config):
    json.dumps(config)  # ne doit pas lever


def _ranking_df():
    return pd.DataFrame(
        {
            "route_id": ["A", "B"],
            "ligne": ["Ligne A", "Ligne B"],
            "ligne_plot": ["⚠ Ligne A", "Ligne B"],
            "score_fiabilite": [42.0, 85.3],
        }
    )


class TestRankingChart:
    def test_trie_par_score_croissant(self):
        config = hc.ranking_chart(_ranking_df())
        assert config["xAxis"]["categories"] == ["⚠ Ligne A", "Ligne B"]
        assert config["series"][0]["data"][0]["y"] == 42.0
        assert config["series"][0]["data"][1]["y"] == 85.3
        _assert_json_serializable(config)

    def test_couleur_par_palier_score(self):
        config = hc.ranking_chart(_ranking_df())
        assert config["series"][0]["data"][0]["color"] == palette_hex(42.0, "score")
        assert config["series"][0]["data"][1]["color"] == palette_hex(85.3, "score")

    def test_sans_colonne_ligne_plot_utilise_ligne(self):
        df = _ranking_df().drop(columns=["ligne_plot"])
        config = hc.ranking_chart(df)
        assert config["xAxis"]["categories"] == ["Ligne A", "Ligne B"]


class TestScatterChart:
    def _df(self):
        return pd.DataFrame(
            {
                "mode": ["Bus", "Bus", "Tramway"],
                "route_type": [3, 3, 0],
                "retard_median_s": [100.0, 50.0, 90.0],
                "pct_retard_5min": [10.0, 5.0, 8.0],
                "observations": [100, 200, 300],
                "ligne": ["L1", "L2", "M1"],
                "score_fiabilite": [70.0, 80.0, 60.0],
            }
        )

    def test_serie_par_mode_forme_dediee(self):
        config = hc.scatter_chart(self._df())
        assert len(config["series"]) == 2
        bus = next(s for s in config["series"] if s["name"] == "Bus")
        assert bus["marker"]["symbol"] == "square"
        assert len(bus["data"]) == 2
        assert bus["data"][0]["score"] == 70.0
        assert bus["data"][0]["z"] == 100
        tram = next(s for s in config["series"] if s["name"] == "Tramway")
        assert tram["marker"]["symbol"] == "circle"
        assert tram["data"][0]["name"] == "M1"
        _assert_json_serializable(config)

    def test_couleur_du_point_par_palier_de_score(self):
        config = hc.scatter_chart(self._df())
        bus = next(s for s in config["series"] if s["name"] == "Bus")
        assert bus["data"][0]["color"] == palette_hex(70.0, "score")
        assert bus["data"][1]["color"] == palette_hex(80.0, "score")

    def test_zone_de_risque_et_seuils_traces(self):
        config = hc.scatter_chart(self._df())
        bus = next(s for s in config["series"] if s["name"] == "Bus")
        assert bus["data"][0]["zone"] == "Retards fréquents mais courts"
        assert bus["data"][1]["zone"] == "Risque faible"
        assert config["xAxis"]["plotLines"][0]["value"] == 60.0
        assert config["yAxis"]["plotLines"][0]["value"] == 15.0


class TestSerieTemporelles:
    def test_network_daily_chart_ms_epoch(self):
        df = pd.DataFrame(
            {
                "date_service": pd.to_datetime(["2026-09-01", "2026-09-02"]),
                "pct_retard_5min": [12.0, 3.0],
                "observations": [1000, 1200],
            }
        )
        config = hc.network_daily_chart(df)
        assert config["xAxis"]["type"] == "datetime"
        assert config["series"][0]["data"][0]["x"] == int(
            pd.Timestamp("2026-09-01").timestamp() * 1000
        )
        assert config["series"][0]["data"][0]["color"] == palette_hex(12.0, "pourcent")
        assert config["series"][0]["data"][1]["color"] == palette_hex(3.0, "pourcent")

    def test_network_hourly_chart_remplit_les_24_heures(self):
        df = pd.DataFrame({"heure": [8, 20], "pct_retard_5min": [26.5, 4.0]})
        config = hc.network_hourly_chart(df)
        assert config["xAxis"]["categories"] == [str(h) for h in range(24)]
        data = config["series"][0]["data"]
        assert len(data) == 24
        assert data[8] == {"y": 26.5, "color": palette_hex(26.5, "pourcent")}
        assert data[0] == {"y": 0, "color": SUNLIT_CLAY}
        assert data[20] == {"y": 4.0, "color": palette_hex(4.0, "pourcent")}


class TestModeCharts:
    def test_mode_comparison_chart_4_metriques(self):
        df = pd.DataFrame(
            {
                "route_type": [3, 0],
                "mode": ["Bus", "Tramway"],
                "pct_a_l_heure": [90.0, 85.0],
                "pct_retard_5min": [5.0, 8.0],
                "pct_avance_1min": [1.0, 2.0],
                "pct_arrets_sautes": [0.5, 1.0],
            }
        )
        config = hc.mode_comparison_chart(df)
        assert config["xAxis"]["categories"] == [
            "Ponctualité ≤ 5 min", "Retards > 5 min", "En avance > 1 min", "Arrêts sautés"
        ]
        bus = config["series"][0]
        assert bus["name"] == "■ Bus"
        assert [d["y"] for d in bus["data"]] == [90.0, 5.0, 1.0, 0.5]
        assert bus["data"][0]["color"] == palette_hex(90.0, "score")
        assert bus["data"][1]["color"] == palette_hex(5.0, "pourcent")
        assert bus["dataLabels"]["format"] == "■"

    def _mode_daily_df(self):
        return pd.DataFrame(
            {
                "mode": ["Bus", "Bus", "Tramway"],
                "route_type": [3, 3, 0],
                "date_service": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-01"]),
                "pct_retard_5min": [6.0, 7.0, 2.0],
            }
        )

    def test_mode_daily_chart(self):
        config = hc.mode_daily_chart(self._mode_daily_df())
        names = [s["name"] for s in config["series"]]
        assert names == ["Bus", "Tramway"]
        bus, tram = config["series"]
        assert bus["color"] == BLACK_FOREST
        assert bus["marker"]["symbol"] == "square"
        assert tram["marker"]["symbol"] == "circle"
        assert bus["dashStyle"] != tram["dashStyle"]
        assert len(bus["data"]) == 2
        assert bus["data"][0]["color"] == palette_hex(6.0, "pourcent")

    def test_mode_hourly_chart_marque_les_trous(self):
        df = pd.DataFrame(
            {
                "mode": ["Bus"],
                "route_type": [3],
                "heure": [8],
                "pct_retard_5min": [10.0],
            }
        )
        config = hc.mode_hourly_chart(df)
        data = config["series"][0]["data"]
        assert len(data) == 24
        assert data[8] == {"y": 10.0, "color": palette_hex(10.0, "pourcent")}
        assert data[0] == {"y": None}

    def test_period_mode_chart_glyphe_et_palier(self):
        df = pd.DataFrame(
            {
                "période": ["Matin", "Matin", "Journée"],
                "route_type": [3, 0, 3],
                "mode": ["Bus", "Tramway", "Bus"],
                "pct_retard_5min": [20.0, 3.0, 8.0],
            }
        )
        config = hc.period_mode_chart(df)
        assert config["xAxis"]["categories"] == ["Matin", "Journée"]
        bus = next(s for s in config["series"] if s["name"] == "■ Bus")
        assert bus["data"][0] == {"y": 20.0, "color": palette_hex(20.0, "pourcent")}
        assert bus["dataLabels"]["format"] == "■"
        tram = next(s for s in config["series"] if s["name"] == "● Tramway")
        assert tram["data"][1] == {"y": None}
        _assert_json_serializable(config)


class TestDistribution:
    def test_delay_distribution_chart_colore_par_depart_absolu(self):
        df = pd.DataFrame(
            {
                "plage": ["< −10 min", "0 à +1", "> +20 min"],
                "observations": [3, 50, 2],
            }
        )
        config = hc.delay_distribution_chart(df)
        data = config["series"][0]["data"]
        assert data[0]["y"] == 3
        assert data[1]["y"] == 50
        # écart absolu médian de chaque classe -> palette « retard »
        assert data[0]["color"] == palette_hex(900, "retard")
        assert data[1]["color"] == palette_hex(30, "retard")
        assert data[2]["color"] == palette_hex(1500, "retard")

    def test_collection_minutely_chart_stock_mas_zoom(self):
        df = pd.DataFrame(
            {
                "minute": pd.date_range("2026-09-11 08:00", periods=3, freq="min"),
                "observations": [1, 2, 3],
            }
        )
        config = hc.collection_minutely_chart(df)
        assert config["chart"]["zoomType"] == "x"
        assert "rangeSelector" in config
        assert config["series"][0]["data"][0][1] == 1
        jitter = json.dumps(config)
        assert "rangeSelector" in jitter

    def test_hourly_distribution_chart(self):
        df = pd.DataFrame({"heure": [8, 9], "observations": [10, 12]})
        config = hc.hourly_distribution_chart(df)
        assert config["series"][0]["data"] == [10, 12]
        assert config["xAxis"]["categories"] == ["8", "9"]


class TestEngagement:
    def _trend(self):
        return pd.DataFrame(
            {
                "date_service": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"]),
                "pct_a_l_heure": [90.0, 80.0, 70.0],
                "retard_moyen_s": [100.0, 110.0, 120.0],
            }
        )

    def test_trend_serie_et_moyenne_glissante(self):
        config = hc.engagement_trend_chart(self._trend(), "pct_a_l_heure")
        names = [s["name"] for s in config["series"]]
        assert names == ["Quotidien", "Moyenne 7 jours"]
        assert config["xAxis"]["type"] == "datetime"
        assert config["yAxis"]["max"] == 100
        assert config["series"][0]["data"][0][0] == int(
            pd.Timestamp("2026-09-01").timestamp() * 1000
        )
        assert config["series"][0]["data"][0][1] == 90.0
        assert config["series"][1]["data"][2][1] == 80.0  # moyenne des 3 points
        _assert_json_serializable(config)

    def test_trend_retard_moyen_sans_max(self):
        config = hc.engagement_trend_chart(self._trend(), "retard_moyen_s")
        assert "max" not in config["yAxis"]

    def _prog(self):
        return pd.DataFrame(
            {
                "ligne": ["L1", "L2"],
                "delta_score": [-20.0, 40.0],
                "score_fiabilite": [60.0, 90.0],
                "score_fiabilite_prev": [80.0, 50.0],
            }
        )

    def test_progression_colore_par_signe(self):
        config = hc.engagement_progression_chart(self._prog())
        assert config["xAxis"]["categories"] == ["L1", "L2"]
        assert config["series"][0]["data"][0]["color"] == COPPERWOOD
        assert config["series"][0]["data"][1]["color"] == OLIVE_LEAF
        assert config["series"][0]["data"][0]["y"] == -20.0
        _assert_json_serializable(config)


class TestHtml:
    def test_html_embarque_le_json_et_la_hauteur(self):
        config = {"chart": {"type": "bar"}, "series": []}
        html = hc._html(config, height=300)
        assert "Highcharts.chart(" in html
        assert "height:300px" in html
        assert '"type": "bar"' in html

    def test_id_stable_par_configuration(self):
        html_a = hc._html({"x": 1}, height=100)
        html_a2 = hc._html({"x": 1}, height=100)
        html_b = hc._html({"x": 2}, height=100)
        assert html_a == html_a2
        assert html_a != html_b

    def test_html_accessibilite_langue_module_et_description(self):
        config = {"chart": {"type": "bar"}, "series": [{"name": "Ligne A"}, {"name": "Ligne B"}]}
        html = hc._html(config, height=300)
        assert 'lang="fr"' in html
        assert "modules/accessibility.js" in html
        assert '"enabled": true' in html
        assert "Graphique en classement : Ligne A, Ligne B" in html

    def test_html_accessibilite_stock_sans_module_separe(self):
        html = hc._html({"chart": {"type": "line"}}, height=300, use_stock=True)
        assert "modules/accessibility.js" not in html
        assert "graphSeries" in html or '"enabled": true' in html

    def test_html_description_sans_series_nommees(self):
        html = hc._html({"chart": {"type": "line"}, "series": [{}]}, height=100)
        assert "Graphique en série temporelle" in html

class TestLisibilite:
    def test_aucun_format_de_nombre_invalide(self):
        import inspect

        assert ":,}" not in inspect.getsource(hc)

    def test_legendes_sans_glyphe_double(self):
        df = pd.DataFrame({"mode": ["Bus"], "route_type": [3], "retard_median_s": [50.0],
                           "pct_retard_5min": [5.0], "observations": [100], "ligne": ["L1"],
                           "score_fiabilite": [80.0]})
        scatter = hc.scatter_chart(df)
        assert scatter["series"][0]["name"] == "Bus"
        assert scatter["legend"]["bubbleLegend"]["enabled"] is True
        assert "{point.z:,.0f}" in scatter["series"][0]["tooltip"]["pointFormat"]
        comp = hc.mode_comparison_chart(pd.DataFrame({
            "route_type": [3], "mode": ["Bus"], "pct_a_l_heure": [90.0], "pct_retard_5min": [5.0],
            "pct_avance_1min": [1.0], "pct_arrets_sautes": [0.5]}))
        assert comp["series"][0]["name"] == "■ Bus"
        assert comp["legend"]["symbolWidth"] == 0


class TestProfils:
    def _profile(self):
        return pd.DataFrame({
            "stop_id": ["a", "b", "c", "d", "e"], "stop_name": list("ABCDE"), "order": [1, 2, 3, 4, 5],
            "prev_stop_name": [None, "A", "B", "C", "D"], "gain_s": [0.0, 10.0, 60.0, 5.0, 5.0],
            "delay_s": [10.0, 20.0, 80.0, 85.0, 90.0], "skipped": [0] * 5, "eligible": [10] * 5,
        })

    def test_bandes_de_commune_sur_les_suites_d_arrets(self):
        bands = hc.commune_bands(["a", "b", "c", "d", "e"], {"b", "c", "e"}, "Pessac")
        assert [(b["from"], b["to"]) for b in bands] == [(0.5, 2.5), (3.5, 4.5)]
        assert bands[0]["label"]["text"] == "Pessac"
        assert "label" not in bands[1]
        assert hc.commune_bands(["a"], set(), "Pessac") == []

    def test_profil_de_ligne_commune_et_arret(self):
        config = hc.line_profile_chart(self._profile(), highlight_stop_id="c", hotspot_stop_ids={"c"},
                                       commune_stop_ids={"d", "e"}, commune_label="Pessac")
        assert config["xAxis"]["plotLines"][0]["value"] == 2
        assert config["xAxis"]["plotBands"][0]["from"] == 2.5
        assert config["series"][0]["data"][2]["color"] == COPPERWOOD
        _assert_json_serializable(config)

    def test_profil_d_un_creneau(self):
        sp = self._profile().assign(slot_delay=[30.0, None, 400.0, 380.0, 100.0],
                                    usual_delay=[20.0, 25.0, 60.0, 70.0, 80.0])
        config = hc.slot_profile_chart(sp, "Vendredi 13 h–14 h", highlight_stop_id="c")
        assert config["series"][0]["name"] == "Vendredi 13 h–14 h"
        assert config["series"][0]["data"] == [30, None, 400, 380, 100]
        assert config["series"][1]["data"][2] == 60
        assert config["xAxis"]["plotLines"][0]["value"] == 2
        _assert_json_serializable(config)


class TestLegendes:
    def test_couleur_par_defaut_de_la_charte(self):
        assert hc.LIGHT_THEME["colors"][0] == BLACK_FOREST

    def test_pas_de_legende_sur_les_graphiques_a_une_serie(self):
        assert hc.ranking_chart(_ranking_df())["legend"] == {"enabled": False}
        t = pd.DataFrame({"période": ["Matin"], "obs": [10], "pct_gt300": [20.0]})
        assert hc.risk_by_label_chart(t, "période")["legend"] == {"enabled": False}

    def test_serie_du_profil_a_une_couleur_de_legende(self):
        p = pd.DataFrame({"stop_id": ["a"], "stop_name": ["A"], "order": [1], "prev_stop_name": [None],
                          "gain_s": [0.0], "delay_s": [10.0]})
        assert hc.line_profile_chart(p)["series"][0]["color"] == hc.BLACK_FOREST_35
