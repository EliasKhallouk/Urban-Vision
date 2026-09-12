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