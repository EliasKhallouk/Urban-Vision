"""Tests de la carte des points de contention : règle des paliers, passages de tronçon, agrégats et carte."""

import json
from datetime import datetime

import pandas as pd

import carte_troncons as ct
import gtfs_static
import troncons as tr
from palette import MEDIUM, NEGATIVE, POSITIVE, troncon_tier


def _ts(day: str, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime.strptime(f"{day} {hour}:{minute}:{second}", "%Y-%m-%d %H:%M:%S").timestamp())


class TestPalier:
    def test_secondes_et_part_du_temps_prevu(self):
        assert troncon_tier(45, 60) == NEGATIVE
        assert troncon_tier(45, 30) == MEDIUM
        assert troncon_tier(20, 30) == MEDIUM

    def test_troncon_long_ou_tres_court_pas_colore_a_tort(self):
        assert troncon_tier(100, 10) == POSITIVE
        assert troncon_tier(14, 90) == POSITIVE
        assert troncon_tier(-30, -40) == POSITIVE


def _rows(delays, start="2026-10-07", hour=8, trip="t1", spacing=90, route="R"):
    """Observations d'un trajet : un arrêt toutes les `spacing` secondes prévues, avec ces retards."""
    rows = []
    for i, delay in enumerate(delays):
        scheduled = _ts(start, hour) + i * spacing
        rows.append({"trip_id": trip, "start_date": start.replace("-", ""), "route_id": route, "seq": i + 1,
                     "stop_id": f"s{i + 1}", "rel": "SCHEDULED", "departure_time": scheduled + delay, "dep": delay})
    return rows


class TestPassages:
    def test_temps_perdu_temps_prevu_et_drapeaux(self):
        p = tr.passages_frame(pd.DataFrame(_rows([10, 20, 80, 85, 90])))
        assert list(zip(p["prev_stop_id"], p["stop_id"])) == [("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s5")]
        assert p["gain"].tolist() == [10, 60, 5, 5]
        assert p["sched"].tolist() == [90] * 4
        assert p["pct"].tolist() == [11, 67, 6, 6]
        assert p["terminus"].tolist() == [True, False, False, True]
        assert p["kept"].tolist() == [False, True, True, False]
        assert set(p["heure"]) == {8} and set(p["date_service"]) == {"2026-10-07"}

    def test_retard_nul_arret_saute_et_valeur_aberrante(self):
        rows = _rows([10, 0, 30, 1500, 40, 50])
        rows.insert(3, {**rows[2], "seq": 3.5, "rel": "SKIPPED", "dep": None, "departure_time": None})
        p = tr.passages_frame(pd.DataFrame(rows)).set_index("stop_id")
        assert bool(p.loc["s2", "zero"]) and bool(p.loc["s3", "zero"])
        assert bool(p.loc["s4", "outlier"]) and bool(p.loc["s5", "outlier"])
        assert not p["kept"].any()

    def test_arrets_non_consecutifs_ecartes(self):
        rows = _rows([10, 20, 30, 40, 50])
        del rows[2]
        p = tr.passages_frame(pd.DataFrame(rows)).set_index("stop_id")
        assert not bool(p.loc["s4", "consecutive"])
        assert not bool(p.loc["s4", "kept"])

    def test_lignes_du_jour(self):
        p = tr.passages_frame(pd.DataFrame(_rows([10, 20, 80, 85, 90]) + _rows([10, 30, 100, 105, 110], trip="t2")))
        out = tr.day_rows(p, "2026-10-07").set_index(["from_stop_id", "to_stop_id"])
        assert out.loc[("s2", "s3"), "n"] == 2
        assert sorted(out.loc[("s2", "s3"), "gains"].split()) == ["60", "70"]
        assert out.loc[("s1", "s2"), "n_terminus"] == 2 and out.loc[("s1", "s2"), "gains"] == ""
        assert tr.day_rows(p, "2026-10-08").empty


def _seed(conn, days=("2026-10-05", "2026-10-06", "2026-10-07"), trips_per_day=8, route="R", zero=False):
    gtfs_static.create_static_tables(conn)
    conn.execute("INSERT OR IGNORE INTO routes VALUES (?, ?, ?, 3)", (route, route, f"Ligne {route}"))
    conn.executemany("INSERT OR IGNORE INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES (?, ?, ?, ?)",
                     [(f"s{i}", f"Arrêt {i}", 44.84, -0.60 + i * 0.004) for i in range(1, 6)])
    rows = []
    for day in days:
        for k in range(trips_per_day):
            delays = [0, 0, 0, 0, 0] if zero else [10, 20, 80, 85, 90]
            for r in _rows(delays, start=day, trip=f"{route}{day}{k}", route=route):
                r["departure_time"] += k * 300
                rows.append((r["trip_id"], r["start_date"], route, 0, r["seq"], r["stop_id"], "SCHEDULED",
                             r["dep"], r["departure_time"], r["departure_time"] + 60))
    conn.executemany(
        "INSERT INTO observations (trip_id, start_date, route_id, direction_id, stop_sequence, stop_id, "
        "schedule_relationship, departure_delay, departure_time, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows)
    conn.commit()
    import db as dbio
    dbio.refresh_aggregates(conn, days=list(days))


class TestCarte:
    def test_jours_a_calculer_puis_carte(self, conn):
        _seed(conn)
        assert tr.missing_days(conn, "2026-10-01", "2026-10-08") == ["2026-10-05", "2026-10-06", "2026-10-07"]
        meta = tr.run(conn, "2026-10-01", "2026-10-08", pause=0)
        assert tr.missing_days(conn, "2026-10-01", "2026-10-08") == []
        assert meta["lignes"]["R"]["ok"] is True
        cells = pd.read_sql_query("SELECT * FROM troncons_carte WHERE jour_type = 'semaine'", conn)
        cell = cells[(cells.heure == 8) & (cells.from_stop_id == "s2")].iloc[0]
        assert (cell.statut, cell.palier, cell.mediane_s, cell.mediane_pct, cell.passages, cell.jours) == (
            "mesure", NEGATIVE, 60.0, 67.0, 24, 3)
        assert cells[(cells.heure == -1) & (cells.from_stop_id == "s3")].iloc[0].palier == POSITIVE
        assert set(cells[cells.from_stop_id == "s1"].statut) == {"terminus"}
        assert conn.execute("SELECT passages FROM troncons_lignes WHERE from_stop_id = 's2'").fetchone() == (24,)
        geo = conn.execute("SELECT route_type, coords FROM troncons_geo WHERE from_stop_id = 's2'").fetchone()
        assert geo[0] == 3 and json.loads(geo[1]) == [[-0.592, 44.84], [-0.588, 44.84]]

    def test_ligne_sans_temps_reel_en_gris(self, conn):
        _seed(conn, route="Z", zero=True)
        meta = tr.run(conn, "2026-10-01", "2026-10-08", pause=0)
        assert meta["lignes"]["Z"]["ok"] is False
        assert set(pd.read_sql_query("SELECT statut FROM troncons_carte", conn)["statut"]) <= {"douteux", "terminus"}

    def test_pas_assez_de_donnees(self, conn):
        _seed(conn, days=("2026-10-05", "2026-10-06"))
        tr.run(conn, "2026-10-01", "2026-10-08", pause=0)
        cell = conn.execute("SELECT statut, palier FROM troncons_carte WHERE from_stop_id = 's2' AND heure = 8").fetchone()
        assert cell == ("peu", None)


class TestGeometrie:
    LINE = [[-0.60, 44.84], [-0.59, 44.84], [-0.58, 44.84]]

    def test_portion_du_trace(self):
        part = tr.segment_geometry([self.LINE], (-0.595, 44.8401), (-0.585, 44.8401))
        assert part[0] == [-0.595, 44.84] and part[-1] == [-0.585, 44.84] and [-0.59, 44.84] in part

    def test_droite_si_loin_du_trace_ou_a_contresens(self):
        assert tr.segment_geometry([self.LINE], (-0.595, 44.85), (-0.585, 44.85)) == [[-0.595, 44.85], [-0.585, 44.85]]
        assert tr.segment_geometry([self.LINE], (-0.585, 44.84), (-0.595, 44.84)) == [[-0.585, 44.84], [-0.595, 44.84]]


class TestComposant:
    def test_decalage_a_droite_du_sens(self):
        east = ct.offset_right([[-0.60, 44.84], [-0.59, 44.84]], 10)
        assert east[0][1] < 44.84 and east[0][0] == -0.60
        west = ct.offset_right([[-0.59, 44.84], [-0.60, 44.84]], 10)
        assert west[0][1] > 44.84

    def test_codes_et_heure_la_plus_difficile(self):
        assert ct.cell_code("mesure", NEGATIVE) == "c"
        assert [ct.cell_code(s, None) for s in ("peu", "terminus", "douteux", "?")] == ["p", "t", "d", "-"]
        codes = ["-" * 8 + "c" + "-" * 16, "-" * 8 + "o" + "-" * 8 + "c" + "-" * 7, "-" * 25]
        assert ct.worst_slot(codes) == 8
        assert ct.worst_slot(["v" * 25]) is None

    def test_donnees_du_composant(self):
        geo = pd.DataFrame({"from_stop_id": ["a", "c"], "to_stop_id": ["b", "d"], "route_type": [3, 4],
                            "coords": [[[-0.60, 44.84], [-0.59, 44.84]], [[-0.50, 44.90], [-0.49, 44.90]]]})
        cells = pd.DataFrame({"heure": [8, -1, 8], "from_stop_id": ["a", "a", "c"], "to_stop_id": ["b", "b", "d"],
                              "statut": ["mesure", "mesure", "peu"], "palier": [NEGATIVE, MEDIUM, None],
                              "mediane_s": [60.0, 20.0, None], "mediane_pct": [67.0, 30.0, None],
                              "passages": [24, 300, 3]})
        payload = ct.troncons_payload(geo, cells, {("a", "b"): "1, 16"}, {"a": "Gare", "b": "Parc"},
                                      {"a": "Mérignac"}, [], "Mérignac", 8)
        first, second = payload["segments"]
        assert first["t"][8] == "c" and first["t"][ct.WHOLE_DAY_SLOT] == "o" and len(first["t"]) == ct.SLOTS
        assert first["v"] == {8: [60, 67, 24], ct.WHOLE_DAY_SLOT: [20, 30, 300]}
        assert (first["n"], first["l"], first["c"], first["m"]) == ("Gare vers Parc", "1, 16", 1, 3)
        assert second["t"][8] == "p" and second["c"] == 0 and second["m"] == 4
        assert payload["bounds"] == [[-0.60, 44.84], [-0.59, 44.84]]
        assert payload["view_key"] == "Mérignac"


def test_le_service_nocturne_est_surveille():
    import veille_collecte as vc
    assert "urban-vision-troncons.service" in vc.WATCHED_UNITS
