"""Tests de la palette partagée : seuils et KPI (seule source de vérité)."""

import pytest

import palette

from palette import (
    CRITICAL,
    COPPERWOOD,
    FREQUENT_SHORT,
    LOW,
    RARE_LONG,
    RISK_ZONE_LABELS,
    HEX,
    LATEX,
    MEDIUM,
    NEGATIVE,
    OLIVE_LEAF,
    POSITIVE,
    SUNLIT_CLAY,
    hex,
    kpi_hex,
    kpi_latex,
    kpi_tier,
    latex as palette_latex,
    mode_glyph,
    mode_marker,
    risk_zone,
    tier,
)


class TestSeuilsScore:
    def test_paliers_score(self):
        assert tier(0.0, "score") == NEGATIVE
        assert tier(49.99, "score") == NEGATIVE
        assert tier(50.0, "score") == MEDIUM
        assert tier(79.99, "score") == MEDIUM
        assert tier(80.0, "score") == POSITIVE
        assert tier(100.0, "score") == POSITIVE

    def test_hors_borne_retombe_sur_moyen(self):
        assert tier(150.0, "score") == MEDIUM


class TestSeuilsRetard:
    def test_paliers_retard(self):
        assert tier(0.0, "retard") == POSITIVE
        assert tier(59.0, "retard") == POSITIVE
        assert tier(60.0, "retard") == MEDIUM
        assert tier(179.0, "retard") == MEDIUM
        assert tier(180.0, "retard") == NEGATIVE


class TestSeuilsPourcent:
    def test_paliers_pourcent(self):
        assert tier(0.0, "pourcent") == POSITIVE
        assert tier(4.9, "pourcent") == POSITIVE
        assert tier(5.0, "pourcent") == MEDIUM
        assert tier(14.9, "pourcent") == MEDIUM
        assert tier(15.0, "pourcent") == NEGATIVE


class TestCouleurs:
    def test_hex_suit_les_paliers(self):
        assert hex(90.0, "score") == OLIVE_LEAF
        assert hex(60.0, "score") == SUNLIT_CLAY
        assert hex(20.0, "score") == COPPERWOOD

    def test_latex_suit_les_paliers(self):
        assert palette_latex(90.0, "score") == "olive"
        assert palette_latex(60.0, "score") == "sunlitclay"
        assert palette_latex(20.0, "score") == "alert"

    def test_trois_couleurs_uniquement_pas_de_degrades(self):
        assert set(HEX.values()) == {OLIVE_LEAF, SUNLIT_CLAY, COPPERWOOD}
        assert set(LATEX.values()) == {"olive", "sunlitclay", "alert"}


class TestKpi:
    def test_kpi_tier_par_cle(self):
        assert kpi_tier({"fiability": 90.0}, "fiability") == POSITIVE
        assert kpi_tier({"ponctualite": 60.0}, "ponctualite") == MEDIUM
        assert kpi_tier({"skip_rate": 20.0}, "skip_rate") == NEGATIVE
        assert kpi_tier({"retard": 90.0}, "retard") == MEDIUM
        assert kpi_tier({"retard_median": 250.0}, "retard_median") == NEGATIVE

    def test_retards_evalues_en_valeur_absolue(self):
        # un retard NÉGATIF (en avance) doit être comparé en valeur absolue
        assert kpi_tier({"retard": -90.0}, "retard") == MEDIUM
        assert kpi_tier({"retard": -59.0}, "retard") == POSITIVE

    def test_kpi_hex_et_latex_deleguent(self):
        assert kpi_hex({"fiability": 90.0}, "fiability") == OLIVE_LEAF
        assert kpi_latex({"skip_rate": 30.0}, "skip_rate") == "alert"
    def test_kpi_part_des_retards_de_plus_de_5_min(self):
        assert kpi_tier({"retard_5min": 4.0}, "retard_5min") == POSITIVE
        assert kpi_tier({"retard_5min": 10.0}, "retard_5min") == MEDIUM
        assert kpi_tier({"retard_5min": 40.0}, "retard_5min") == NEGATIVE


class TestModes:
    def test_forme_par_mode(self):
        assert mode_marker(0) == "circle"
        assert mode_marker(3) == "square"
        assert mode_marker(4) == "triangle"

    def test_mode_inconnu_ou_absent(self):
        assert mode_marker(7) == "diamond"
        assert mode_marker(None) == "diamond"
        assert mode_marker("3") == "square"

    def test_glyphes(self):
        assert [mode_glyph(rt) for rt in (0, 3, 4, 11)] == ["●", "■", "▲", "◆"]


class TestZonesDeRisque:
    def test_quatre_zones(self):
        assert risk_zone(30.0, 5.0) == LOW
        assert risk_zone(120.0, 5.0) == FREQUENT_SHORT
        assert risk_zone(30.0, 25.0) == RARE_LONG
        assert risk_zone(120.0, 25.0) == CRITICAL

    def test_seuils_inclus(self):
        assert risk_zone(60.0, 0.0) == FREQUENT_SHORT
        assert risk_zone(0.0, 15.0) == RARE_LONG

    def test_retard_median_en_valeur_absolue(self):
        assert risk_zone(-90.0, 0.0) == FREQUENT_SHORT

    def test_libelles_des_rapports(self):
        assert set(RISK_ZONE_LABELS.values()) == {
            "Zone critique", "Retards fréquents mais courts",
            "Retards rares mais longs", "Risque faible",
        }


class TestMotsDesPaliers:
    def test_mots_courants_par_palier(self):
        assert palette.tier_label(85) == "fiable"
        assert palette.tier_label(65) == "à surveiller"
        assert palette.tier_label(30) == "problématique"

    def test_mots_selon_le_jeu_de_seuils(self):
        assert palette.tier_label(30, "retard") == "fiable"
        assert palette.tier_label(400, "retard") == "problématique"

    def test_aucun_nom_de_couleur(self):
        assert set(palette.TIER_LABELS) == {palette.POSITIVE, palette.MEDIUM, palette.NEGATIVE}
        for word in palette.TIER_LABELS.values():
            assert not any(color in word.lower() for color in ("olive", "clay", "copper", "vert", "orange"))


class TestCharteEcran:
    def test_rapports_inchanges(self):
        assert HEX == {POSITIVE: OLIVE_LEAF, MEDIUM: SUNLIT_CLAY, NEGATIVE: COPPERWOOD}

    def test_dix_couleurs(self):
        couleurs = {palette.INK, palette.INK_MUTED, palette.SURFACE, palette.LINE, palette.ACTION,
                    *palette.SCALE, palette.SCREEN_NO_DATA}
        assert len(couleurs) == 10

    def test_paliers_ecran(self):
        assert palette.screen_hex(90.0, "score") == palette.GARONNE
        assert palette.screen_hex(60.0, "score") == palette.PIERRE
        assert palette.screen_hex(20.0, "score") == palette.VIN


class TestSecondesRetenues:
    @pytest.mark.parametrize("s,pct,attendu", [
        (100.0, 25.0, 15.0), (100.0, 50.0, 40.0), (100.0, 10.0, 6.0), (100.0, 60.0, 50.0),
        (20.0, 80.0, 20.0), (-12.0, -30.0, -18.0),
    ])
    def test_plafond_par_part_du_temps_prevu(self, s, pct, attendu):
        assert palette.retained_seconds(s, pct) == pytest.approx(attendu)

    @pytest.mark.parametrize("s", [-30.0, 0.0, 14.9, 15.0, 22.0, 39.9, 40.0, 41.0, 90.0])
    @pytest.mark.parametrize("pct", [-5.0, 0.0, 24.9, 25.0, 30.0, 49.9, 50.0, 51.0, 120.0])
    def test_equivaut_aux_paliers_de_la_regle_b(self, s, pct):
        palier = palette.troncon_tier(s, pct)
        retenu = palette.retained_seconds(s, pct)
        assert (palier == NEGATIVE) == (retenu >= 40.0)
        assert (palier != POSITIVE) == (retenu >= 15.0)


class TestDegrade:
    def test_reperes_des_troncons(self):
        assert palette.TRONCON_ANCHORS == (0.0, 15.0, 40.0, 60.0)
        assert palette.scale_color(-20.0) == palette.GARONNE
        assert palette.scale_color(15.0) == palette.PIERRE
        assert palette.scale_color(40.0) == palette.VIN
        assert palette.scale_color(300.0) == palette.LIE

    def test_interpolation_a_mi_chemin(self):
        assert palette.scale_color(27.5) == "#C39078"

    def test_reperes_des_scores_decroissants(self):
        assert palette.score_color(90.0) == palette.GARONNE
        assert palette.score_color(60.0) == palette.PIERRE
        assert palette.score_color(50.0) == palette.VIN
        assert palette.score_color(10.0) == palette.LIE

    def test_couleur_forte_des_le_palier_problematique(self):
        assert palette.score_color(49.0) != palette.score_color(51.0)
        assert palette.tier(49.0, "score") == NEGATIVE

    def test_troncon_color_passe_par_les_secondes_retenues(self):
        assert palette.troncon_color(100.0, 25.0) == palette.PIERRE
        assert palette.troncon_color(100.0, 50.0) == palette.VIN
