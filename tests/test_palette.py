"""Tests de la palette partagée : seuils et KPI (seule source de vérité)."""

import pytest

from palette import (
    COPPERWOOD,
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