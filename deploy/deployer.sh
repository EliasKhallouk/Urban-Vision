#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
PY="$REPO/.venv/bin/python"
UV="${UV:-$HOME/.local/bin/uv}"
SERVICES=(urban-vision-collect urban-vision-collect-alerts urban-vision-dashboard)
PULL=1
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --sans-pull) PULL=0 ;;
    --sans-redemarrage) RESTART=0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

etape() { printf '\n== %s\n' "$*"; }
echec() { printf 'ÉCHEC : %s\n' "$*" >&2; exit 1; }

etape "Code"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echec "des fichiers suivis sont modifiés sur la VM ; déploiement annulé"
fi
if [ "$PULL" = 1 ]; then git pull --ff-only; fi
echo "Version : $(git log --oneline -1)"

etape "Dépendances"
if [ -x "$UV" ]; then
  "$UV" pip install --quiet --python "$PY" -r requirements.txt
else
  "$PY" -m pip install --quiet -r requirements.txt
fi

etape "Unités systemd"
for f in deploy/systemd/*; do
  cible="/etc/systemd/system/$(basename "$f")"
  if ! sudo cmp -s "$f" "$cible"; then
    sudo install -m 644 "$f" "$cible"
    echo "  installée : $(basename "$f")"
  fi
done
sudo systemctl daemon-reload
for t in deploy/systemd/*.timer; do
  sudo systemctl enable --now "$(basename "$t")" >/dev/null 2>&1
done

etape "Rotation des journaux"
sudo install -m 644 deploy/logrotate/urban-vision /etc/logrotate.d/urban-vision
sudo logrotate --debug /etc/logrotate.d/urban-vision >/dev/null 2>&1 || echec "configuration logrotate invalide"

etape "nginx"
if ! sudo cmp -s deploy/nginx/urban-vision /etc/nginx/sites-available/urban-vision; then
  sudo cp /etc/nginx/sites-available/urban-vision "/etc/nginx/sites-available/urban-vision.avant-$(date +%Y%m%d%H%M%S)"
  sudo install -m 644 deploy/nginx/urban-vision /etc/nginx/sites-available/urban-vision
  if sudo nginx -t 2>/dev/null; then
    sudo systemctl reload nginx
    echo "  configuration rechargée"
  else
    sudo cp "$(ls -t /etc/nginx/sites-available/urban-vision.avant-* | head -1)" /etc/nginx/sites-available/urban-vision
    echec "nginx -t refuse la nouvelle configuration ; ancienne configuration remise"
  fi
fi

etape "Tâche cron root"
if ! diff -q <(sudo crontab -l 2>/dev/null) deploy/cron/root.crontab >/dev/null; then
  echo "  attention : la crontab root diffère de deploy/cron/root.crontab (non modifiée automatiquement)"
fi

if [ "$RESTART" = 1 ]; then
  etape "Redémarrage des services"
  sudo systemctl restart "${SERVICES[@]}"
  echo "  attente d'un relevé (75 s)…"
  sleep 75
fi

etape "Contrôles"
for s in "${SERVICES[@]}"; do
  etat="$(systemctl is-active "$s" || true)"
  echo "  $s : $etat"
  [ "$etat" = active ] || echec "$s n'est pas actif"
done
"$PY" - <<'EOF'
import sqlite3, time
c = sqlite3.connect("file:data/urban_vision.db?mode=ro", uri=True)
row = c.execute("SELECT started_at, entities, error FROM collection_runs ORDER BY started_at DESC LIMIT 1").fetchone()
if row is None or row[2] or time.time() - row[0] > 150:
    raise SystemExit(f"ÉCHEC : pas de relevé récent réussi dans collection_runs ({row})")
print(f"  dernier relevé il y a {time.time() - row[0]:.0f} s, {row[1]} entités")
EOF
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://urban-vision.duckdns.org/ || true)"
echo "  dashboard HTTPS : $code"
[ "$code" = 200 ] || echec "le dashboard ne répond pas 200"
systemctl list-timers 'urban-vision-*' --no-pager | head -n 10 || true
"$PY" src/scripts/veille_collecte.py --dry-run | sed 's/^/  /'

etape "Déploiement terminé"
