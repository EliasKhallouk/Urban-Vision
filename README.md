# Vigie-TBM

GTFS statique (horaires théoriques) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/static/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

GTFS-RT TripUpdates (retards/annulations) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

GTFS-RT VehiclePositions (position des véhicules) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/vehicles/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

#### POUR CE CONNECTER
elias@hp-info-01:~/PROJECT/Vigie-TBM$ ssh -i ~/.ssh/oracle-ek-hub.key ubuntu@88.96.51.44

#### POUR CE METTRE DANS L'ENVIRONNEMENT 
ubuntu@ek-hub-vnic:~$ cd Vigie-TBM/
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ source .venv/bin/activate

#### CRÉER LA BDD
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ python3 src/scripts/db.py

#### POUR LANCER LE SCRIPTE DE COLLECT DE DONNÉES
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo nano /etc/systemd/system/vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl enable vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl start vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-collect.service

#### POUR LANCER LE SCRIPTE DE COLLECT D'ALERTES
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo nano /etc/systemd/system/vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl enable vigie-tbm-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl start vigie-tbm-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-collect-alerts.service

#### POUR AFFICHER LE STATUS DES SERVICES
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-collect.service vigie-tbm-collect-alerts.service vigie-tbm-dashboard.service

#### POUR AFFICHER LES LOG DE LA COLLECT
ubuntu@ek-hub-vnic:~/Vigie-TBM$ tail -f data/collect.log

#### LANCER LE DASHBOARD
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl enable vigie-tbm-dashboard.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl start vigie-tbm-dashboard.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-dashboard.service 

//avant :(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ .venv/bin/streamlit run dashboard/app.py


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
