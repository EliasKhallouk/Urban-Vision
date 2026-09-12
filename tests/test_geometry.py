"""Tests des primitives géométriques de assign_stop_municipalities.py.

Coordonnées : (longitude, latitude), comme dans le GeoJSON des contours.
Aucun réseau, aucune donnée réelle : uniquement des polygones de contrôle.
"""

import pytest

from assign_stop_municipalities import (
    bounding_box,
    geometry_contains,
    point_in_polygon,
    point_in_ring,
    point_on_segment,
)

SQUARE = [
    [0.0, 0.0],
    [10.0, 0.0],
    [10.0, 10.0],
    [0.0, 10.0],
    [0.0, 0.0],
]

HOLE = [
    [3.0, 3.0],
    [7.0, 3.0],
    [7.0, 7.0],
    [3.0, 7.0],
    [3.0, 3.0],
]


class TestPointOnSegment:
    def test_point_colineaire_dans_le_segment(self):
        assert point_on_segment((5.0, 5.0), [0.0, 0.0], [10.0, 10.0]) is True

    def test_point_colineaire_hors_du_segment(self):
        assert point_on_segment((11.0, 11.0), [0.0, 0.0], [10.0, 10.0]) is False

    def test_point_non_colineaire(self):
        assert point_on_segment((6.0, 5.0), [0.0, 0.0], [10.0, 10.0]) is False


class TestPointInRing:
    def test_point_interieur(self):
        assert point_in_ring((5.0, 5.0), SQUARE) is True
        assert point_in_ring((1.0, 1.0), SQUARE) is True

    def test_point_sur_la_frontiere_compte_interieur(self):
        assert point_in_ring((0.0, 5.0), SQUARE) is True
        assert point_in_ring((5.0, 0.0), SQUARE) is True

    def test_point_exterieur(self):
        assert point_in_ring((15.0, 15.0), SQUARE) is False
        assert point_in_ring((5.0, -0.5), SQUARE) is False

    def test_anneau_sequentiel_mesoscopique(self):
        ring = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
        assert point_in_ring((1.0, 1.0), ring) is True
        assert point_in_ring((3.0, 1.0), ring) is False


class TestPointInPolygon:
    def test_trou_interieur_exclu(self):
        polygon = [SQUARE, HOLE]
        assert point_in_polygon((2.0, 2.0), polygon) is True
        assert point_in_polygon((5.0, 5.0), polygon) is False

    def test_anneau_exterieur_dominant(self):
        polygon = [SQUARE, HOLE]
        assert point_in_polygon((9.0, 5.0), polygon) is True
        assert point_in_polygon((50.0, 50.0), polygon) is False

    def test_polygone_vide(self):
        assert point_in_polygon((1.0, 1.0), []) is False


class TestGeometryContains:
    def test_polygon_simple(self):
        geometry = {"type": "Polygon", "coordinates": [SQUARE]}
        assert geometry_contains((1.0, 1.0), geometry) is True
        assert geometry_contains((50.0, 50.0), geometry) is False

    def test_multipolygon_union_des_pieces(self):
        other = [
            [[12.0, 12.0], [15.0, 12.0], [15.0, 15.0], [12.0, 15.0], [12.0, 12.0]]
        ]
        geometry = {"type": "MultiPolygon", "coordinates": [[SQUARE], other]}
        assert geometry_contains((1.0, 1.0), geometry) is True
        assert geometry_contains((13.0, 13.0), geometry) is True
        assert geometry_contains((11.0, 11.0), geometry) is False


class TestBoundingBox:
    def test_boite_du_polygone(self):
        geometry = {"type": "Polygon", "coordinates": [SQUARE]}
        assert bounding_box(geometry) == (0.0, 0.0, 10.0, 10.0)

    def test_boite_du_multipolygon(self):
        other = [[[12.0, 12.0], [15.0, 12.0], [15.0, 15.0], [12.0, 15.0], [12.0, 12.0]]]
        geometry = {"type": "MultiPolygon", "coordinates": [[SQUARE], other]}
        assert bounding_box(geometry) == (0.0, 0.0, 15.0, 15.0)