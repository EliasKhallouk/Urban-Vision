"""Tests des configurations Highcharts : payloads JSON sérialisables, structures valides."""

import json

import pandas as pd
import pytest

import highcharts as hc
from palette import INK, SCREEN_NO_DATA, VIN, score_color, screen_hex


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
        assert config["series"][0]["data"][0]["color"] == score_color(42.0)
        assert config["series"][0]["data"][1]["color"] == score_color(85.3)

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
        assert [x["name"] for x in config["series"]] == ["Bus", "Tramway", "Zones"]
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
        assert bus["data"][0]["color"] == score_color(70.0)
        assert bus["data"][1]["color"] == score_color(80.0)

    def test_zone_de_risque_et_seuils_traces(self):
        config = hc.scatter_chart(self._df())
        bus = next(s for s in config["series"] if s["name"] == "Bus")
        assert bus["data"][0]["zone"] == "Retards fréquents mais courts"
        assert bus["data"][1]["zone"] == "Risque faible"
        assert config["xAxis"]["plotLines"][0]["value"] == 60.0
        assert config["yAxis"]["plotLines"][0]["value"] == 15.0

    def test_libelles_des_zones_aux_quatre_coins(self):
        zones = hc.scatter_chart(self._df())["series"][-1]
        assert zones["enableMouseTracking"] is False and zones["showInLegend"] is False
        labels = {(d["dataLabels"]["align"], d["dataLabels"]["verticalAlign"]): d["dataLabels"]["format"]
                  for d in zones["data"]}
        assert labels == {("left", "top"): "Retards rares mais longs", ("right", "top"): "Zone critique",
                          ("right", "bottom"): "Retards fréquents mais courts", ("left", "bottom"): "Risque faible"}
        top_left = next(d for d in zones["data"] if d["dataLabels"]["align"] == "left")
        assert top_left["x"] == 0.0


class TestCreneauxEtGroupes:
    def test_libelle_de_creneau_avec_jours_et_heures(self):
        assert hc.period_category("Matin") == ('Matin<br><span style="font-size:12px">lun. à ven.<br>'
                                               '6\u00a0h à 10\u00a0h</span>')
        assert hc.period_category("Week-end").endswith("samedi<br>et dimanche</span>")
        assert hc.period_category("Autre") == "Autre"

    def test_score_par_creneau(self):
        df = pd.DataFrame({"période": ["Matin", "Week-end"], "score_fiabilite": [82.0, 45.0],
                           "pct_a_l_heure": [86.0, 50.0], "pct_arrets_sautes": [2.0, 2.5],
                           "observations": [1000, 400]})
        config = hc.period_score_chart(df)
        assert config["xAxis"]["categories"][0].startswith("Matin<br>")
        assert config["yAxis"]["max"] == 100
        point = config["series"][0]["data"][1]
        assert point["y"] == 45.0 and point["color"] == score_color(45.0) and point["skip"] == 2.5
        assert config["lexique"] == "score-de-fiabilite"
        _assert_json_serializable(config)

    def _daily(self):
        return pd.DataFrame({
            "date_service": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-01", "2026-09-02"]),
            "groupe": ["Ensemble", "Ensemble", "Bus", "Bus"], "route_type": [-1, -1, 3, 3],
            "score_fiabilite": [80.0, 70.0, 60.0, 50.0], "retard_moyen_s": [100.0, 120.0, 150.0, 170.0]})

    def test_tendance_ensemble_seul_avec_moyenne_glissante(self):
        config = hc.groups_trend_chart(self._daily(), "score_fiabilite", ["Ensemble"])
        assert [x["name"] for x in config["series"]] == ["Ensemble", "Moyenne sur 7 jours"]
        first = config["series"][0]["data"][0]
        assert first["x"] == int(pd.Timestamp("2026-09-01").timestamp() * 1000)
        assert first["color"] == score_color(80.0)
        assert config["series"][1]["data"][1][1] == 75.0
        assert config["yAxis"]["max"] == 100
        assert config["lexique"] == "score-de-fiabilite"
        _assert_json_serializable(config)

    def test_tendance_par_mode_avec_forme(self):
        config = hc.groups_trend_chart(self._daily(), "retard_moyen_s", ["Ensemble", "Bus", "Ferry"])
        assert [x["name"] for x in config["series"]] == ["Ensemble", "■ Bus"]
        assert config["series"][1]["marker"]["symbol"] == "square"
        assert "max" not in config["yAxis"]
        assert config["legend"]["enabled"] is True

    def test_heures_a_peu_de_passages_en_gris(self):
        assert hc.thin_hours({3: 2626, 18: 468385, 8: 408158}) == {3}
        assert hc.thin_hours({6: 4, 7: 359, 8: 393}) == {6}
        assert hc.thin_hours({}) == set()
        df = pd.DataFrame({"heure": [3, 18], "groupe": ["Ensemble"] * 2, "route_type": [-1, -1],
                           "pct_retard_5min": [58.0, 15.0], "observations": [2626, 468385]})
        series = hc.groups_hourly_chart(df, ["Ensemble"])["series"][0]
        assert series["data"][3]["color"] == SCREEN_NO_DATA and series["data"][3]["peu"] == 2626
        assert series["data"][18]["color"] == screen_hex(15.0, "pourcent")
        assert series["zones"][2] == {"value": 3, "color": SCREEN_NO_DATA, "dashStyle": "Dot"}
        assert series["zones"][5] == {"value": 6}
        assert "Peu de passages" in series["tooltip"]["pointFormat"]

    def test_retards_par_heure_et_par_groupe(self):
        df = pd.DataFrame({"heure": [8, 8], "groupe": ["Ensemble", "Tramway"], "route_type": [-1, 0],
                           "pct_retard_5min": [12.0, 4.0]})
        config = hc.groups_hourly_chart(df, ["Ensemble", "Tramway"])
        ensemble, tram = config["series"]
        assert len(ensemble["data"]) == 24
        assert ensemble["data"][8] == {"y": 12.0, "color": screen_hex(12.0, "pourcent")}
        assert ensemble["data"][0] == {"y": None}
        assert tram["marker"]["symbol"] == "circle"
        assert config["xAxis"]["labels"]["step"] == 2


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
            "À l'heure (−1 à +5 min)", "Retards > 5 min", "En avance > 1 min", "Arrêts non desservis"
        ]
        bus = config["series"][0]
        assert bus["name"] == "■ Bus"
        assert [d["y"] for d in bus["data"]] == [90.0, 5.0, 1.0, 0.5]
        assert bus["data"][0]["color"] == score_color(90.0)
        assert bus["data"][1]["color"] == screen_hex(5.0, "pourcent")
        assert bus["dataLabels"]["format"] == "■"

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
        assert [c.split("<br>")[0] for c in config["xAxis"]["categories"]] == ["Matin", "Journée"]
        bus = next(s for s in config["series"] if s["name"] == "■ Bus")
        assert bus["data"][0] == {"y": 20.0, "color": screen_hex(20.0, "pourcent")}
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
        assert data[0]["color"] == screen_hex(900, "retard")
        assert data[1]["color"] == screen_hex(30, "retard")
        assert data[2]["color"] == screen_hex(1500, "retard")

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
    def _prog(self):
        return pd.DataFrame(
            {
                "ligne": ["L1", "L2"],
                "delta_score": [-20.0, 40.0],
                "score_fiabilite": [60.0, 90.0],
                "score_fiabilite_prev": [80.0, 50.0],
            }
        )

    def test_progression_neutre_quel_que_soit_le_signe(self):
        config = hc.engagement_progression_chart(self._prog())
        assert config["xAxis"]["categories"] == ["L1", "L2"]
        assert config["series"][0]["data"][0]["color"] == INK
        assert config["series"][0]["data"][1]["color"] == INK
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
        assert config["series"][0]["data"][2]["color"] == VIN
        _assert_json_serializable(config)

    def test_profil_d_un_creneau(self):
        sp = self._profile().assign(slot_delay=[30.0, None, 400.0, 380.0, 100.0],
                                    usual_delay=[20.0, 25.0, 60.0, 70.0, 80.0])
        config = hc.slot_profile_chart(sp, "Vendredi de 13 h à 14 h", highlight_stop_id="c")
        assert config["series"][0]["name"] == "Vendredi de 13 h à 14 h"
        assert config["series"][0]["data"] == [30, None, 400, 380, 100]
        assert config["series"][1]["data"][2] == 60
        assert config["xAxis"]["plotLines"][0]["value"] == 2
        _assert_json_serializable(config)


class TestLegendes:
    def test_couleur_par_defaut_de_la_charte(self):
        assert hc.LIGHT_THEME["colors"][0] == INK

    def test_pas_de_legende_sur_les_graphiques_a_une_serie(self):
        assert hc.ranking_chart(_ranking_df())["legend"] == {"enabled": False}
        t = pd.DataFrame({"période": ["Matin"], "obs": [10], "pct_gt300": [20.0]})
        assert hc.risk_by_label_chart(t, "période")["legend"] == {"enabled": False}

    def test_serie_du_profil_a_une_couleur_de_legende(self):
        p = pd.DataFrame({"stop_id": ["a"], "stop_name": ["A"], "order": [1], "prev_stop_name": [None],
                          "gain_s": [0.0], "delay_s": [10.0]})
        assert hc.line_profile_chart(p)["series"][0]["color"] == hc.INK_35


class TestInfobulles:
    def test_francais_et_unites(self):
        assert hc.LIGHT_THEME["lang"]["decimalPoint"] == ","
        assert hc.LIGHT_THEME["lang"]["months"][8] == "septembre"
        assert hc.unit_tooltip(" %")["pointFormat"] == "{series.name} : <b>{point.y:,.1f} %</b><br/>"
        assert hc.unit_tooltip(" s", 0, header="H")["headerFormat"] == "H"

    def test_chaque_graphique_renvoie_a_un_terme_du_lexique(self):
        import app as app_mod

        slugs = {app_mod.lexique_slug(term) for term, _ in app_mod.LEXIQUE}
        hourly = pd.DataFrame({"heure": [7, 8], "pct_retard_5min": [10.0, 20.0], "observations": [5, 6]})
        groups = hourly.assign(groupe="Ensemble", route_type=-1)
        configs = [hc.groups_hourly_chart(groups, ["Ensemble"]), hc.hourly_distribution_chart(hourly),
                   hc.ranking_chart(_ranking_df())]
        for config in configs:
            assert config["lexique"] in slugs
            assert config["series"][0]["tooltip"]
        source = (hc.__file__ and open(hc.__file__, encoding="utf-8").read())
        import re
        assert set(re.findall(r'"lexique": "([a-z0-9-]+)"', source)) <= slugs
        trend = pd.DataFrame({"date_service": ["2026-09-01", "2026-09-02"], "groupe": ["Ensemble"] * 2,
                              "route_type": [-1, -1], "score_fiabilite": [88.0, 87.0], "pct_a_l_heure": [90.0, 91.0],
                              "pct_retard_5min": [8.0, 7.0], "pct_arrets_sautes": [1.0, 1.5],
                              "retard_moyen_s": [60.0, 55.0]})
        for metric in hc.TREND_METRICS:
            config = hc.groups_trend_chart(trend, metric, ["Ensemble"])
            assert config["lexique"] in slugs
            assert config["series"][0]["tooltip"]["pointFormat"].endswith(f"{hc.TREND_METRICS[metric][1]}</b><br/>")

    def test_lien_lexique_dans_le_html(self):
        html = hc._html({"chart": {"type": "line"}, "series": [], "lexique": "retard-moyen"}, 300)
        assert "Définitions dans le lexique" in html
        assert '.replace("__TERME__", "retard-moyen")' in html
        assert '"lexique"' not in html.split("Highcharts.chart(")[1]
