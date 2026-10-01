"""Tests du lanceur de rapports (generate_all_reports.py)."""

from datetime import date

import generate_all_reports as gar


class TestMoisPrecedent:
    def test_premier_du_mois(self):
        assert gar.previous_month(date(2026, 10, 1)) == "2026-09"

    def test_janvier_donne_decembre_de_l_annee_precedente(self):
        assert gar.previous_month(date(2027, 1, 1)) == "2026-12"


class TestPdfSeulement:
    def test_ne_garde_que_les_pdf(self, tmp_path):
        batch = tmp_path / "2026-09"
        commune = batch / "communes" / "mérignac"
        reseau = batch / "reseau" / "bordeaux-metropole"
        for folder in (commune, reseau):
            folder.mkdir(parents=True)
            for name in ("rapport.pdf", "rapport.tex", "rapport.aux", "rapport.log", "evolution.png"):
                (folder / name).write_text("x")
        (batch / "compile_all.sh").write_text("#!/bin/bash")

        kept = gar.keep_only_pdfs(batch)

        assert sorted(p.relative_to(batch).as_posix() for p in batch.rglob("*") if p.is_file()) == [
            "communes/mérignac/rapport.pdf",
            "reseau/bordeaux-metropole/rapport.pdf",
        ]
        assert len(kept) == 2

    def test_dossier_sans_pdf_conserve(self, tmp_path):
        batch = tmp_path / "2026-09"
        failed = batch / "communes" / "ambès"
        failed.mkdir(parents=True)
        (failed / "rapport.tex").write_text("x")
        gar.keep_only_pdfs(batch)
        assert (failed / "rapport.tex").exists()
