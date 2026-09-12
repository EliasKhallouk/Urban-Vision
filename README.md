# Urban Vision

Observatoire indépendant de la fiabilité du réseau de transport TBM (Bordeaux Métropole).

GTFS statique (horaires théoriques) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/static/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

GTFS-RT TripUpdates (retards/annulations) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

GTFS-RT VehiclePositions (position des véhicules) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/vehicles/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

#### POUR CE CONNECTER
elias@hp-info-01:~/PROJECT/Urban-Vision$ ssh -i ~/.ssh/oracle-ek-hub.key ubuntu@88.96.51.44

#### POUR CE METTRE DANS L'ENVIRONNEMENT
ubuntu@ek-hub-vnic:~$ cd Urban-Vision/
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ source .venv/bin/activate

#### CRÉER LA BDD
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ python3 src/scripts/db.py

#### POUR LANCER LE SCRIPTE DE COLLECT DE DONNÉES
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo nano /etc/systemd/system/urban-vision-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl enable urban-vision-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl start urban-vision-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl status urban-vision-collect.service

#### POUR LANCER LE SCRIPTE DE COLLECT D'ALERTES
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo nano /etc/systemd/system/urban-vision-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl enable urban-vision-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl start urban-vision-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl status urban-vision-collect-alerts.service

#### POUR AFFICHER LE STATUS DES SERVICES
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl status urban-vision-collect.service urban-vision-collect-alerts.service urban-vision-dashboard.service

#### POUR AFFICHER LES LOG DE LA COLLECT
ubuntu@ek-hub-vnic:~/Urban-Vision$ tail -f data/collect.log

#### LANCER LE DASHBOARD
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl enable urban-vision-dashboard.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl start urban-vision-dashboard.service
(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ sudo systemctl status urban-vision-dashboard.service

//avant :(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ .venv/bin/streamlit run dashboard/app.py

#### GÉNERER LES RAPPORTS
Trois scripts dans `reports/` :
- `generate_single_report.py` : un rapport à la fois (réseau entier **ou** une commune).
- `generate_all_reports.py` : tout en une fois (réseau + toutes les communes), un dossier par rapport, avec script de compilation.
- `generate_monthly_report.py` : le moteur interne (pas besoin de l'appeler directement).

##### Rapport unique — réseau
.venv/bin/python reports/generate_single_report.py --month 2026-08 --network --compile

##### Rapport unique — commune
.venv/bin/python reports/generate_single_report.py --month 2026-08 --commune "Mérignac" --compile

##### Tous les rapports (réseau + toutes les communes), avec compilation :
.venv/bin/python reports/generate_all_reports.py --month 2026-08 --compile
# (--compile génère les PDF automatiquement ; compile_all.sh reste dispo pour relancer)

#### POUR LANCER LES TESTS
Les tests vivent dans `tests/` et s'exécutent avec pytest sur des bases SQLite
temporaires (aucune donnée réelle n'est touchée, pas de réseau).

(.venv) ubuntu@ek-hub-vnic:~/Urban-Vision$ .venv/bin/python -m pytest
# sur la machine de dev : .venv/bin/python -m pytest -q  # 167 tests

#### SÉCURITÉ & EXPLOITATION PROD (durcissement appliqué le 12/09/2026)

> Les fichiers ci-dessous vivent sur la VM (`/etc/...`) et ne sont **pas** dans git. Ce README est leur trace.

- **Accès SSH** : clé uniquement, mot de passe système désactivé. Brute-force bannie 1 h par `fail2ban` (5 échecs / 10 min).
- **Dashboard** : accessible **uniquement en HTTPS** via nginx (`https://urban-vision.duckdns.org`), qui reverse vers `127.0.0.1:8501`. Le port `8501` est fermé dans `/etc/iptables/rules.v4` — ne jamais le rouvrir.
- **Firewall** : iptables persistés (`netfilter-persistent`) ; seuls `22/80/443` sont ouverts. `rpcbind` (port 111) désactivé.
- **Sudo `ubuntu`** : **`NOPASSWD:ALL`** (comportement OCI d'origine, restauré le 12/09/2026). Le compte n'a **pas de mot de passe** (verrouillé, jamais créé par l'image OCI) — le sudo ne demandera donc jamais de mot de passe. C'est le réglage de confort choisi ; si tu veux resserrer un jour, la règle vit dans `/etc/sudoers.d/90-cloud-init-users` et `/etc/sudoers`.
- **Root / dépannage** : en cas de blocage réseau ou système, la **console OCI** de l'instance (portail → Compute → Instances → `ek-hub` → **Console connection**) donne un accès série hors-bande.
- **Services systemd** : les 3 unités (`/etc/systemd/system/urban-vision-*.service`) tournent en user `ubuntu` avec `NoNewPrivileges`, `ProtectSystem=full`, `PrivateTmp`, `RestrictAddressFamilies`.
- **Secrets** : `.env` gitignoré (permissions 600), absent de prod. Token DuckDNS inutilisé par le code → supprimé de la machine de dev, à régénérer sur duckdns.org si besoin.