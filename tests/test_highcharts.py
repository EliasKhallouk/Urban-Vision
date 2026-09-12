"""Tests des configurations Highcharts : payloads JSON sérialisables, structures valides."""

import json

import pandas as pd
import pytest

import highcharts as hc
from palette import BLACK_FOREST, OLIVE_LEAF, SUNLIT_CLAY, hex as palette_hex


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
                "mode_color": ["#2A6F6F", "#2A6F6F", "#bc6c25"],
                "retard_median_s": [100.0, 50.0, 90.0],
                "pct_retard_5min": [10.0, 5.0, 8.0],
                "observations": [100, 200, 300],
                "ligne": ["L1", "L2", "M1"],
                "score_fiabilite": [70.0, 80.0, 60.0],
            }
        )

    def test_serie_par_mode_avec_couleur_dediee(self):
        config = hc.scatter_chart(self._df())
        assert len(config["series"]) == 2
        bus = next(s for s in config["series"] if s["name"] == "Bus")
        assert bus["color"] == "#2A6F6F"
        assert len(bus["data"]) == 2
        assert bus["data"][0]["score"] == 70.0
        assert bus["data"][0]["z"] == 100
        tram = next(s for s in config["series"] if s["name"] == "Tramway")
        assert tram["data"][0]["name"] == "M1"
        _assert_json_serializable(config)


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

    def test_hourly_risk_chart_compatible_threshold(self):
        df = pd.DataFrame({"heure": [8], "pct_retard_5min": [10.0]})
        config = hc.hourly_risk_chart(df, threshold=80.0)
        assert len(config["series"][0]["data"]) == 24

    def test_timeline_chart(self):
        df = pd.DataFrame(
            {
                "date_service": pd.to_datetime(["2026-09-01", "2026-09-02"]),
                "pct_retard_5min": [9.0, 14.5],
            }
        )
        config = hc.timeline_chart(df)
        assert config["chart"]["type"] == "area"
        assert config["series"][0]["data"][0][1] == 9.0


class TestModeCharts:
    def test_mode_comparison_chart_4_metriques(self):
        df = pd.DataFrame(
            {
                "route_type": [3, 0],
                "mode": ["Bus", "Tramway"],
                "mode_color": ["#2A6F6F", "#bc6c25"],
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
        assert bus["name"] == "Bus"
        assert bus["data"] == [90.0, 5.0, 1.0, 0.5]

    def _mode_daily_df(self):
        return pd.DataFrame(
            {
                "mode": ["Bus", "Bus", "Tramway"],
                "mode_color": ["#2A6F6F", "#2A6F6F", "#bc6c25"],
                "route_type": [3, 3, 0],
                "date_service": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-01"]),
                "pct_retard_5min": [6.0, 7.0, 2.0],
            }
        )

    def test_mode_daily_chart(self):
        config = hc.mode_daily_chart(self._mode_daily_df())
        names = [s["name"] for s in config["series"]]
        assert names == ["Bus", "Tramway"]
        bus = config["series"][0]
        assert bus["color"] == "#2A6F6F"
        assert len(bus["data"]) == 2

    def test_mode_hourly_chart_marque_les_trous(self):
        df = pd.DataFrame(
            {
                "mode": ["Bus"],
                "mode_color": ["#2A6F6F"],
                "route_type": [3],
                "heure": [8],
                "pct_retard_5min": [10.0],
            }
        )
        config = hc.mode_hourly_chart(df)
        assert len(config["series"][0]["data"]) == 24


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