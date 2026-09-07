#!/usr/bin/env python3
"""Génère en une commande le rapport réseau Bordeaux Métropole + un rapport par commune.

Chaque rapport est généré dans son propre dossier (pas de collision des PNG).
Avec `--compile`, un script compile_all.sh est écrit pour compiler tous les .tex
en PDF avec xelatex (à lancer sur une machine où xelatex est disponible).
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "vigie_tbm.db"
REPORT_GENERATOR = Path(__file__).resolve().parent / "generate_monthly_report.py"


def slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-") or "rapport"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Génère le rapport réseau + un rapport par commune, chacun dans son dossier."
    )
    parser.add_argument("--month", required=True, help="Mois analysé au format AAAA-MM.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB, help="Base SQLite à analyser.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "output",
                        help="Répertoire racine (par défaut reports/output).")
    parser.add_argument("--compile", action="store_true",
                        help="Génère compile_all.sh pour compiler tous les rapports en PDF (xelatex).")
    parser.add_argument("--communes", nargs="+",
                        help="Sous-ensemble facultatif de communes (utile pour tester).")
    args = parser.parse_args()
    if not args.db_path.exists():
        parser.error(f"Base introuvable : {args.db_path}")

    with sqlite3.connect(args.db_path) as conn:
        has_mapping = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='stop_municipalities')"
        ).fetchone()[0]
        if not has_mapping:
            parser.error(
                "Rattachement communal absent. Lancez d'abord src/scripts/assign_stop_municipalities.py."
            )
        communes = [
            row[0] for row in conn.execute(
                "SELECT commune_name FROM stop_municipalities "
                "GROUP BY commune_name ORDER BY commune_name"
            )
        ]

    if args.communes:
        requested = {name.strip().casefold() for name in args.communes}
        communes = [name for name in communes if name.casefold() in requested]
        unknown = requested - {name.casefold() for name in communes}
        if unknown:
            parser.error(f"Commune(s) inconnue(s) : {', '.join(sorted(unknown))}")

    batch_root = args.output_dir / args.month

    # (dossier de destination, libellé, options supplémentaires du moteur)
    targets: list[tuple[Path, str, list[str]]] = [
        (
            batch_root / "reseau" / "bordeaux-metropole",
            "Réseau Bordeaux Métropole",
            ["--recipient", "Bordeaux Métropole et TBM"],
        ),
    ]
    targets += [
        (
            batch_root / "communes" / slug(commune),
            commune,
            ["--recipient", f"Mairie de {commune}", "--communes", commune],
        )
        for commune in communes
    ]

    failures = []
    total = len(targets)
    for index, (destination, label, extra) in enumerate(targets, start=1):
        print(f"[{index}/{total}] {label}")
        command = [
            sys.executable, str(REPORT_GENERATOR), "--month", args.month,
            "--db-path", str(args.db_path), "--output-dir", str(destination),
        ] + extra
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            # Nouvelle tentative : un premier échec peut être transitoire
            # (verrou SQLite tenu par le collecteur, formatage de la table...).
            result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            failures.append((label, result.stderr.strip() or result.stdout.strip()))
            print(f"  ÉCHEC : {failures[-1][1]}", file=sys.stderr)

    if args.compile:
        compile_script = batch_root / "compile_all.sh"
        compile_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -u\n"
            'cd "$(dirname "$0")"\n'
            "failures=()\n"
            'for tex in $(find . -name "*.tex" | sort); do\n'
            '  dir=$(dirname "$tex")\n'
            '  name=$(basename "$tex" .tex)\n'
            '  echo "Compilation : $name"\n'
            '  if ! (cd "$dir" && xelatex -interaction=nonstopmode -halt-on-error "$name" \\\n'
            '        && xelatex -interaction=nonstopmode -halt-on-error "$name"); then\n'
            '    failures+=("$name")\n'
            "  fi\n"
            "done\n"
            'if [ ${#failures[@]} -gt 0 ]; then\n'
            '  printf "Échec de compilation (%d) :\\n" "${#failures[@]}"\n'
            '  printf "  %s\\n" "${failures[@]}"\n'
            "  exit 1\n"
            "fi\n"
            'echo "Tous les rapports ont été compilés."\n'
        )
        compile_script.chmod(0o755)
        print(f"[+] Script de compilation : {compile_script}")
        print("[+] Compilation des PDF (xelatex)...")
        compile_result = subprocess.run(["bash", str(compile_script)], text=True)
        if compile_result.returncode:
            print("Échec de la compilation (voir la liste ci-dessus).", file=sys.stderr)
            print(f"Relancez-la manuellement : bash {compile_script}", file=sys.stderr)
            return 1
        print("[+] PDF générés.")

    print(f"\n{total} rapports générés dans {batch_root}")
    if failures:
        print(f"{len(failures)} échec(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())