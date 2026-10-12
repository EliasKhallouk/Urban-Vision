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


class TestDate:
    def test_creneaux_de_deux_heures_et_seuil(self, conn):
        _seed(conn)
        tr.run(conn, "2026-10-01", "2026-10-08", pause=0)
        cells = tr.date_cells(conn, "2026-10-07", {"R"})
        cell = cells[(cells.heure == 8) & (cells.from_stop_id == "s2")].iloc[0]
        assert (cell.jour_type, cell.statut, cell.palier, cell.passages, cell.jours) == (
            "2026-10-07", "mesure", NEGATIVE, 8, 1)
        assert set(cells["heure"]) == {8, tr.WHOLE_DAY}

    def test_moins_de_huit_passages(self, conn):
        _seed(conn, days=("2026-10-07",), trips_per_day=7)
        tr.run(conn, "2026-10-01", "2026-10-08", pause=0)
        cells = tr.date_cells(conn, "2026-10-07", {"R"})
        assert set(cells[cells.from_stop_id == "s2"]["statut"]) == {"peu"}

    def test_textes_et_dates(self):
        import app as app_mod
        passages = {"2026-10-05": 170000, "2026-10-06": 174000, "2026-10-07": 180000, "2026-10-08": 90000,
                    "2026-10-04": 80000}
        assert app_mod.contention_dates(passages, "2026-10-05", "2026-10-08", ["2026-10-06"]) == [
            "2026-10-05", "2026-10-07"]
        assert app_mod.date_coverage_reason("2026-10-04", passages, []) == "C'est un dimanche."
        assert app_mod.date_coverage_reason("2026-10-07", passages, []) is None
        assert "52 % des passages" in app_mod.date_coverage_reason("2026-10-08", passages, [])
        assert app_mod.date_coverage_reason("2026-11-11", passages, []) == "C'est un jour férié (armistice)."
        assert app_mod.date_slot_hours(None) == 2 and app_mod.date_slot_hours("C'est un dimanche.") == 4
        assert app_mod.date_label("2026-10-07") == "mercredi 07/10/2026"

    def test_creneaux_de_quatre_heures(self, conn):
        _seed(conn)
        tr.run(conn, "2026-10-01", "2026-10-08", pause=0)
        cells = tr.date_cells(conn, "2026-10-07", {"R"}, slot_hours=4)
        assert set(cells["heure"]) == {8, tr.WHOLE_DAY}
        assert cells[(cells.heure == 8) & (cells.from_stop_id == "s2")].iloc[0].passages == 8

    def test_jours_feries(self):
        import indicateurs as ind
        assert str(ind.easter(2026)) == "2026-04-05" and str(ind.easter(2027)) == "2027-03-28"
        assert ind.public_holiday("2026-11-11") == "armistice"
        assert ind.public_holiday("2026-04-06") == "lundi de Pâques"
        assert ind.public_holiday("2026-05-14") == "Ascension"
        assert ind.public_holiday("2026-05-25") == "lundi de Pentecôte"
        assert ind.public_holiday("2026-10-07") is None

    def test_donnees_d_une_date(self):
        geo = pd.DataFrame({"from_stop_id": ["a", "c"], "to_stop_id": ["b", "d"], "route_type": [3, 3],
                            "coords": [[[-0.60, 44.84], [-0.59, 44.84]], [[-0.58, 44.84], [-0.57, 44.84]]]})
        cells = pd.DataFrame({"heure": [8], "from_stop_id": ["a"], "to_stop_id": ["b"], "statut": ["mesure"],
                              "palier": [NEGATIVE], "mediane_s": [60.0], "mediane_pct": [70.0], "passages": [9],
                              "part_2min": [0.0]})
        info = {"day": "2026-10-07", "label": "mercredi 07/10/2026", "reason": None}
        payload = ct.troncons_payload(geo, cells, {}, {}, {}, [], {}, {}, None, {}, step=2, date_info=info)
        assert [s["t"][8] for s in payload["segments"]] == ["c", "-"]
        assert (payload["step"], payload["mode"], payload["date"]["label"]) == (2, "2026-10-07", "mercredi 07/10/2026")
        typed = ct.troncons_payload(geo, cells, {}, {}, {}, [], {}, {}, None, {})
        assert len(typed["segments"]) == 1 and typed["step"] == 1 and typed["mode"] == "type"


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

    def _geo_cells(self):
        geo = pd.DataFrame({"from_stop_id": ["a", "c"], "to_stop_id": ["b", "d"], "route_type": [3, 4],
                            "coords": [[[-0.60, 44.84], [-0.59, 44.84]], [[-0.50, 44.90], [-0.49, 44.90]]]})
        cells = pd.DataFrame({"heure": [8, -1, 8, 17], "from_stop_id": ["a", "a", "c", "a"],
                              "to_stop_id": ["b", "b", "d", "b"], "statut": ["mesure", "mesure", "peu", "mesure"],
                              "palier": [NEGATIVE, MEDIUM, None, POSITIVE], "mediane_s": [60.0, 20.0, None, 3.0],
                              "mediane_pct": [67.0, 30.0, None, 4.0], "passages": [24, 300, 3, 30],
                              "part_2min": [12.4, 5.0, None, 0.0]})
        return geo, cells

    def test_donnees_du_composant(self):
        geo, cells = self._geo_cells()
        scope = {"commune": "Mérignac", "place": "à Mérignac", "label": "Mérignac"}
        payload = ct.troncons_payload(geo, cells, {("a", "b"): "1, 16"}, {"a": "Gare", "b": "Parc"},
                                      {"a": "Mérignac"}, [], scope, {("a", "b"): ["01", "16"]})
        first, second = payload["segments"]
        assert first["t"][8] == "c" and first["t"][ct.WHOLE_DAY_SLOT] == "o" and len(first["t"]) == ct.SLOTS
        assert first["v"][8] == [60, 67, 24, 12] and first["v"][ct.WHOLE_DAY_SLOT] == [20, 30, 300, 5]
        assert (first["n"], first["l"], first["c"], first["m"]) == ("Gare vers Parc", "1, 16", 1, 3)
        assert second["t"][8] == "p" and second["v"][8] == [None, None, 3, None]
        assert second["c"] == 0 and second["m"] == 4
        assert payload["bounds"] == [[-0.60, 44.84], [-0.60, 44.84]]
        assert payload["slot"] == 8 and payload["place"] == "à Mérignac" and payload["label"] == "Mérignac"

    def test_perimetre_ligne_et_arret(self):
        communes = {"a": "Mérignac", "c": "Pessac"}
        assert ct.in_scope("a", "b", ["01"], communes, {"route_id": "01"})
        assert not ct.in_scope("c", "d", ["02"], communes, {"route_id": "01"})
        assert ct.in_scope("c", "d", [], communes, {"stop_id": "d"})
        assert not ct.in_scope("a", "b", ["01"], communes, {"commune": "Pessac", "route_id": "01"})
        geo, cells = self._geo_cells()
        payload = ct.troncons_payload(geo, cells, {}, {}, communes, [], {"stop_id": "c"}, {})
        assert [s["c"] for s in payload["segments"]] == [0, 1]
        assert payload["slot"] == ct.WHOLE_DAY_SLOT
        assert payload["view_key"] == "||c"


class TestTram:
    def _first_last(self):
        first = pd.DataFrame({
            "trip_id": ["ok", "sans_tr", "partiel", "aberrant"], "start_date": ["20261007"] * 4,
            "route_id": ["T"] * 4, "direction_id": [0] * 4, "seq0": [1, 1, 1, 1],
            "t0": [_ts("2026-10-07", 8)] * 4, "dep0": [30, 0, 10, 10], "ds": ["2026-10-07"] * 4,
            "heure": [8] * 4, "n": [10, 10, 3, 10], "nonzero": [9, 0, 3, 10]})
        last = pd.DataFrame({
            "trip_id": ["ok", "sans_tr", "partiel", "aberrant"], "start_date": ["20261007"] * 4,
            "seq1": [10, 10, 10, 10], "terminus_id": ["s9"] * 4,
            "t1": [_ts("2026-10-07", 8, 40, 20)] * 4, "dep1": [140, 0, 50, 4000], "arr1": [150, 0, None, 4000]})
        return first, last

    def test_temps_perdu_de_bout_en_bout(self):
        trips = tr.tram_trips(*self._first_last())
        assert list(trips["trip_id"]) == ["ok"]
        assert trips.iloc[0]["perte"] == 120
        assert trips.iloc[0]["prevu"] == 40 * 60 + 20 - 140 + 30

    def test_terminus_principaux(self):
        rows = []
        for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
            rows += [("Nord", 0, 100 + k, 2400, day, 8) for k in range(8)]
            rows += [("Dépôt", 0, 10, 600, day, 8) for _ in range(8)]
            rows += [("Sud", 1, 60, 2300, day, 17 if k % 2 else 18) for k in range(8)]
        trips = pd.DataFrame(rows, columns=["terminus", "direction_id", "perte", "prevu", "ds", "heure"]).assign(
            route_id="T")
        out = tr.summarize_tram(trips)
        assert set(out["terminus"]) == {"Nord", "Sud"}
        nord = out[(out.terminus == "Nord") & (out.heure == tr.WHOLE_DAY)].iloc[0]
        assert (nord.perte_s, nord.prevu_s, nord.trajets, nord.jours) == (103.5, 2400, 24, 3)
        assert set(out[out.terminus == "Sud"]["heure"]) == {tr.WHOLE_DAY}
        assert set(out[out.terminus == "Nord"]["heure"]) == {tr.WHOLE_DAY, 8}

    def test_table_du_tram(self, conn):
        gtfs_static.create_static_tables(conn)
        conn.execute("INSERT INTO routes VALUES ('T', 'A', 'Tram A', 0)")
        conn.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES ('t3', 'Terminus', 44.8, -0.6)")
        rows = []
        for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
            for k in range(8):
                start = _ts(day, 8) + k * 300
                for seq, (stop, delay) in enumerate([("t1", 20), ("t2", 60), ("t3", 140)], start=1):
                    rows.append((f"{day}{k}", day.replace("-", ""), "T", 0, seq, stop, "SCHEDULED", delay, delay,
                                 start + seq * 600 + delay, start + seq * 600 + delay + 60))
        conn.executemany(
            "INSERT INTO observations (trip_id, start_date, route_id, direction_id, stop_sequence, stop_id, "
            "schedule_relationship, arrival_delay, departure_delay, departure_time, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        assert tr.build_tram(conn, "2026-10-01", "2026-10-08") == 2
        assert conn.execute(
            "SELECT terminus, perte_s, prevu_s, trajets, jours FROM troncons_tram WHERE heure = -1"
        ).fetchone() == ("Terminus", 120.0, 1200.0, 24, 3)

    def test_donnees_du_composant(self):
        rows = pd.DataFrame({"heure": [8, -1, -1], "route_id": ["T"] * 3, "direction_id": [0, 0, 1],
                             "terminus": ["Nord", "Nord", "Sud"], "perte_s": [130.4, 100.0, 60.0],
                             "prevu_s": [2400.0, 2400.0, 2300.0], "trajets": [40, 300, 280]})
        lines = ct.tram_lines_payload(rows, {"T": "A"})
        assert lines["T"]["n"] == "A" and [d["vers"] for d in lines["T"]["s"]] == ["Nord", "Sud"]
        assert lines["T"]["s"][0]["v"][8] == [130, 2400, 40] and lines["T"]["s"][1]["v"][8] is None
        paths = [{"r": "T", "p": [[-0.6, 44.8], [-0.5, 44.9]]}, {"r": "U", "p": [[-0.7, 44.7], [-0.6, 44.7]]}]
        empty = pd.DataFrame(columns=["from_stop_id", "to_stop_id", "route_type", "coords"])
        no_cells = pd.DataFrame(columns=["heure", "from_stop_id", "to_stop_id", "statut", "palier", "mediane_s",
                                         "mediane_pct", "passages"])
        payload = ct.troncons_payload(empty, no_cells, {}, {}, {}, paths, {"route_id": "T"}, {}, None, lines)
        assert payload["tram_line"] == "T" and payload["slot"] == ct.WHOLE_DAY_SLOT
        assert [t["c"] for t in payload["tram"]] == [1, 0]
        assert payload["bounds"] == [[-0.6, 44.8], [-0.5, 44.9]]
        network = ct.troncons_payload(empty, no_cells, {}, {}, {}, paths, {}, {}, None, lines)
        assert network["tram_line"] is None and [t["c"] for t in network["tram"]] == [1, 1]


def test_textes_du_perimetre():
    import app as app_mod
    assert app_mod.contention_scope(None, None, None, None, None) == {
        "commune": None, "route_id": None, "stop_id": None, "place": "sur le réseau", "label": ""}
    s = app_mod.contention_scope("Mérignac", "23", "23", None, None)
    assert (s["place"], s["label"]) == ("sur la ligne 23 à Mérignac", "Ligne 23")
    s = app_mod.contention_scope(None, "23", "23", "s9", "Gare")
    assert (s["place"], s["label"]) == ("autour de l'arrêt Gare", "Gare")


def test_message_carte_pas_encore_calculee():
    import app as app_mod
    assert app_mod.contention_empty_message(True) == "La carte n'est pas encore calculée : elle l'est chaque nuit à 3 h 40."
    local = app_mod.contention_empty_message(False)
    assert local.startswith("La carte n'est pas encore calculée") and ".venv/bin/python src/scripts/troncons.py" in local


def test_le_service_nocturne_est_surveille():
    import veille_collecte as vc
    assert "urban-vision-troncons.service" in vc.WATCHED_UNITS
