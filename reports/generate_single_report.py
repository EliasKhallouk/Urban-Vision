#!/usr/bin/env python3
"""Génère un rapport pour une commune OU pour le réseau Bordeaux Métropole."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_GENERATOR = PROJECT_ROOT / "generate_monthly_report.py"
DEFAULT_OUTPUT = PROJECT_ROOT / "output"


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-") or "rapport"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Génère un rapport mensuel pour une commune ou pour Bordeaux Métropole."
    )
    parser.add_argument("--month", required=True, help="Mois au format AAAA-MM.")
    parser.add_argument("--commune", help="Nom de la commune (ex: Bordeaux, Mérignac).")
    parser.add_argument("--network", action="store_true",
                        help="Génère le rapport réseau Bordeaux Métropole.")
    parser.add_argument("--db-path", help="Base SQLite à analyser (défaut : data/vigie_tbm.db).")
    parser.add_argument("--output-dir", help="Dossier de sortie (défaut : reports/output).")
    parser.add_argument("--compile", action="store_true",
                        help="Compile aussi en PDF.")
    args = parser.parse_args()

    if not args.commune and not args.network:
        parser.error("Précisez --commune ou --network.")
    if args.commune and args.network:
        parser.error("Choisissez --commune OU --network, pas les deux.")

    cmd = [
        sys.executable, str(REPORT_GENERATOR),
        "--month", args.month,
    ]
    if args.network:
        cmd += ["--recipient", "Bordeaux Métropole et TBM"]
    else:
        cmd += ["--recipient", f"Mairie de {args.commune}", "--communes", args.commune]
    if args.db_path:
        cmd += ["--db-path", args.db_path]

    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT / args.month
    if args.network:
        output_dir = output_dir / "reseau" / "bordeaux-metropole"
    else:
        output_dir = output_dir / "communes" / _slug(args.commune)
    cmd += ["--output-dir", str(output_dir)]

    if args.compile:
        cmd.append("--compile")

    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
