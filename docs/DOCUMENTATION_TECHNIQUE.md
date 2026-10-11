# Urban Vision — Documentation technique

| | |
|---|---|
| **Projet** | Urban Vision |
| **Version du document** | 1.1 |
| **Date** | 2026-10-01 |
| **Commit de référence** | `3afe0d3` (branche `main`) + branche `refonte-ux-dashboard` |
| **Auteur d'origine** | Elias Khallouk (eliaskhallouk@gmail.com) |
| **Licence / dépôt** | https://github.com/EliasKhallouk/Urban-Vision |
| **Périmètre** | Dépôt local **ET** environnement de production (VM Oracle Cloud) |

Ce document décrit l'état **réel** du projet au 01/10/2026. Chaque information
provient du code, de la configuration ou de l'environnement observés ; rien n'a
été inventé. Les points restés incertains sont signalés **« à confirmer »** et
consolidés dans la section 26.

> Organisation du document : les sections 1 à 25 couvrent le projet ; la
> section 26 est un **audit formalisé** (faits établis, points incertains,
> incohérences constatées, dette documentaire, recommandations). Les sections
> dont le contenu était obsolète dans l'ancienne doc sont reconstituées ici à
> partir du code (sections 24, 20, 5, 6).

---

## Table des matières

1. [Présentation](#1-présentation)
2. [Architecture générale](#2-architecture-générale)
3. [Arborescence du dépôt](#3-arborescence-du-dépôt)
4. [Prérequis](#4-prérequis)
5. [Installation](#5-installation)
6. [Configuration](#6-configuration)
7. [Base de données](#7-base-de-données)
8. [Pipeline de données](#8-pipeline-de-données)
9. [Collecte temps réel](#9-collecte-temps-réel)
10. [Analyse et agrégation](#10-analyse-et-agrégation)
11. [Dashboard](#11-dashboard)
12. [Rapports mensuels](#12-rapports-mensuels)
13. [Référence des scripts et commandes](#13-référence-des-scripts-et-commandes)
14. [Services système (systemd)](#14-services-système-systemd)
15. [Nginx et HTTPS](#15-nginx-et-https)
16. [Réseau et sécurité](#16-réseau-et-sécurité)
17. [Logs](#17-logs)
18. [Maintenance](#18-maintenance)
19. [Sauvegarde et restauration](#19-sauvegarde-et-restauration)
20. [Git et déploiement](#20-git-et-déploiement)
21. [Dépannage (troubleshooting)](#21-dépannage-troubleshooting)
22. [FAQ](#22-faq)
23. [Limites connues](#23-limites-connues)
24. [Informations historiques et obsolètes](#24-informations-historiques-et-obsolètes)
25. [Glossaire](#25-glossaire)
26. [Audit de documentation](#26-audit-de-documentation)

---

## 1. Présentation

Urban Vision est un **observatoire indépendant de la fiabilité du réseau de
transport en commun TBM (Bordeaux Métropole)**. Il consomme les flux GTFS
publiques de TBM pour :

1. **Collecter en continu** les retards, avances et annulations de passages
   (`trip updates`), ainsi que les alertes travaux (`service alerts`) ;
2. **Stocker** l'historique dans une base SQLite locale ;
3. **Visualiser** l'état et l'évolution dans un tableau de bord web (Streamlit,
   graphiques Highcharts) ;
4. **Publier mensuellement** des rapports PDF (LaTeX) réseau et par commune
   de Bordeaux Métropole.

Il a été développé par Elias Khallouk.

**Données sources (clé d'API publique, sans quota documenté) :**

| Flux | URL |
|---|---|
| GTFS statique (horaires, lignes, arrêts) | `https://bdx.mecatran.com/utw/ws/gtfsfeed/static/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt` |
| GTFS-RT TripUpdates (retards/annulations) | `https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt` |
| GTFS-RT ServiceAlerts (alertes) | `https://bdx.mecatran.com/utw/ws/gtfsfeed/alerts/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt` |
| GTFS-RT VehiclePositions | `https://bdx.mecatran.com/utw/ws/gtfsfeed/vehicles/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt` *(non utilisé par le code actuel)* |

Utilisateurs : la Mairie/Bordeaux Métropole et les 28 communes de la Métropole
chacune peut recevoir un rapport mensuel dédié à ses arrêts. Le dashboard est
public en ligne (https://urban-vision.duckdns.org).

---

## 2. Architecture générale

```
                    ┌──────────────────────────────────────────────────┐
                    │               Flux TBM (protobuf)                │
                    │ static · realtime · alerts · (vehicles inutilisé)│
                    └───────────┬─────────────────┬────────────────────┘
                                │                 │
        collect.py (60 s)       │                 │  collect_alerts.py (120 s)
        TripUpdates             │                 │  ServiceAlerts
                                ▼                 ▼
      ┌───────────────────────────┐   ┌────────────────────────┐
      │    SQLite  urban_vision.db │◄──┤  observations / trip_status │
      │                            │   │  service_alerts            │
      │  observations, trip_status,│   └────────────────────────┘
      │  service_alerts,           │
      │  collection_gaps,          │
      │  routes, stops,            │
      │  agg_daily/agg_hourly/…,   │
      │  agg_daily_segment,        │
      │  stop_municipalities,      │
      │  municipalities,           │
      │  stop_direction,           │
      │  daily_line_stats          │
      └─────┬──────────────┬───────┘
            │              │
            │ (lectures SQL│ agrégées)
            ▼              ▼
      dashboard/app.py   generate_monthly_report.py
      Streamlit :8501    → LaTeX → PDF          analyze.py (bilan quotidien)
      (via nginx HTTPS)  → reports/output/      (daily_line_stats, usage marginal)
```

Flux de traitement en résumé :

1. **Collecte** : `collect.py` interroge TripUpdates toutes les 60 s et upsert
   chaque `stop_time_update` dans `observations` + un enregistrement par trajet
   dans `trip_status`. `collect_alerts.py` interroge ServiceAlerts toutes les
   120 s et alimente `service_alerts`. Les trous de collecte sont journalisés
   dans `collection_gaps`.
2. **Référentiel statique** : `gtfs_static.py` télécharge le GTFS statique et
   remplit `routes`/`stops`. `assign_stop_municipalities.py` rattache chaque
   arrêt à une commune (point-in-polygon sur les 28 contours officiels), avec
   secours via l'API Adresse.
3. **Agrégation** : `db.py` expose `refresh_aggregates()` qui calcule les
   tables `agg_daily`, `agg_hourly`, `agg_daily_stop`, `agg_hourly_stop`
   (histogrammes JSON des délais pour une médiane exacte). Le timer
   `urban-vision-rafraichir` les recalcule toutes les 5 min sur hier et
   aujourd'hui (`rafraichir_agregats.py`, hors de la boucle de collecte) ;
   le dashboard les reconstruit intégralement s'ils sont vides/incomplets.
   `refresh_segments()` calcule en plus `agg_daily_segment` (retard pris
   tronçon par tronçon, retard déjà présent en arrivant, arrêts non desservis par
   direction), au même rythme ; son rattrapage complet est fait par le
   recalcul planifié quand la table est vide
   (`rafraichir_agregats.py::ensure_segments`), jamais par le dashboard.
4. **Analyse quotidienne** : `analyze.py` calcule les statistiques par ligne
   (buffer de stabilisation 20 min, exclusion des trous de collecte) et les
   écrit dans `daily_line_stats` (usage historique, non lue par le dashboard).
5. **Visualisation** : `dashboard/app.py` (Streamlit) lit les tables agrégées
   et dessine les graphiques Highcharts (`dashboard/highcharts.py`), palette et
   seuils partagés via `reports/palette.py`.
6. **Rapports** : `reports/generate_monthly_report.py` (moteur) produit un
   rapport LaTeX par périmètre ; `generate_single_report.py` / `generate_all_reports.py`
   orchestrent les générations ; les PDF sont compilés avec **xelatex**.
7. **Veille** : `veille_collecte.py`, lancé toutes les 5 min par un timer
   systemd, contrôle la collecte (relevés, trous, volume du flux, flux figé,
   logs, sauvegardes, tâches planifiées) et envoie un email en cas d'anomalie
   (section 9.4) ; il peut aussi envoyer un signal de vie à une sonde externe.
8. **Protection des données** : sauvegarde de la base chaque nuit
   (`sauvegarde.py`, section 19) et archive du GTFS statique chaque fois qu'il
   change (`archive_gtfs.py`, section 9.5), car TBM ne publie que les horaires
   en cours et à venir.

**Environnements :**

| Environnement | Emplacement | Rôle |
|---|---|---|
| Dev (local) | `~/PROJECT/Urban-Vision` (dépôt git) | Développement + tests|
| Production | VM Oracle OCI `ek-hub` — user `ubuntu`, `/home/ubuntu/Urban-Vision` | Collecte 24/7 + dashboard public + génération de rapports  |

---

## 3. Arborescence du dépôt

Arborescence pertinente (hors `.git/`, `.venv/`, `data/*.db`, `.vscode` et
`reports/output/` qui sont gitignorés) :

```
Urban-Vision/
├── .claude/skills/urban-vision-design/
│   └── SKILL.md                    # charte graphique (skill Claude Code)
├── .github/workflows/tests.yml     # intégration continue : installation, ruff, pytest
├── .gitignore
├── .streamlit/
│   └── config.toml                 # thème + serveur 127.0.0.1:8501
├── AGENTS.md                       # consignes de maintenance pour les agents de code
├── CLAUDE.md                       # guide Claude Code (importe AGENTS.md)
├── apt-requirement.txt             # dépendances système (TeX + fonts)
├── assets/logo/
│   ├── urban-vision-logo-bw.png
│   ├── urban-vision-logo-color.png
│   └── urban-vision-logo-white.png # utilisé par le dashboard et les rapports
├── dashboard/
│   ├── app.py                      # dashboard Streamlit (pages, loaders, fiches, 3742 lignes)
│   ├── carte.py                    # carte des arrêts : composant st.components.v2, données, icônes
│   ├── carte_arrets.js             # JavaScript du composant (deck.gl + MapLibre)
│   ├── carte_troncons.py           # carte des points de contention : composant, données
│   ├── carte_troncons.js           # JavaScript de la carte des tronçons (deck.gl + MapLibre)
│   ├── diagnostic.py               # logique pure des fiches arrêt / ligne (verdicts, phrases)
│   ├── graphique.py                # graphique Highcharts cliquable (composant st.components.v2)
│   └── highcharts.py               # configs Highcharts (charte partagée)
├── data/                           # GITIGNORÉ (100 %)
│   ├── urban_vision.db             # base SQLite (~749 Mo en dev)
│   ├── urban_vision.db-wal         # journal WAL SQLite (~39 Mo)
│   ├── urban_vision.db-shm
│   ├── collect.log                 # logs du collecteur
│   ├── alerts.log                  # logs du collecteur d'alertes
│   ├── veille_collecte.json        # état des alertes de la veille (production)
│   ├── sauvegardes/                # sauvegardes quotidiennes compressées + manifestes
│   ├── gtfs_archive/               # versions successives du GTFS statique + index.json
│   └── dashboard.log               # logs Streamlit — local/dev uniquement
├── deploy/                         # configuration de la VM, versionnée
│   ├── deployer.sh                 # déploiement : code, dépendances, unités, contrôles
│   ├── systemd/                    # unités et timers urban-vision-*
│   ├── nginx/urban-vision          # vhost HTTPS (en-têtes de sécurité)
│   ├── logrotate/urban-vision      # rotation de collect.log et alerts.log
│   └── cron/root.crontab           # crontab root de référence (GoAccess, veille des visiteurs)
├── docs/
│   └── DOCUMENTATION_TECHNIQUE.md  # documentation technique (ce document)
├── pytest.ini                      # testpaths=tests, addopts=-q
├── LICENSE                         # licence du code : GNU AGPL-3.0
├── README.md                       # README racine (synthèse + pointeur docs/)
├── reports/
│   ├── generate_all_reports.py     # réseau + toutes les communes
│   ├── generate_monthly_report.py  # MOTEUR (rapport LaTeX/PDF, 1360 lignes)
│   ├── generate_single_report.py   # un rapport (réseau OU commune)
│   ├── palette.py                  # palette + seuils partagés (source unique)
│   ├── recipients.example.json     # profils de destinataires (modèle)
│   ├── plan/                       # 27 PDF de plans de réseau/communes (littéraux)
│   └── output/                     # GITIGNORÉ — résultats (2026-07, 2026-08, …)
├── requirements.txt               # dépendances Python épinglées (42 lignes)
├── requirements-dev.txt           # pytest, ruff
├── src/
│   ├── scripts/
│   │   ├── analyze.py              # bilan quotidien par ligne
│   │   ├── analyse_troncons.py     # faisabilité de la carte des tronçons (analyse, lecture seule)
│   │   ├── archive_gtfs.py         # archive du GTFS statique quand il change
│   │   ├── assign_stop_municipalities.py  # rattachement arrêts↔communes
│   │   ├── collect.py              # collecteur TripUpdates (60 s)
│   │   ├── collect_alerts.py       # collecteur ServiceAlerts (120 s)
│   │   ├── db.py                   # schéma SQLite + agrégats (source unique)
│   │   ├── export_open_data.py     # export CSV open data (lecture seule)
│   │   ├── gtfs_static.py          # chargement routes/stops
│   │   ├── indicateurs.py          # score de fiabilité : composantes, exclusions, marges
│   │   ├── rafraichir_agregats.py  # recalcul des agrégats (timer 5 min)
│   │   ├── route_shapes.py         # tracés des lignes extraits du GTFS (carte)
│   │   ├── troncons.py             # carte des points de contention (calcul nocturne)
│   │   ├── sauvegarde.py           # sauvegarde quotidienne, contrôle, restauration
│   │   ├── veille_collecte.py      # veille de la collecte + alertes email
│   │   └── veille_visiteurs.py     # veille des visiteurs humains (logs nginx)
└── tests/                          # 28 fichiers, 557 tests pytest
    ├── conftest.py                 # fixtures base temporaire
    ├── gtfs_factory.py             # generateurs de flux synthétiques
    └── test_*.py
```

---

## 4. Prérequis

**Système**

- Ubuntu 22.04 LTS (production = aarch64 / ARM64 Oracle OCI). Fonctionne aussi
  sur machine de développement Linux/x86_64.
- Python :
  - **Dev (local)** : Python **3.11.2** dans `.venv` ;
  - **Production (VM)** : venv **3.12.14**, Python système **3.10.12** ;
  - Le code requiert **Python ≥ 3.10** (syntaxe `str | None`). Pas de
    `.python-version` ni `runtime.txt` : la version est documentée ici à titre
    informatif.
- Accès réseau sortant vers : `bdx.mecatran.com`, `opendata.bordeaux-metropole.fr`,
  `api-adresse.data.gouv.fr`, et les CDN `code.highcharts.com` (dashboard) et
  Google Fonts (rapports, via `Lato` — **à confirmer** si téléchargé à la compilation).

**Python (cf. `requirements.txt`)**

Dépendances directes du projet : `streamlit` (1.60.0), `pandas` (3.0.5),
`numpy` (2.4.6), `requests` (2.34.2), `gtfs-realtime-bindings` (2.1.0) pour le
décodage protobuf, `protobuf` (7.35.1), `pyarrow` (24.0.0), `GitPython`
(3.1.59), `pillow` (12.3.0) pour l'atlas d'icônes de la carte. `pydeck`
(0.9.3) reste épinglé comme dépendance de Streamlit ; le dashboard ne l'utilise
plus (carte : composant deck.gl, §11.3). Le reste est des dépendances
transitives épinglées. La liste complète figure dans le fichier.

**Système (apt — cf. `apt-requirement.txt`)**

Nécessaire uniquement pour **compiler les rapports en PDF** :

```
texlive-latex-base
texlive-latex-recommended
texlive-latex-extra
texlive-lang-french
texlive-xetex
texlive-fonts-recommended
fonts-inter
```

`xelatex` (ou `lualatex`) est requis ; le moteur choisit
`shutil.which("xelatex") or shutil.which("lualatex")`
(`generate_monthly_report.py:1029`). La police `Inter` est définie pour
matplotlib, `Lato` pour la compilation du PDF.

**Exécution des tests** : `pytest ≥ 8` (`requirements-dev.txt`).

---

## 5. Installation

### 5.1 Récupération du code

```bash
git clone https://github.com/EliasKhallouk/Urban-Vision.git
cd Urban-Vision
```

En production, le dépôt vit dans `/home/ubuntu/Urban-Vision/` (déjà déployé).

### 5.2 Environnement Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# pour développer :
pip install -r requirements-dev.txt   # pytest
```

### 5.3 Création / mise à jour de la base

Le module `src/scripts/db.py` est **auto-exécutable** : à son import, il crée
la base au chemin `data/urban_vision.db`, applique le schéma complet
(`init_db`) et migre la colonne `departure_time` si absente (« migration »
fiabilisée dans le code, basée sur `PRAGMA table_info`, en complément du
script SQL versionné).

```bash
python src/scripts/db.py
```

Effet de bord à connaître : **tout import de `src/scripts/db.py`** (par exemple
`from db import ...` dans les tests, ou par `_ensure_aggregates` du dashboard)
ouvre aussi une connexion à la base réelle et applique `init_db` (idempotent).
C'est voulu (schéma auto-porteur) mais à garder en tête lors de contextes
offline.

### 5.4 Données de référence (une seule fois)

```bash
# 1. GTFS statique (routes/stops)
python src/scripts/gtfs_static.py

# 2. Rattachement des arrêts aux communes (contours officiels Bordeaux Métropole)
python src/scripts/assign_stop_municipalities.py
```

### 5.5 Lancement du dashboard en local

```bash
streamlit run dashboard/app.py
# ou, selon l'interpréteur :
.venv/bin/streamlit run dashboard/app.py
```

Le serveur écoute sur `127.0.0.1:8501` (config `.streamlit/config.toml`) ;
ouvrir http://127.0.0.1:8501.

### 5.6 Exécution des tests

```bash
.venv/bin/python -m pytest        # 431 tests (config : pytest.ini, -q)
```

Les tests n'utilisent aucune donnée réelle : bases SQLite temporaires
(`tmp_path`) + flux synthétiques (`tests/gtfs_factory.py`).

### 5.7 Installation des services en production

Toute la configuration de la VM est versionnée dans `deploy/` (section 14) et
installée par le script de déploiement (section 20.2) :

```bash
cd ~/Urban-Vision && deploy/deployer.sh            # met à jour, installe, redémarre, contrôle
deploy/deployer.sh --sans-pull --sans-redemarrage  # réinstalle la configuration seule
```

Prérequis propres à la VM, hors dépôt : identifiants SMTP de la veille
(section 6.5), adresse optionnelle de copie des sauvegardes hors VM
(section 6.6), clé BigDataCloud de la veille des visiteurs (section 17.2).

---

## 6. Configuration

Le projet **ne lit pas de `.env`** (aucun `os.getenv` dans le code). La
configurable via fichiers :

### 6.1 `.streamlit/config.toml`

```toml
[theme]
base = "light"
primaryColor = "#283618"
backgroundColor = "#FFFFFF"
secondaryBackgroundColor = "#ffffff"
textColor = "#283618"
font = "sans serif"

[browser]
gatherUsageStats = false

[server]
address = "127.0.0.1"     # n'écoute QUE sur localhost
port = 8501
```

### 6.2 `reports/recipients.json` (profils territoriaux)

Le moteur de rapports accepte `--profile <nom>` ; les profils sont lus dans
`reports/recipients.json` (gitignoré). Le fichier de référence à copier est
`reports/recipients.example.json`, qui définit :

- `bordeaux_metropole` : destinataire « Bordeaux Métropole et TBM », réseau
  complet ;
- 28 profils `mairie_<commune>` avec le destinataire « Mairie de <commune> »
  et la commune associée.

```bash
cp reports/recipients.example.json reports/recipients.json
# puis, par ex. :
python reports/generate_single_report.py --month 2026-08 --profile mairie_merignac --compile
```

> **Production** : `reports/recipients.json` est **absent** de la VM. La
> génération mensuelle y passe donc par `--network` / `--commune` (entrées
> uniques), ou exigerait `--recipients-file` explicite.

### 6.3 Constantes intégrées au code (valeurs centrales)

| Constante | Valeur | Emplacement |
|---|---|---|
| `POLL_INTERVAL_SECONDS` (TripUpdates) | 60 s | `collect.py:24` |
| `POLL_INTERVAL_SECONDS` (ServiceAlerts) | 120 s | `collect_alerts.py:24` |
| `GAP_THRESHOLD_SECONDS` | 180 s (3 × intervalle) | `collect.py:25` |
| `DB_BUSY_TIMEOUT_MS` (collect) | 120 000 ms | `collect.py:27` |
| `PREDICTION_HORIZONS` | prévision retenue à 10, 5 et 2 min du départ | `collect.py:28` |
| `DB_BUSY_TIMEOUT_MS` (alertes) | 180 000 ms | `collect_alerts.py:27` |
| Intervalle du recalcul des agrégats | 5 min (`OnCalendar=*:2/5`) | `deploy/systemd/urban-vision-rafraichir.timer` |
| `REFRESH_WARN_SECONDS` | 60 s (warning « Rafraîchissement des agrégats lent ») | `rafraichir_agregats.py:15` |
| `BACKFILL_PAUSE_SECONDS` | 1 s de pause entre deux jours rattrapés (tronçons, départs en avance par heure, historique du score), pour que le collecteur obtienne le verrou d'écriture | `rafraichir_agregats.py:16` |
| `SKP_LAST_SEEN_MARGIN_SECONDS` | 86 400 s (marge `last_seen_at` du recalcul incrémental) | `db.py:383` |
| Seuils de la veille (`HEARTBEAT_MAX_AGE_SECONDS`, `GAP_WINDOW_SECONDS`, `LOG_MIN_LINES`, `VOLUME_MIN_RATIO`, `VOLUME_MIN_BASELINE`, `REMINDER_SECONDS`, `FROZEN_WINDOW_SECONDS`, `FROZEN_MIN_RUNS`, `BACKUP_MAX_AGE_SECONDS`) | 600 s, 3 600 s, 3 lignes/h, 20 %, 2 000 passages, 12 h, 900 s, 5 relevés, 26 h | `veille_collecte.py:26-48` |
| `SIGNIFICANT_GAP_SECONDS` | 600 s (interruption comptée dans la méthode du rapport) | `generate_monthly_report.py:62` |
| `KEEP_DAILY` / `KEEP_MONTHLY` | 7 sauvegardes quotidiennes, 6 mensuelles | `sauvegarde.py:20-21` |
| `FRESHNESS_BUFFER_SECONDS` | 1200 s (20 min) | `analyze.py:17`, `generate_monthly_report.py:57`, `app.py:72` |
| `CACHE_TTL_SECONDS` (dashboard) | 60 s | `app.py:73` |
| `MIN_OBSERVATIONS` (dashboard) | 50 (aussi : arrêts listés dans « Arrêts les plus touchés » d'une fiche ligne) | `app.py:74` |
| `COLLECTION_HOURLY_DAYS` (dashboard) | 7 jours (répartition horaire du « Suivi de la collecte ») | `app.py:75` |
| `HIGHCHARTS_CDN` | Highcharts 13.1.1 servi par jsDelivr | `highcharts.py:137` |
| Veille des visiteurs (`RETENTION_DAYS`, `GEO_MAX_PER_RUN`) | 30 jours de conservation, 50 géolocalisations au plus par passage | `veille_visiteurs.py:41-43` |
| `timeout` HTTP (collecte) | 15 s | `collect.py:44`, `collect_alerts.py:59` |
| `timeout` HTTP (gtfs statique) | 30 s | `gtfs_static.py:25` |
| Seuil « retard de plus de 5 min » | > 300 s | commun (SQL, rapport, palette) |
| Seuil « en avance > 1 min » | < −60 s | `db.py:242`, `analyze.py:66` |
| Fenêtre « à l'heure » (`EARLY_TOLERANCE_SECONDS`, `LATE_TOLERANCE_SECONDS`), partout | de −60 s à +300 s | `indicateurs.py:10-11` |
| Quantiles de Student des marges (`T975`, puis `Z95`) | 12,706 pour 2 jours … 2,042 pour 31 jours, 1,96 au-delà | `indicateurs.py:12-15` |
| Temps réel douteux (`DOUBTFUL_ZERO_SHARE`, `DOUBTFUL_MIN_PASSAGES`) | au moins 25 % de retards exactement nuls, sur au moins 200 passages | `indicateurs.py:16-17` |
| Ligne fréquente (`FREQUENT_MIN_DEPARTURES_PER_HOUR`, `HEADWAY_MAX_SECONDS`) | au moins 5 passages prévus dans l'heure à l'arrêt ; intervalles de plus d'1 h ignorés | `db.py:564-565` |
| Qualité des jours (`QUALITY_HOURS`, `QUALITY_MIN_BASELINE`, `QUALITY_LACUNAR_RATIO`, `QUALITY_INCOMPLETE_HOURS`, `QUALITY_REFERENCE_WEEKS`) | heures de 5 h à 23 h ; référence d'au moins 500 passages ; heure lacunaire sous 50 % de la médiane des mêmes jours 1, 2 et 3 semaines avant ; jour incomplet à partir de 3 heures lacunaires | `db.py:649-653` |

### 6.4 Charte graphique et seuils (source unique : `reports/palette.py`)

Référence unique des couleurs **et** des seuils KPI, partagée par le dashboard,
les graphiques Highcharts et les rapports (via un ajout au `sys.path`).

**Échelle typographique du dashboard** (variables CSS de `inject_style`, seule
source des tailles ; test `TestEchelleTypographique`) :

| Variable | Taille | Usage |
|---|---|---|
| `--fs-page` | 1,75 rem (28 px), gras | titre de page (`st.title`) |
| `--fs-figure` | 1,6 rem, gras | valeur des cartes indicateurs |
| `--fs-section` | 1,3 rem, gras | titre de section (`###`), titre de fiche, nom du site |
| `--fs-block` | 1,0625 rem (17 px), gras | titre de bloc (`####`), titres des priorités |
| `--fs-body` | 1 rem (16 px) | texte courant, verdicts, « En bref », menu, replis |
| `--fs-small` | 0,875 rem (14 px) | légendes (`st.caption`), notes, libellés de listes, aides |
| `--fs-label` | 0,8125 rem (13 px), demi-gras | libellés et sous-textes des cartes, légendes de paliers |
| `--fs-micro` | 0,75 rem (12 px) | grille jour × heure, pastille « ? » |

Interlignage 1,2 (titres), 1,55 (texte), 1,45 (légendes). Texte secondaire en
Olive Leaf plein (`--ink-2`, contraste 5,6:1 sur blanc), sans transparence ;
aucune étiquette en capitales espacées. Graphiques : police Source Sans 3
chargée dans chaque graphique depuis jsDelivr (`CHART_FONT_CSS`, même famille
que l'application), libellés d'axes et légendes en 13 px, infobulles en 14 px,
étiquettes de données en 12 px, libellés d'axe non pivotés (les longs passent à
la ligne ; heures une sur deux). Carte des arrêts : infobulle en 13 px, titre en
15 px.

Mots affichés au lecteur (`TIER_LABELS`, `tier_label(value, kind)`) : palier
positif = « fiable », moyen = « à surveiller », négatif = « problématique ».
Le dashboard n'affiche jamais le nom interne d'un palier ni celui de sa
couleur (légendes `render_tier_legend`, cartes « État », tableaux).

Couleurs

| Nom | Hex | Usage |
|---|---|---|
| Black Forest | `#283618` | marque, titres, neutres |
| Olive Leaf | `#606c38` | positif |
| Sunlit Clay | `#DDA15E` | moyen |
| Copperwood | `#bc6c25` | négatif |
| Cornsilk | `#FEFAE0` | accent ponctuel (pastille de navigation active, texte sur cellules colorées) — jamais une couleur de score |
| White | `#FFFFFF` | fond de page (dashboard et rapports) |
| Teal | `#2A6F6F` | couleur d'appoint des graphiques matplotlib des rapports (non utilisée par le dashboard) |

Seuils (même valeur partout)

| Jeu de seuils | Positif | Moyen | Négatif |
|---|---|---|---|
| `score` (fiabilité/ponctualité) | ≥ 80/100 | 50–80 | < 50 |
| `retard` (en valeur **absolue**) | ≤ 60 s | 60–180 s | > 180 s |
| `pourcent` | ≤ 5 % | 5–15 % | > 15 % |

Modes de transport au dashboard (`MODE_LABELS`, `app.py`) : `{0: Tramway, 2: Rail,
3: Bus, 4: Ferry, 5: Câble, 7: Funiculaire, 11: Trolleybus}`. Un mode ne se
code **jamais par la couleur** (réservée aux paliers) mais par la forme du
marqueur : `palette.MODE_MARKERS = {0: circle (●), 3: square (■), 4: triangle (▲)}`,
losange (◆) pour tout autre mode ; `mode_marker(route_type)` renvoie le symbole
Highcharts, `mode_glyph(route_type)` le glyphe texte des légendes et libellés.
Les séries par mode sans score (courbes temporelles) sont en Black Forest avec
un style de trait propre au mode (`MODE_DASH` de `highcharts.py`).

Zones de la carte de risque (`palette.risk_zone(median_s, pct_gt300)`, utilisée
par le dashboard) :

| Zone | Retard médian (valeur absolue) | Passages > 5 min |
|---|---|---|
| Risque faible | < 60 s | < 15 % |
| Retards fréquents mais courts | ≥ 60 s | < 15 % |
| Retards rares mais longs | < 60 s | ≥ 15 % |
| Zone critique | ≥ 60 s | ≥ 15 % |

Les seuils (`RISK_MEDIAN_S`, `RISK_PCT_GT300`) reprennent la borne haute du
palier positif `retard` et la borne basse du palier négatif `pourcent`. Les
rapports PDF affichent les mêmes libellés de zone, placés aux coins du
graphique, sans seuil tracé.


### 6.5 Alertes email (`/etc/urban-vision/alertes.env`)

`veille_collecte.py` (section 9.4) lit ses identifiants SMTP dans les variables
d'environnement, sinon dans le fichier `UV_ALERT_ENV_FILE` (défaut
`/etc/urban-vision/alertes.env` : une variable `CLÉ=valeur` par ligne, `#` pour
les commentaires). Le fichier n'est pas dans git et n'est lisible que par
`root` ; l'unité systemd le charge par `EnvironmentFile=` (section 14).

| Variable | Défaut | Rôle |
|---|---|---|
| `UV_SMTP_USER` | — (obligatoire) | identifiant SMTP (adresse Gmail) |
| `UV_SMTP_PASSWORD` | — (obligatoire) | mot de passe d'application Gmail ou clé SMTP d'un autre fournisseur ; les espaces sont retirés |
| `UV_SMTP_HOST` | `smtp.gmail.com` | serveur SMTP |
| `UV_SMTP_PORT` | `465` | `465` = SSL ; toute autre valeur = STARTTLS (ex. `587`) |
| `UV_ALERT_TO` | `UV_SMTP_USER` | destinataire(s), séparés par des virgules |
| `UV_ALERT_FROM` | `UV_SMTP_USER` | expéditeur |
| `UV_HEARTBEAT_URL` | — (facultatif) | adresse de sonde externe (type healthchecks.io) appelée à chaque passage, suffixée de `/fail` quand une alerte est active |

Sonde externe (une fois) : créer un compte gratuit sur un service de
surveillance par signal de vie (par exemple healthchecks.io), créer un
contrôle de période 5 min et de tolérance 10 min, puis copier son adresse
dans `UV_HEARTBEAT_URL`. Le service prévient par email quand le signal cesse,
ce qui couvre aussi une panne de la VM entière, que la veille ne peut pas
signaler elle-même.

Mot de passe d'application Gmail (une fois) : activer la validation en deux
étapes du compte Google, puis <https://myaccount.google.com/apppasswords> →
nom « Urban Vision » → copier le code de 16 lettres (Google l'affiche en
4 groupes : les espaces sont facultatifs). Un autre fournisseur
(Brevo, Mailjet, SendGrid…) s'utilise avec `UV_SMTP_HOST`, `UV_SMTP_PORT`,
l'identifiant et la clé SMTP qu'il fournit.

Création du fichier sur la VM, puis envoi d'un email de test :

```bash
sudo install -d -m 755 /etc/urban-vision
sudo install -m 600 /dev/null /etc/urban-vision/alertes.env
sudo nano /etc/urban-vision/alertes.env
#   UV_SMTP_USER=adresse@gmail.com
#   UV_SMTP_PASSWORD=abcdefghijklmnop
cd ~/Urban-Vision && sudo .venv/bin/python src/scripts/veille_collecte.py --test-email
```

### 6.6 Copie des sauvegardes hors VM (`/etc/urban-vision/sauvegarde.env`)

Facultatif. `sauvegarde.py` (section 19) envoie chaque sauvegarde vers un
seau de stockage objet quand `UV_BACKUP_PAR_URL` est défini, par une simple
requête HTTP `PUT`, sans outil supplémentaire. Le fichier est chargé par
l'unité `urban-vision-sauvegarde.service` (`EnvironmentFile=-…`, facultatif).

Mise en place sur Oracle Cloud (Object Storage, inclus dans l'offre
gratuite) : créer un seau, puis une *requête pré-authentifiée* en écriture
sur ce seau (« Pre-Authenticated Request », accès « Permit object writes »,
avec une date d'expiration lointaine), et copier son adresse, qui se termine
par `/o/`. Ajouter une règle de cycle de vie qui supprime les objets de plus
de 30 jours.

```bash
sudo install -m 600 /dev/null /etc/urban-vision/sauvegarde.env
sudo nano /etc/urban-vision/sauvegarde.env
#   UV_BACKUP_PAR_URL=https://objectstorage.<région>.oraclecloud.com/p/<jeton>/n/<espace>/b/<seau>/o/
sudo systemctl start urban-vision-sauvegarde.service && journalctl -u urban-vision-sauvegarde.service -n 5
```

---

## 7. Base de données

Système : **SQLite** (pas d'ORM ; `sqlite3` standard). Fichier `data/urban_vision.db`
(~950 Mo en dev local ; **~3,6 Go en production** au 01/10/2026, 12,9 millions
d'observations) +
`-wal`/`-shm`. Mode **WAL** activé par les
collecteurs (`PRAGMA journal_mode=WAL;`) et par les tests ; le dashboard
n'active pas WAL lui-même mais émet `PRAGMA busy_timeout` (120 s) et
`cache_size=-65536`, `mmap_size=268435456`, `temp_store=MEMORY`
(`app.py:280-285`).

> En WAL, l'écrivain tient des verrous courts ; le collecteur (écrivain
> régulier, y compris le recalcul incrémental des agrégats : 17 à 19 s en
> production depuis le déploiement du 01/10/2026, contre ≈ 200 s avant le
> correctif de la section 9.3) et le service d'alertes coexistent grâce aux `busy_timeout`
> élevés (120 s / 180 s). Un `rollback` après erreur est effectué côté alertes
> (`collect_alerts.py:143`) — comportement couvert par
> `tests/test_collect_alerts.py::TestRecuperationApresVerrou`.

### 7.1 Schéma (source unique de vérité)

Tout est défini dans `src/scripts/db.py::SCHEMA_DDL` (créé de manière
idempotente), qui inclut `AGG_DDL` : `init_db` crée aussi les tables agrégées,
vides jusqu'au premier recalcul. `init_db` ajoute d'abord à une base existante les colonnes
apparues depuis sa création (`OBSERVATION_COLUMNS_ADDED`), puis applique le
schéma. Les tables `routes`/`stops` et `stop_municipalities`/`municipalities`,
ainsi que `stop_direction`, sont définies dans leurs modules respectifs
(`gtfs_static.py`, `assign_stop_municipalities.py`, `db.py`).

**`observations`** — chaque passage d'un véhicule à un arrêt (upsert à la collecte).
Clé primaire `(trip_id, start_date, stop_sequence)`.

| Colonne | Type | Rôle |
|---|---|---|
| `trip_id` | TEXT | identifiant du voyage |
| `start_date` | TEXT | date de service du voyage (`AAAAMMJJ`, fournie par le flux) |
| `route_id` | TEXT | ligne (code GTFS) |
| `direction_id` | INTEGER | sens du voyage |
| `stop_sequence` | INTEGER | rang de l'arrêt dans le trajet |
| `stop_id` | TEXT | arrêt |
| `schedule_relationship` | TEXT | `SCHEDULED`, `SKIPPED`, etc. |
| `arrival_delay` | INTEGER | retard à l'arrivée (s) — ignoré au 1er arrêt |
| `departure_delay` | INTEGER | retard au départ (s) — **variable d'analyse** |
| `departure_time` | INTEGER | heure de départ effective (epoch) — ajoutée par migration |
| `last_seen_at` | INTEGER | dernière fois où le flux mentionnait ce passage (epoch) |
| `pred_dep_10`, `pred_dep_5`, `pred_dep_2` | INTEGER | heure de départ prévue (epoch) lue au relevé où le départ annoncé passe sous 10, 5 puis 2 min ; `NULL` si l'horizon n'a pas été franchi sous les yeux du collecteur (passage apparu trop tard). Comparées à `departure_time` final, elles mesurent la fiabilité de l'information voyageur |

**`daily_line_stats`** — statistiques quotidiennes par ligne (écrites par `analyze.py` ;
**non lues** par le dashboard ni les rapports — usage historique).
PK `(stat_date, route_id)`.

**`collection_gaps`** — trous de collecte `(gap_start, gap_end)` en epoch. Écrits
par `collect.py` quand l'écart entre deux succès dépasse 180 s. **Exclus**
de l'analyse (cf. 7.3). Surveillés par `veille_collecte.py` (section 9.4).

**`collection_runs`** — journal de collecte, une ligne par relevé (`started_at`
en epoch, clé primaire) : horodatage du flux (`feed_ts`), entités, lignes
écrites, durées de téléchargement et d'écriture (ms), message d'erreur
(`NULL` si le relevé a réussi). Il donne la complétude exacte de la collecte,
sert de signal de vie à la veille et de point de reprise au collecteur.

**`trip_status`** — dernier statut connu par voyage. PK `(trip_id, start_date)`.

**`route_shapes`** — tracé principal de chaque ligne et sens, PK `(route_id,
direction_id)` : `shape_id` le plus emprunté dans `trips.txt`, nombre de ses
courses (`trips`) et coordonnées `[[lon, lat], …]` en JSON (`coords`),
simplifiées à 4 m près (Douglas-Peucker, `SIMPLIFY_METERS`) : 245 tracés,
≈ 117 points chacun, 626 Ko au 02/10/2026. Écrite par `route_shapes.py`.

**`service_alerts`** — alertes du flux ServiceAlerts, une ligne par
(alerte × route informée × période). PK `(alert_id, route_id, active_period_start)`.
Colonne `cause` (entier protobuf) quasi toujours `UNKNOWN_CAUSE` — ignorée dans
le dashboard et les rapports.

**Tables agrégées** (calculées par `db.py::refresh_aggregates` ; `agg_daily_trips`,
`agg_hourly_regularity` et `quality_days` par `db.py::refresh_v2`, qu'il appelle)

| Table | Granularité | Colonnes clés |
|---|---|---|
| `agg_daily` | jour × ligne | `obs`, `sum_delay`, `cnt_le300`, `cnt_gt300`, `cnt_lt60`, `skipped`, `eligible`, `histogram` |
| `agg_hourly` | jour × ligne × heure | `obs`, `sum_delay`, `cnt_le300`, `cnt_gt300`, `cnt_lt60` |
| `agg_daily_stop` | jour × ligne × arrêt | idem `agg_daily` + `skipped`/`eligible` |
| `agg_hourly_stop` | jour × ligne × arrêt × heure | idem `agg_hourly` |
| `agg_daily_trips` | jour de course × ligne | `scheduled`, `cancelled`, `added` : courses par statut, lues dans `trip_status` |
| `agg_hourly_regularity` | jour × ligne × sens × arrêt × heure | `n_act`, `sum_h_act`, `sum_h2_act` (intervalles réels entre deux départs), `n_sch`, `sum_h_sch`, `sum_h2_sch` (intervalles prévus) |
| `quality_days` | jour | `passages`, `lacunar_hours` (heures lacunaires, JSON), `flag` (`ok`, `degrade`, `incomplet`, `non_evalue`), `computed_at` |
| `agg_daily_segment` | jour × ligne × direction × arrêt | voir ci-dessous (calculée par `db.py::refresh_segments`) |
| `troncons_jour` | jour × tronçon (arrêt de départ, arrêt d'arrivée) × ligne × heure | `n` (passages gardés), `n_zero`, `n_terminus`, `n_plateau`, `gains` et `pcts` (valeurs gardées en texte : secondes perdues et % du temps prévu) ; section 10.5 |
| `troncons_jours_faits` | jour | `passages`, `computed_at` : jours déjà traités par `troncons.py` |
| `troncons_carte` | jour type × heure (−1 = journée) × tronçon | `statut` (`mesure`, `peu`, `terminus`, `douteux`), `palier`, `mediane_s`, `mediane_pct`, `passages`, `part_2min`, `jours` |
| `troncons_lignes` | tronçon × ligne | `passages` sur la période |
| `troncons_geo` | tronçon | `route_type`, `coords` (tracé JSON) |
| `troncons_meta` | clé | `depuis`, `jusqu_a`, `calcule_le`, `jours_exclus`, `lignes` (qualité de chaque ligne), en JSON |

- `cnt_le300` : passages avec retard ≤ 300 s.
- `cnt_gt300` : passages avec retard > 300 s.
- `cnt_lt60` : passages en avance de plus de 60 s. Un passage est **à l'heure**
  s'il part entre 60 s d'avance et 300 s de retard : `cnt_le300 − cnt_lt60`.
  La colonne a été ajoutée aux tables horaires (`agg_hourly`, `agg_hourly_stop`)
  après coup : `db.py::migrate_agg_columns` l'ajoute (valeur 0) aux bases
  existantes, appelé par `init_db` et `refresh_aggregates`, et
  `rafraichir_agregats.py::ensure_hourly_early` recalcule les jours concernés
  (section 10.2).
- `histogram` : JSON `{secondes_de_retard: effectif}` permettant de reconstruire
  une **médiane exacte** sur toute période (`app.py::_median_from_hists`).
- `agg_daily_trips` : `cancelled` = `CANCELED` ou `DELETED`, `added` = `ADDED`,
  `NEW`, `DUPLICATED` ou `REPLACEMENT`, `scheduled` = les autres statuts.
- `agg_hourly_regularity` : seules les heures avec au moins 5 passages prévus
  à l'arrêt sont conservées (lignes fréquentes) ; l'intervalle est attribué à
  l'heure du second départ.
- `quality_days` : un jour reçoit sa ligne dès que ses courses et sa qualité sont calculées
  (sauf le jour courant) ; elle sert aussi de marqueur au rattrapage de
  l'historique (section 10.2).

**`agg_daily_segment`** — retard pris tronçon par tronçon. Pour chaque voyage,
le retard observé à un arrêt est comparé à celui de l'**arrêt observé
précédent du même voyage** (`LAG(departure_delay) OVER (PARTITION BY trip_id,
start_date ORDER BY stop_sequence)`, passages `SCHEDULED` à retard connu). PK
`(date_service, route_id, direction_id, stop_id)` ; `direction_id` absent du
flux → `-1`.

| Colonne | Rôle |
|---|---|
| `eligible`, `skipped` | arrêts attendus (`SCHEDULED` + `SKIPPED`) et sautés, par direction (jour-service = `start_date`) |
| `sum_seq` | somme des `stop_sequence` des arrêts attendus : `sum_seq / eligible` = rang moyen de l'arrêt dans la ligne (ordre du profil) |
| `obs`, `sum_delay` | passages à retard connu et somme des retards à l'arrêt |
| `pairs` | passages ayant un arrêt observé précédent dans le même voyage |
| `sum_prev_delay` | somme des retards **déjà présents en arrivant** (à l'arrêt précédent) |
| `sum_gain` | somme des retards **pris sur le tronçon** (retard ici − retard à l'arrêt précédent) |
| `cnt_gain_gt120` | tronçons parcourus en perdant plus de 2 min |
| `prev_stop_id` | arrêt précédent le plus fréquent (libellé du tronçon) |
| `hist_gain` | JSON `{secondes_gagnées: effectif}` (médiane exacte du retard pris) |

Un arrêt `SKIPPED` n'interrompt pas le calcul : le tronçon relie les deux arrêts
observés qui l'encadrent.

**Tables de référence**

- `routes(route_id PK, route_short_name, route_long_name, route_type)` ;
- `stops(stop_id PK, stop_name, stop_lat, stop_lon)` ;
- `municipalities(insee_code PK, commune_name, boundary_source, updated_at)` ;
- `stop_municipalities(stop_id PK, insee_code, commune_name, assignment_method, assigned_at)` :
  `assignment_method` ∈ `point-in-polygon` | `api-adresse-reverse` ;
- `stop_direction(route_id, stop_id, direction_id, terminus)` PK `(route_id, stop_id)` :
  direction dominante par (ligne, arrêt) étiquetée par son terminus
  (noms d'arrêts en double distingués par le sens).

### 7.2 Index (déclarés dans `SCHEMA_DDL`)

- `idx_observations_last_seen_at` (sur `last_seen_at`)
- `idx_observations_route` (sur `route_id`)
- `idx_service_alerts_period` (`active_period_start, active_period_end`)
- `idx_observations_departure_time` (`departure_time, schedule_relationship, departure_delay, route_id`)
- + index sur `agg_daily(date_service)`, `agg_hourly(date_service)`,
  `agg_daily_stop(date_service)` et `(stop_id)`, `agg_hourly_stop(date_service)` et `(stop_id)`,
  `agg_daily_segment(route_id, date_service)` et `(stop_id)`,
  `agg_hourly_stop(route_id, date_service)` (profil d'une ligne par créneau),
  `agg_hourly_regularity(route_id, date_service)`
- `idx_stop_municipalities_commune` (sur `commune_name`, défini dans `assign_stop_municipalities.py`)

Le dashboard applique aussi en opportunité quelques index à la première
connexion (`app.py::INDEX_DDL`), erreurs avalées.

`init_db` supprime `idx_observations_sched_delay`
(`schedule_relationship, departure_delay, last_seen_at, route_id`) par
`DROP INDEX IF EXISTS` : plus aucune requête ne l'utilise et il ralentissait
chaque écriture du collecteur (section 9.3).

### 7.3 Convention d'analyse commune (anti-mesures)

Toute requête d'analyse définit un **seuil de stabilisation** par rapport au
dernier instant observé dans la base :

```
cutoff = MAX(last_seen_at) - FRESHNESS_BUFFER_SECONDS   # - 20 min
```

Seules les observations avec `last_seen_at < cutoff` sont considérées
définitives (le véhicule a quitté le flux). Cette règle est appliquée de façon
cohérente dans `analyze.py`, `app.py` et `generate_monthly_report.py`.

La question des **trous de collecte** est traitée différemment selon le
consommateur :

- `analyze.py` : exclut les observations tombant dans une fenêtre de ± buffer
  autour d'un `collection_gaps` ;
- `generate_monthly_report.py` : n'exclut pas les observations des trous, mais
  **compte** les secondes de coupure et les observations écartées dans la
  section « Méthode » (grâce à `query_collection_gaps`) ;
- `app.py` (dashboard) : n'applique pas l'exclusion des trous (l'écart est
  visible dans « Données & méthode › Suivi de la collecte »).

En revanche, le dashboard, les rapports et l'export open data écartent tous
de **tous** leurs indicateurs les mêmes jours et les mêmes lignes de la
période (`indicateurs.exclusions`, section 10.3) : jours `incomplet` de
`quality_days`, lignes au temps réel douteux et lignes Flex'.

---

## 8. Pipeline de données

Vue temporelle d'une journée-type :

```
06:00  ...  TBM publie les GTFS-RT (protobuf encodé, envoyé gzippé)
              │
 60 s        collect.py  ──► upsert observations / trip_status, ligne de collection_runs
 120 s       collect_alerts.py ──► upsert service_alerts
 5 min       rafraichir_agregats.py ──► refresh_aggregates(days=[hier, aujourd'hui])
                                    ──► refresh_segments(days=[hier, aujourd'hui])
                                    ──► courses et qualité (refresh_v2, quality_days)
 5 min       veille_collecte.py ──► alertes email, signal de vie
 02:30       sauvegarde.py ──► data/sauvegardes/ (+ copie hors VM)
 03:40       troncons.py ──► troncons_jour (jours manquants), troncons_carte, troncons_geo
 06:15       archive_gtfs.py ──► data/gtfs_archive/ si le GTFS a changé
              │
 à chaq. réexéc.    dashboard/app.py ──► lit agg_* (cache 60 s)
              │
 1er, 03:00   generate_all_reports.py ──► generate_monthly_report.py ──► xelatex ──► PDF
```

Détails des requêtes SQL d'agrégation (`db.py`)

- `_DAILY_SQL` / `_DAILY_STOP_SQL` : regroupent les `SCHEDULED` par
  (jour-service, ligne[, arrêt]), cumulent `obs`, `sum_delay`, compteurs ≤ 300 /
  > 300 / < −60 s, construisent l'histogramme JSON, et agrègent les `SKIPPED`
  pour donner `skipped`/`eligible` (jour-service dérivé de `start_date` au
  format `AAAAMMJJ`).
- `_HOURLY_SQL` / `_HOURLY_STOP_SQL` : même principe par heure locale
  (`strftime('%H', datetime(departure_time,'unixepoch','localtime'))`).
- `refresh_aggregates(days=None)` : **recalcul complet** (≈ 55 s pour
  3,2 millions d'observations sur le poste de dev ; durée en production à
  confirmer). `days=[...]` : **incrémental** — supprime puis recalcule les
  journées listées uniquement (c'est le mode utilisé par le collecteur pour
  hier et aujourd'hui).
- Requêtes incrémentales (`incremental_statements(days)`) : elles ne lisent
  `observations` que par deux index bornés, `idx_observations_departure_time`
  pour les délais (plage `departure_time` des jours traités) et
  `idx_observations_last_seen_at` pour les arrêts non desservis (jours traités ±
  `SKP_LAST_SEEN_MARGIN_SECONDS`, 1 jour ; dans la base, `last_seen_at` est
  compris entre +3,7 h et +28,4 h après le début du jour de service). Le `+`
  de `+o.schedule_relationship` empêche SQLite d'utiliser un index commençant
  par `schedule_relationship`, qui parcourrait tout l'historique (section 9.3).
  Les CTE `metrics`, `skpagg` et `hist` sont fusionnées par `UNION ALL` +
  `GROUP BY`, sans jointure.
- Jour-service : dérivé de `departure_time` (local) pour les délais, et de
  `start_date` pour les arrêts non desservis ; deux clauses bornées
  (`::SCHED_BOUNDS::` / `::SKP_BOUNDS::`) paramétrées par le mode d'exécution.
- Courses, régularité et qualité (`refresh_v2(days)`, mêmes modes complet et incrémental) :
  `_TRIPS_SQL` compte les courses de `trip_status` par jour de course
  (`start_date`) et par statut ; `_REGULARITY_SQL` calcule, par ligne, sens et
  arrêt, l'intervalle réel (`departure_time`) et l'intervalle prévu
  (`departure_time − departure_delay`) avec le départ précédent (`LAG`), et
  cumule par heure locale les intervalles et leurs carrés. En incrémental
  (`v2_statements(days)`), la lecture de `observations` est bornée par
  `idx_observations_departure_time` et commence une heure avant le premier
  jour, pour que le premier intervalle du jour ait son prédécesseur.
- `refresh_quality_days(days)` : qualité de chaque jour d'après `agg_hourly`
  (`day_quality`) ; une heure de 5 h à 23 h est lacunaire si elle compte moins
  de la moitié de la médiane des mêmes jours de la semaine 1, 2 et 3 semaines
  avant (référence d'au moins 500 passages). `incomplet` à partir de
  3 heures lacunaires, `degrade` pour 1 ou 2, `non_evalue` avec moins de deux
  semaines de référence. Le jour courant n'est jamais évalué.

`stop_direction` est un **backfill ponctuel** (`db.py::refresh_stop_directions`),
recalculé uniquement si la table est vide (direction la plus fréquente par
arrêt/ligne, étiquetée par le terminus au `stop_sequence` maximal). Utilisé par
le dashboard territorial et les rapports (colonne « direction »).

---

## 9. Collecte temps réel

### 9.1 `collect.py` (TripUpdates)

- Interroge l'URL `realtime/` toutes les **60 s** (`fetch_feed`, timeout 15 s).
- Décodage protobuf (`gtfs_realtime_pb2.FeedMessage`).
- Pour chaque `trip_update` :
  - écrit/upsert `trip_status` (statut du voyage) ;
  - pour chaque `stop_time_update` : upsert `observations`. Particularités :
    - `arrival_delay` ignoré si `stop_sequence == 1` (arrivées de dépôt
      incohérentes) ;
    - `departure_delay` et `departure_time` ne sont lus que si le champ
      `departure` est présent.
- Upsert idempotent (`ON CONFLICT(trip_id, start_date, stop_sequence)` : la
  dernière valeur gagne) — garantit que 60 s de collecte ne doublonnent rien
  (`last_seen_at` est mis à jour à chaque cycle pour les passages encore visibles).
- Détection des trous : si l'écart entre deux « succès » dépasse 180 s, un
  intervalle est inséré dans `collection_gaps` (+ warning log). Au démarrage,
  le dernier succès est lu dans `collection_runs` (repli sur
  `MAX(last_seen_at)`) : un redémarrage la nuit, quand le flux est vide, ne
  crée plus de faux trou de plusieurs heures.
- Journal de collecte : chaque relevé, réussi ou non, ajoute une ligne à
  `collection_runs` (section 7.1).
- Trajectoire des prévisions : l'upsert retient, dans `pred_dep_10`,
  `pred_dep_5` et `pred_dep_2`, l'heure prévue au relevé où le départ annoncé
  franchit 10, 5 puis 2 min (`PREDICTION_HORIZONS`) ; une valeur retenue n'est
  plus modifiée.
- Le collecteur ne calcule plus les agrégats ni les tronçons :
  `rafraichir_agregats.py` le fait toutes les 5 min dans un processus séparé
  (section 10.2), y compris le rattrapage de `agg_daily_segment` quand la table
  est vide, désormais jour par jour pour relâcher le verrou entre deux jours. Un recalcul lent ou
  en échec ne retarde plus aucun relevé ; l'écriture d'un relevé attend au plus
  la fin d'un recalcul en cours (verrou SQLite, `busy_timeout`).
- Boucle inconditionnelle ; les erreurs HTTP sont loggées et le cycle reprend.
  `time.sleep(max(0, interval - elapsed))` compense le temps de traitement.

### 9.2 `collect_alerts.py` (ServiceAlerts)

- Interroge l'URL `alerts/` toutes les **120 s** (timeout 15 s).
- Une même alerte peut viser plusieurs `route_id` et plusieurs `active_period` :
  la table `service_alerts` stocke le produit cartésien (alerte × route ×
  période). Alerte sans période → période illimitée (`start=0`, `end=NULL`) ;
  sans route informée → `route_id = ""` (impact réseau entier).
- Texte : traduction `fr` préférée, sinon première traduction.
- Upsert idempotent ; en cas d'erreur inattendue, `conn.rollback()` puis reprise
  au cycle suivant (bug de prod historique « database is locked » couvert par
  un test).

### 9.3 Considérations de performance

- La base est en mode **WAL** ; depuis le 01/10/2026, le recalcul des agrégats
  tourne dans son propre processus (timer toutes les 5 min) et ne bloque plus
  les relevés ; `busy_timeout` élevés des deux collecteurs et du recalcul.
- **Incident du 22/09/2026** (I8, section 26.3) : les requêtes incrémentales
  choisissaient `idx_observations_sched_delay` (égalité sur
  `schedule_relationship`) et parcouraient donc tous les passages `SCHEDULED`
  de l'historique au lieu des deux jours traités ; le filtre `start_date` des
  arrêts non desservis n'était porté par aucun index, et la jointure non indexée de
  `_DAILY_STOP_SQL` sur ses CTE était quadratique. La durée du
  rafraîchissement suivait la taille de la base (≈ 150 s mi-septembre, ≈ 200 s
  fin septembre en production) ; au-delà de 180 s, chaque rafraîchissement
  produisait un trou de collecte. Correctif : plages `departure_time` et
  `last_seen_at` portées par leurs index, `+o.schedule_relationship`,
  `UNION ALL` + `GROUP BY` (section 8). Mesures : 27,5 s → 2,9 s sur la base de
  dev (3,2 millions d'observations), résultats identiques ligne à ligne ;
  16,8 à 19,0 s par rafraîchissement en production depuis le déploiement du
  01/10/2026 (12,9 millions d'observations, VM 1 vCPU). Le coût dépend du volume des jours traités, plus de l'historique.
  Garde-fou : `tests/test_refresh_aggregates.py::TestRefreshIncrementalBorne`
  (plan de requête et équivalence avec le recalcul complet).
- `refresh_segments(days=[hier, aujourd'hui])` (fenêtre `LAG` sur les
  observations bornées par `departure_time`, élargie de
  `SEGMENT_LOOKBACK_SECONDS` = 3 h pour les voyages à cheval sur minuit) prend
  ≈ 1,6 s sur le poste de développement pour deux jours (5,2 s avant que sa
  partie « arrêts non desservis » soit bornée par `last_seen_at`, comme les agrégats ;
  `incremental_segment_statement`) ; le recalcul complet (~7 semaines) ~33 s.
  La requête assemble ses parties par `UNION ALL` + `GROUP BY` et lit
  `observations` par `idx_observations_departure_time` (`+o.schedule_relationship`). Durée
  en production à lire dans `collect.log` (« Tronçons rafraîchis en … s »).
- **Index `idx_observations_sched_delay` supprimé** : depuis le correctif
  ci-dessus, aucune requête ne l'utilisait, mais chaque relevé le mettait à
  jour. Sur une copie de la base de production du 01/10/2026, l'écriture d'un
  relevé est 34 % plus rapide sans lui et ≈ 400 Mo de pages sont libérées,
  réutilisées par les écritures suivantes (le fichier ne rétrécit qu'après
  `VACUUM`) ; les plans
  des requêtes du recalcul, du rapport et du dashboard sont inchangés, avant
  comme après `ANALYZE`. Le recalcul lance `PRAGMA optimize` après chaque
  passage (section 10.2) pour garder les statistiques du planificateur à jour.
- Les lectures du dashboard sont presque exclusivement sur les tables `agg_*`
  (petites) ; `observations` (grande table) n'est utilisée que sur la page
  « Suivi de la collecte », bornée aux 7 derniers jours par
  `idx_observations_last_seen_at` (observations par minute, répartition
  horaire). Les totaux de cette page viennent de `MAX(rowid)` (observations
  brutes : la table n'est jamais purgée et l'upsert conserve le `rowid`),
  `trip_status` (courses suivies) et `agg_daily` (lignes, passages analysés) :
  ≈ 1,6 s de requêtes sur la copie de production, contre ≈ 44 s pour les
  cinq parcours complets de `observations` qu'elle faisait jusqu'au
  01/10/2026.


### 9.4 Veille de la collecte et alertes email (`veille_collecte.py`)

Script en bibliothèque standard, lancé toutes les 5 min par
`urban-vision-veille-collecte.timer` (section 14). Il ouvre la base en lecture
seule, lit la fin (512 Ko) de `data/collect.log` et `data/alerts.log`, le
dernier manifeste de sauvegarde et l'état des tâches planifiées, évalue les
conditions suivantes et envoie un email (identifiants SMTP : section 6.5).

| Condition | Règle | Source |
|---|---|---|
| Collecte arrêtée | aucun relevé réussi depuis plus de 10 min ; l'email cite la dernière erreur | `collection_runs` (repli sur les lignes `OK - …` de `collect.log` si le journal est vide). Le journal enregistre aussi les relevés à 0 entité de 2 h à 5 h, alors que `last_seen_at` n'avance plus la nuit |
| Flux temps réel figé | au moins 5 relevés réussis sur 15 min, tous avec le même horodatage de flux | `collection_runs` |
| Trous de collecte | au moins un trou de `collection_gaps` terminé dans la dernière heure | base |
| Flux temps réel quasi vide | passages (`departure_time`) des 2 dernières heures, décalées de 10 min, inférieurs à 20 % de la médiane du même créneau les 3 semaines précédentes, quand cette médiane atteint 2 000 | base |
| Avertissements répétés dans les logs | au moins 3 lignes `WARNING`/`ERROR` dans la dernière heure, hors « Trou de collecte » | `collect.log`, `alerts.log` |
| Base de données illisible | erreur SQLite à l'ouverture ou à la lecture | base |
| Sauvegarde manquante | dernière sauvegarde de plus de 26 h, ou aucune | manifestes de `data/sauvegardes/` (condition ignorée si le dossier n'existe pas) |
| Tâche planifiée en échec | résultat `exit-code`, `timeout`, `signal`… pour le recalcul des agrégats, la sauvegarde, l'archive GTFS, les rapports ou la carte des tronçons | `systemctl show -p Result` (d'où `AF_UNIX` dans les familles autorisées de l'unité) |

- Un email part à l'apparition d'une alerte, puis un rappel toutes les 12 h
  tant qu'elle dure, et un email « Retour à la normale » à sa fin ; les
  conditions d'un même passage sont regroupées dans un seul email. Si l'envoi
  échoue, l'alerte est retentée au passage suivant et le script sort en code 1
  (unité en échec dans `systemctl --failed`).
- État des alertes : `data/veille_collecte.json` (gitignoré).
- Signal de vie : si `UV_HEARTBEAT_URL` est défini (section 6.5), chaque
  passage appelle cette adresse, suffixée de `/fail` quand une alerte est
  active ; un échec d'appel est seulement signalé sur la sortie d'erreur.
- Seuil de volume, calibré sur les données de production : hors incident, le
  ratio reste entre 0,90 et 1,10. Le 08/09/2026 (≈ 3 h–9 h 30) et le
  24/09/2026 (≈ 3 h–11 h), le flux TBM ne contenait presque plus de courses
  (ratio 0,01–0,04) sans qu'aucun trou soit enregistré ; rejouée sur ces deux
  journées, la veille alerte à 6 h. Un dimanche rapporté à un jour de semaine
  donne ≈ 0,25 aux heures de pointe : un jour férié en semaine reste au-dessus
  du seuil, à confirmer au premier férié (11/11/2026).

```bash
.venv/bin/python src/scripts/veille_collecte.py --dry-run    # diagnostic, ni email ni état
sudo .venv/bin/python src/scripts/veille_collecte.py --test-email
journalctl -u urban-vision-veille-collecte.service -n 20
```

Options : `--db`, `--log-dir`, `--state` (par défaut, les chemins du dépôt).

### 9.5 Archive du GTFS statique (`archive_gtfs.py`)

TBM ne publie que les horaires en cours et à venir (le fichier téléchargé le
01/10/2026 couvre du 01/10 au 30/12/2026) : sans archive, l'offre prévue d'un
jour passé est perdue. Le timer `urban-vision-archive-gtfs` télécharge le GTFS
statique chaque jour à 6 h 15 (TBM le régénère vers 4 h 50), contrôle qu'il
contient `trips.txt`, `stop_times.txt`, `routes.txt` et `stops.txt`, puis le
conserve dans `data/gtfs_archive/gtfs_<date>_<empreinte>.zip` seulement si son
empreinte SHA-256 est nouvelle (22,7 Mo par version). `index.json` liste les
versions (dates de validité et version lues dans `feed_info.txt`) et la date du
dernier contrôle.

À chaque nouvelle version, ou si `route_shapes` est vide, le même passage
recharge les tracés des lignes depuis le fichier téléchargé
(`route_shapes.refresh_route_shapes`, ≈ 1,5 s) ; un échec est signalé sans
faire échouer l'archive.

```bash
.venv/bin/python src/scripts/archive_gtfs.py            # --dest, --db pour d'autres chemins
.venv/bin/python src/scripts/route_shapes.py            # tracés depuis la dernière archive (--zip, --db)
```

---

## 10. Analyse et agrégation

### 10.1 `analyze.py` — bilan quotidien (chaîne legacy)

- `load_completed_observations(conn)` : observation = entrée avec
  `last_seen_at < max(last_seen_at) − 20 min`, jointes aux `routes`/`stops`;
  exclusion des observations tombant dans une fenêtre autour d'un trou de
  collecte (buffer 20 min avant le début du trou).
- `compute_line_stats(df)` : par (ligne, nom court GTFS) → `n_observations`,
  `retard_moyen_s`, `retard_median_s`, `pct_retard_5min` (retard > 300 s),
  `pct_avance_1min` (retard < −60 s), `n_arrets_sautes` (les `SKIPPED` sont
  comptés à part, hors calcul de retard). Tri par `pct_retard_5min` décroissant.
- `save_stats_to_db(conn, stats, stat_date)` : upsert dans `daily_line_stats`
  (idempotent, PK `(stat_date, route_id)`).

> Usage : quotidien/ponctuel. Le dashboard et les rapports ne lisent **pas**
> cette table (ils utilisent les `agg_*`). Elle reste conservée pour l'historique.

### 10.2 `db.py::refresh_aggregates` — agrégats du dashboard (chaîne principale)

Le recalcul de routine est lancé toutes les 5 min par
`urban-vision-rafraichir.timer` : `rafraichir_agregats.py` recalcule les
agrégats puis les tronçons (`refresh_segments`) d'hier et d'aujourd'hui, après
avoir rattrapé jour par jour `agg_daily_segment` s'il est vide (pause
d'une seconde entre deux jours, `BACKFILL_PAUSE_SECONDS`), journalise la durée dans `data/collect.log` (« Agrégats rafraîchis
en X s », warning « Rafraîchissement des agrégats lent » au-delà de 60 s) et
sort en code 1 en cas d'échec, ce que la veille signale (« Tâche planifiée en
échec »).

Ensuite, `ensure_hourly_early` recalcule, un jour à la fois et avec la même
pause, les jours dont `agg_daily` compte des départs en avance mais pas
`agg_hourly` (`db.py::hourly_early_missing_days` : colonne `cnt_lt60` ajoutée
après coup, ≈ 2,5 s par jour sur le poste de développement) ; échec journalisé
« Rattrapage des départs en avance par heure échoué » (code 1).

Puis `ensure_v2_history` calcule les courses et la qualité des jours de `agg_daily`
antérieurs à aujourd'hui qui n'ont pas encore de ligne dans `quality_days`,
un jour à la fois, avec la même pause d'une seconde entre deux jours pour que
le collecteur obtienne le verrou d'écriture (≈ 4 s par jour en production),
puis réévalue la qualité de tous les jours ; c'est ce qui
construit l'historique au premier passage après un déploiement. Un jour sans
course connue dans `trip_status` reçoit quand même sa ligne de qualité : il
n'est pas recalculé à chaque passage. `PRAGMA optimize` suit. Échec
journalisé « Méthode 2.0 : calcul de l'historique échoué » (code 1, libellé
inchangé dans les logs).

Cf. sections 7.1 et 8. Les valeurs réseau du dashboard sont issues d'une
lecture des `agg_*` puis de regroupements en mémoire :

- `app.py::_load_daily_core` → `agg_daily` (+ variante `agg_daily_stop`
  filtrée par `stop_municipalities` pour la vue territoriale), jointe à
  `agg_daily_trips`, hors jours et lignes écartés (`load_exclusions`) ; les
  composantes du score sont ajoutées ligne × jour (`indicateurs.add_components`) ;
- `app.py::_group_daily_stop_to_route` : la vue territoriale par arrêt est
  regroupée au niveau ligne (sommes + fusion des histogrammes JSON, courses de
  la ligne ce jour-là) avant le calcul des composantes ;
- `app.py::_daily_to_network` + `make_ranking` : somme les composantes par
  ligne, calcule `pct_arrets_sautes` (skipped/eligible), `pct_service` et
  `score_fiabilite = indicateurs.score_of(Σ assurés à l'heure, Σ attendus)`,
  le mode, et trie par score croissant (du plus prioritaire au meilleur) ; une
  ligne sans passage attendu n'est pas classée.
- Médiane : reconstruite à partir des histogrammes JSON (`_median_from_hists`).

### 10.3 Score de fiabilité (formule centrale, `indicateurs.py`)

Le score de fiabilité est le même partout : bandeau, classements, carte,
fiches, créneaux et évolutions du dashboard, rapports mensuels et export open
data. Il répond à la question : sur 100 passages prévus, combien ont été
assurés et à l'heure ? Il remplace depuis le 11/10/2026 l'ancienne formule
`max(0 ; ponctualité ≤ 5 min − 2 × taux d'arrêts non desservis)`, qui
comptait « à l'heure » un départ très en avance et ignorait les courses
supprimées ; il a été publié à côté d'elle (« méthode 2.0 ») en septembre
2026. Les rapports PDF déjà produits (jusqu'à celui de septembre 2026) gardent
l'ancien calcul ; la page « Rapports mensuels » du dashboard le signale.

```
Par ligne et par jour (et par arrêt quand le périmètre est un arrêt) :
  à l'heure           = passages partis entre −60 s et +300 s (cnt_le300 − cnt_lt60)
  supprimées nettes   = max(supprimées − ajoutées, 0)
  passages supprimés  = supprimées nettes × éligibles / (prévues + ajoutées)
  attendus            = éligibles (SCHEDULED + SKIPPED) + passages supprimés
  assurés             = éligibles − sautés
  assurés à l'heure   = à l'heure / passages observés × assurés
Sur un regroupement quelconque (période, ligne, arrêt, commune, mode, jour) :
  Score de fiabilité  = 100 × Σ assurés à l'heure / Σ attendus
  Service assuré      = 100 × Σ assurés / Σ attendus
  À l'heure           = 100 × Σ à l'heure / Σ passages observés
```

- **Source unique** : `indicateurs.add_components(df)` ajoute les colonnes
  `on_time`, `expected`, `assured`, `assured_on_time` à n'importe quel tableau
  d'agrégats (ligne × jour ou arrêt × ligne × jour) ; `score_of(Σ assurés à
  l'heure, Σ attendus)` donne le score (NaN sans passage attendu). Les
  composantes se somment sur tout regroupement : le score d'une commune, d'un
  mode ou du réseau est celui de la somme de ses lignes et de ses jours, pas une
  moyenne de scores. `route_day_components` fait le même calcul ligne à ligne
  (méthode `indicators`) ; un test vérifie que le dashboard et `indicators`
  donnent le même score.
- **Décomposition exacte** : 100 − score = (100 − service assuré) [service non
  rendu : arrêts non desservis, courses supprimées] + (service assuré − score)
  [horaires non tenus : retard de plus de 5 min ou avance de plus d'1 min]
  (`diagnostic.score_breakdown`).
- **Exclusions, partout** (`indicateurs.exclusions(conn, début, fin)` →
  `Exclusions(jours, lignes)`, filtre SQL `exclusion_sql(alias, excl)`) : jours
  `incomplet` de `quality_days`, lignes au temps réel douteux de la période,
  lignes Flex'. Elles s'appliquent à tous les indicateurs (passages, retards,
  carte, fiches, rapports, open data), pas seulement au score.
- **Créneaux** : les arrêts non desservis et les courses supprimées n'ont pas
  d'heure ; le score d'un créneau est sa part à l'heure × le service assuré des
  jours qu'il couvre (semaine ou week-end, `app.py::period_day_rates`).
- Seuils de lecture inchangés : ≥ 80 fiable (positif), 50–80 à surveiller
  (moyen), < 50 problématique (négatif).

### 10.4 Marges, comparaisons et qualité des données (`indicateurs.py`)

Le score est accompagné d'une marge d'incertitude (« Données & méthode », fiche
ligne, page « Détail du score de fiabilité » du rapport mensuel).

Fonctions de période : `indicators(conn, début, fin, routes, communes,
stop_ids, day_types)` renvoie un `Indicateurs` (score, marge, ponctualité à
l'heure et sa marge, service assuré, part des départs en avance, passages,
attendus, jours, jours exclus, lignes écartées), sur les mêmes exclusions que
section 10.3.

- **Courses supprimées nettes** : lors d'un incident, le flux publie souvent
  la suppression des courses prévues puis des courses de remplacement
  (`NEW`). Le 16/09/2026 sur le tram B, 53 courses supprimées sans horaire
  ont été remplacées par 59 courses `NEW` entre 8 h et 10 h. Compter les
  suppressions brutes pénaliserait un service effectivement rendu.
- **Marges (±)** : intervalle de confiance à 95 % d'un estimateur par ratio,
  les jours étant les unités d'échantillonnage (`ratio_estimate`), avec le
  quantile de Student à n − 1 degrés de liberté (`student_quantile`). Elles
  mesurent la variabilité d'un jour à l'autre. Un écart entre deux périodes
  est significatif s'il dépasse √(m₁² + m₂²) (`compare`).
- **Exclusions** : jours `incomplet` de `quality_days` (`excluded_days`),
  lignes au temps réel douteux, dont au moins 25 % des retards valent
  exactement zéro sur au moins 200 passages (`doubtful_routes`), lignes Flex'.
- **Régularité des lignes fréquentes** : attente moyenne = Σh² / (2 Σh) sur
  les intervalles h réels et prévus (`regularity`) ; l'attente excédentaire
  est leur différence (`route_regularity`, jours incomplets exclus, nombre
  de jours renvoyé).
- **Rattachement des jours** : attendus et assurés au jour de la course
  (`start_date`), ponctualité au jour du départ effectif ; les deux ne
  diffèrent que pour les départs après minuit.
- **Fonctions** : `indicators(conn, début, fin, routes, communes, stop_ids,
  day_types)` renvoie un `Indicateurs` (score, marge, ponctualité et sa marge,
  service, part des départs en avance, passages, attendus, jours, jours
  exclus, lignes écartées) ; `route_indicators` donne les mêmes indicateurs
  par ligne ; `quality_flags` lit `quality_days`. Les bornes sont des jours
  `AAAA-MM-JJ`, fin exclue.

Septembre 2026 (copie de la base de production du 01/10/2026) : score
75,1 ± 1,2 contre 83 avec l'ancienne formule ; à l'heure
78,4 ± 0,9 % ; service assuré 95,9 % ; 8,1 % des passages partent plus
d'une minute en avance. Jours de semaine : 73,9 ± 1,0 contre 78,1 ± 1,3 en
août, baisse significative de 4,2 ± 1,7 points ; samedis et dimanches dans
la marge. Jours exclus : 08/09 et 24/09 ; lignes écartées : 102, 306,
TBNight et S22. Les lignes scolaires (S35, S49…) reculent le plus : 60 à
80 % de leurs départs ont plus d'une minute d'avance.

### 10.5 Carte des points de contention (`troncons.py`)

Elle colorie chaque tronçon entre deux arrêts consécutifs selon le temps que
les bus y perdent (circulation, feux ou montée des voyageurs ; jamais « la rue »
ni « l'exploitant » comme responsables). Faisabilité et calibrage :
`analyse_troncons.py` (section 13), sur la sauvegarde de production du
11/10/2026, période scolaire du 01/09 au 10/10/2026.

**Mesure** (`passages_frame`) : pour un même trajet, couple (arrêt i, arrêt
i+1) de rangs consécutifs, tous deux `SCHEDULED` avec retard connu. Temps perdu
= retard au départ de i+1 − retard au départ de i ; temps prévu = écart des
horaires prévus (`departure_time − departure_delay`) ; part du temps prévu =
temps perdu / temps prévu. L'heure et le jour sont ceux du départ de l'arrêt i.
Sont écartés : paires non consécutives, première et dernière paire du trajet
(départ du terminus, arrivée : « début ou fin de ligne »), passages dont un
retard vaut exactement 0 s (horaire théorique republié), écarts au-delà de
±15 min (`OUTLIER_SECONDS`), temps prévu nul ou au-delà de 30 min.

**Qualité des lignes** (`line_quality`, sur la période) : une ligne est
mesurée si moins de 25 % de ses retards valent exactement 0
(`DOUBTFUL_ZERO_SHARE`, comme le score) et moins de 15 % de ses passages gardés
perdent 0 s pile (« paliers » : retard recopié d'un arrêt à l'autre,
`MAX_PLATEAU_SHARE`). En septembre-octobre 2026, 93 lignes de bus sur 129 (99 %
des passages de bus) sont mesurées ; sont en gris les lignes sans temps réel
(16 lignes à 100 % de zéros), les lignes scolaires à paliers et TBNight (29 %
de zéros, 28,5 % de paliers : son temps réel est recopié par paliers, comme
celui du tram, quel que soit le seuil). Le **tram** n'est pas mesuré : son
retard est publié par paliers (30 % d'écarts de 0 s pile), la médiane par
tronçon cache environ 90 % du temps perdu (ligne A : somme des médianes 20 s
pour 237 s perdues de bout en bout) ; ses tracés sont dessinés en gris hachuré.
Le **ferry** : seule la ligne 952 a des tronçons mesurables (deux tronçons du
milieu, environ 1 600 passages) ; les traversées des lignes 951 et 953 n'ont
que deux escales, leurs tronçons sont « début ou fin de ligne ».

**Paliers** (`palette.troncon_tier`, règle validée le 11/10/2026) : médiane du
temps perdu et médiane de la part du temps prévu. Beaucoup de temps perdu
(Copperwood) : au moins 40 s **et** la moitié du temps prévu ; un peu (Sunlit
Clay) : au moins 15 s **et** un quart ; sinon pas de temps perdu (Olive Leaf).
Les deux conditions évitent de colorer à tort les tronçons longs (avec les
secondes seules, 25 % des tronçons de plus de 5 min prévues étaient rouges,
contre 0,7 % des courts) comme les très courts (pourcentage seul). Sur les
cases tronçon × heure des bus mesurés : 90 % vert, 8 % ocre, 1,4 % cuivre ;
95 % des cases gardent leur palier entre jours pairs et impairs. Bruit :
écart d'un jour à l'autre de la médiane de 3 s (7 s au 90e centile).

**Assez de données** : au moins 20 passages (`MIN_PASSAGES`) sur au moins
3 jours (`MIN_DAYS`) par tronçon et par heure (ou journée). Sinon gris « pas
assez de données ». Un trou de collecte ne donne aucun passage, donc du gris,
jamais du vert. Jours `incomplet` et lignes Flex' exclus ; jours types
séparés (semaine, samedi, dimanche).

**Calcul nocturne** (`run`, timer `urban-vision-troncons`, 3 h 40) :
`refresh_day` remplit `troncons_jour` pour chaque jour de `agg_daily` depuis
`PERIOD_START` (le 01/09/2026, variable d'environnement
`URBAN_VISION_TRONCONS_DEPUIS`) non encore traité (`troncons_jours_faits`),
une seconde de pause entre deux jours ; `build_map` calcule les médianes
exactes en Python, par paquets d'arrêts de départ (dernier chiffre de
`from_stop_id`, `PARTITIONS`) pour limiter la mémoire ; `build_geometry`
découpe le tracé de `route_shapes` entre les projections des deux arrêts
(`segment_geometry`, à moins de 150 m et dans le bon ordre), sinon un segment
droit. Les calculs sont faits avant d'écrire : chaque écriture est une
transaction courte (`with conn`), le collecteur n'attend jamais un calcul.
Mesuré en développement sur la copie de production : 5,5 s par jour, dont
0,7 s d'écriture (verrou) ; carte 69 s et géométrie 10 s, écrites chacune en
une transaction ; mémoire au plus 612 Mo (valeurs lues en tableaux d'entiers,
`_int_values`) ; `troncons_jour` compte 2,9 millions de lignes (35 Mo de
valeurs) pour 39 jours. Sur la VM, à `Nice=19` et E/S `idle`, la collecte
(toutes les 60 s, attente du verrou jusqu'à 120 s) passe devant : impact
attendu nul hors ces écritures d'au plus quelques secondes. Échec journalisé « Tronçons : calcul
échoué » dans `data/collect.log` (code 1, signalé par la veille).

**Couverture** (semaine type, tronçons de bus colorés) : 77 % sur la
journée, 68 % à 8 h, 67 % à 17 h, 49 % à 21 h. Les communes les moins couvertes
sont en périphérie : Le Taillan-Médoc (47 % sur la journée, 40 % à 17 h),
Bouliac (48 %), Carbon-Blanc (58 %), Saint-Aubin-de-Médoc (59 %, 24 % à 17 h),
Ambarès-et-Lagrave (62 %).

---

## 11. Dashboard

### 11.1 Lancement

```bash
streamlit run dashboard/app.py        # → 127.0.0.1:8501 (config .toml)
```

Le point d'entrée `if __name__ == "__main__": main()` crée une connexion SQLite
par exécution, la ferme dans un `finally` ; les garde-fous : base absente →
`st.error`, pas d'observations → `st.warning`.

### 11.2 Connexion et cache

- `get_connection()` : `sqlite3.connect(DB_PATH)` + `row_factory`, `busy_timeout
  120_000`, cache 64 Mo, mmap 256 Mo, `temp_store=MEMORY`, agrégat SQL
  `median_s` (déprécié, « kept for compatibility »), puis `_ensure_aggregates`.
- `_ensure_aggregates(conn)` : importe `src/scripts/db.py` (via `sys.path`),
  applique `AGG_DDL`, **reconstruit les agrégats si `agg_daily` est vide ou si
  `agg_daily_stop` couvre moins de jours** (`refresh_aggregates(None)`) et
  backfill `stop_direction` si vide.
- Modules partagés via `sys.path` : `reports/` (palette) et `src/scripts/`.
- **Cache Streamlit** : tous les loaders sont décorés
  `@st.cache_data(ttl=60)` (le premier paramètre `_conn` n'est pas haché).
- Seuil de fraîcheur : `cutoff = MAX(last_seen_at) − 20 min`
  (`FRESHNESS_BUFFER_SECONDS`).

### 11.3 Structure des vues

Navigation par `st.radio` dans la sidebar (pas d'onglets natifs), 8 pages :
6 pages d'analyse organisées par question, puis « Rapports mensuels » et
« À propos » (`NAV_ITEMS`, une fonction `render_page_*` par page, aiguillage
par le dictionnaire `PAGES` ; contexte commun `PageContext`). Chaque
page suit le même ordre de lecture : une phrase de verdict (bloc `insight`), le
graphique principal, puis le détail en blocs repliables. Seule la page active
est calculée ; les pages qui regroupent plusieurs vues passent par
`st.segmented_control`, qui ne calcule que la vue affichée.

**Vue rapide et vue détaillée.** Un interrupteur « Vue détaillée » sous la
navigation (`detailed_view`, transmis aux pages par `PageContext.detailed`)
choisit le niveau de lecture ; la vue rapide est celle par défaut, pour un
lecteur qui n'est pas analyste :

- **bandeau** (`header_kpis`) : en vue rapide, quatre cartes en mots, « État »
  (« Fiable », « À surveiller » ou « Problématique », score sur 100 en
  dessous : `status_kpi`), « À l'heure », « Retard moyen » et « Arrêts non
  desservis » ; en vue détaillée, les cinq mesures d'origine et leurs
  définitions techniques (SCHEDULED, SKIPPED). Chaque carte porte une aide
  (`HELP_TEXTS`) : pastille « ? » pleine de 20 px dans `kpi_card`, qui ouvre au
  survol ou au clavier une bulle avec la définition et un lien « Voir dans le
  lexique ↗ » vers le terme correspondant (`HELP_TERMS`, `lexique_link`), ouvert
  dans un nouvel onglet pour ne pas perdre la sélection en cours. Les icônes
  « ? » des listes (aide Streamlit) sont agrandies à 19 px, en vert foncé. Une carte évaluée
  (palier fiable, à surveiller ou problématique, ou évolution) est remplie de
  la couleur de son palier, texte crème, vert foncé ou blanc selon le fond
  (`KPI_FILLS`) ; une carte neutre reste blanche à liseré. Les pages
  d'information (`INFO_PAGES` : « Rapports mensuels », « À propos ») n'ont ni
  titre ni bandeau ;
- **vue rapide** : verdict en phrase, graphique principal et, dans les
  fiches, état, évolution, type de retard, « En bref » et « Pistes ». Restent
  réservés à la vue détaillée : les blocs repliables de « Mon territoire »
  (comparaison des communes, tous les arrêts) et de « Quand ? », le tableau
  de toutes les lignes, les sous-vues des fiches (où, quand, quel type,
  contexte) et la ligne « Détail du score » de la fiche ligne. La page « Modes de
  transport » est la même dans les deux vues ;
- **guide de lecture** (`show_guide`, boîte `st.dialog`) : trois étapes
  (`GUIDE_STEPS` : choisir territoire et période, lire l'état en un mot,
  cliquer pour comprendre), affichées au premier chargement de la session
  sauf sur un lien direct vers une fiche (`should_show_guide`), et
  rouvrables par le bouton « Guide de lecture » de la sidebar ; la boîte se
  ferme par ✕ ou Échap, sans relancer le calcul de la page ; une fois vu, le
  guide n'est plus ouvert automatiquement par ce navigateur (cookie
  `uv_guide_vu`, un an, posé par `remember_guide_seen`, lu par
  `st.context.cookies`) ;
- **vocabulaire** : « arrêt non desservi » remplace « arrêt sauté » partout
  (dashboard et rapports) ; le lexique (`LEXIQUE`) est la première vue de
  « Données & méthode ».

1. **Mon territoire** — pour une commune, verdict (score de la commune
   comparé au réseau, tous deux calculés par `network_score` sur les totaux,
   comme le bandeau) ; bloc **Priorités du moment** : un tableau 2 × 2
   (`_render_priorities`, conteneur `prio_grid`) qui croise *quoi* (lignes du
   tableau : arrêt, ligne) et *quel signal* (colonnes : « Le plus de retards »,
   « La plus forte baisse » du score par rapport à la période de comparaison),
   chaque libellé n'étant écrit qu'une fois (`diagnostic.priorities`,
   `PRIORITY_GRID`). Chaque case (`priority_cell_html`) donne le chiffre en
   grand (passages partis avec plus de 5 min de retard, ou points de score
   perdus en Copperwood), puis l'arrêt ou la ligne :
   - une ligne est une pastille à son nom, au fond de la couleur de son état,
     comme un pictogramme de réseau (`line_badge_html`) ;
   - un arrêt porte le glyphe de son mode (● ■ ▲) à la couleur de son état,
     comme sur la carte (`stop_mark_html`) ;
   - la colonne « baisse » dessine la pente du score d'une période à l'autre
     sur l'échelle 0–100, avec les seuils 80 et 50 en pointillés (`slope_svg`,
     décrite pour les lecteurs d'écran par son `aria-label`) ;
   - un lien « Ouvrir la fiche de l'arrêt / de la ligne » (bouton Streamlit
     présenté en lien souligné).
   Sous 640 px, les colonnes s'empilent : la ligne d'en-têtes est masquée et
   chaque case affiche le nom de sa colonne (`.pc-col`).
   Seuls comptent les arrêts et lignes d'au moins `MIN_OBSERVATIONS`
   passages (sur les deux périodes pour une baisse) ; une carte sans
   candidat le dit (pas de période de comparaison, aucune baisse, pas assez
   de passages). La règle est rappelée dans « Méthode » (`PRIORITY_RULE`).
   Puis la **carte des arrêts**, avec un champ « Chercher un arrêt »
   (`stop_search`, synchronisé sur `stop_id`, `_on_stop_search`) et une
   légende couleur (état), forme (mode) et taille (passages)
   (`map_legend_html`) (`dashboard/carte.py` + `dashboard/carte_arrets.js`,
   composant `st.components.v2` qui pilote deck.gl 9.1 sur un fond MapLibre,
   style vectoriel « Positron » de Carto ; arrêts de `load_territorial`, depuis
   `agg_daily_stop`) :
   - **regroupement selon le zoom** : sous le zoom 14 (`SPLIT_ZOOM`), les quais
     d'un même arrêt (même nom, même mode, à moins de 150 m : `group_stops`,
     loader `grouped_territorial`) forment un seul marqueur, à leur position
     moyenne, portant le quai **le moins fiable** (couleur, fiche ouverte au
     clic) ; à partir du zoom 14, chaque quai a son marqueur, écarté de ses
     voisins dans la direction réelle de sa position tant qu'ils sont à moins
     de 30 px les uns des autres (`MIN_SEP_PX`), puis à sa place exacte ;
   - couleur = palier du score de fiabilité du quai, toutes lignes confondues
     (section 10.3, composantes de chaque ligne et jour à ce quai) ; forme = mode de
     la ligne principale ; taille en mètres (`STOP_SIZE_METERS` : 120 à 240 m
     selon les passages), donc proportionnelle au zoom, bornée entre 8 et 28 px
     (`STOP_SIZE_PIXELS`) ; icônes tirées d'un atlas PNG unique
     (`marker_atlas`, 4 formes × 3 paliers + halo de sélection, dessiné avec
     Pillow ; clé `icon_key` « forme|couleur ») ; les moins fiables sont
     dessinés au-dessus ;
   - **infobulle** (HTML propre au composant) : nom, une ligne par quai
     (pastille de couleur, direction, score, part > 5 min ; le quai survolé
     ou le moins fiable en tête), lignes desservies, passages, « Cliquer pour
     ouvrir la fiche » ;
   - **arrêt sélectionné** : halo et icône agrandie, bandeau « Arrêt
     sélectionné » avec un bouton « Centrer » ; la carte s'y déplace (zoom 15,
     `FOCUS_ZOOM`) quand l'arrêt est choisi ailleurs que sur la carte
     (recherche, « Priorités du moment », lien `?arret=`), pas après un clic sur la
     carte ;
   - **lignes de l'arrêt sélectionné** : tracé réel de chaque ligne qui le
     dessert (`route_shapes`, dans le sens de la ligne à cet arrêt d'après
     `stop_direction`, les deux sens sinon ; `selected_stop_paths`,
     `stop_route_paths`), couleur du palier de la ligne sur un liseré blanc,
     étiquette au nom de la ligne, placée vers une extrémité du tracé (alternance
     85 %, 15 %, 70 %, 30 % de sa longueur) et masquée quand elle en chevauche
     une autre (`CollisionFilterExtension` de deck.gl : elle réapparaît en
     zoomant), infobulle (état et score) ; les autres arrêts
     passent en transparence. Le bandeau ajoute « Voir les lignes entières »,
     qui cadre la carte sur tous les tracés ; une phrase sous la carte les
     énumère avec leur état ;
   - le clic renvoie l'identifiant du quai à Python (`setTriggerValue`,
     callback `_on_map_click`) ; `map_payload` prépare les données (listes
     compactes de quais et de groupes) ; le composant est enregistré à
     l'import de `carte.py` et réenregistré au premier affichage s'il a été
     importé hors du serveur Streamlit.
   **Fiche arrêt** (§11.7) sous la carte, ouverte par un clic sur un arrêt
   de la carte, par la liste « Arrêt » de la barre de contexte, par le bloc
   « Priorités du moment » ou par une ligne du tableau des arrêts
   (`st.dataframe(on_select=…)`) ; ces entrées écrivent la même clé
   `st.session_state["stop_id"]` et font
   défiler la page jusqu'à la fiche (`request_scroll` / `scroll_if_requested` :
   script `scrollIntoView` injecté une fois par `st.html(...,
   unsafe_allow_javascript=True)`, cibles fixes `.fiche-arret`,
   `.fiche-ligne`, `.carte-anchor`). Blocs repliables : **Comparer les
   communes** (périmètre « Réseau complet » ; `load_commune_stats`, graphique
   `commune_ranking_chart` et tableau) et **Tous les arrêts du périmètre**.
2. **Où les bus perdent du temps** (`render_page_contention`, page
   d'information : sans bandeau d'indicateurs, car elle porte sur la période
   scolaire et non sur la période de la barre du haut ; sur cette page, le
   choix « Période » est grisé, « Période scolaire », avec la raison en aide :
   `period_picker(locked=True)`) — carte des points de contention
   (section 10.5). Le choix « Jour » (semaine, samedi, dimanche type) est sur
   la ligne du titre, suivi d'une phrase d'explication ; la carte prend la
   hauteur de l'écran (`fitHeight` : entre 400 et 760 px sur ordinateur, entre
   460 et 720 px sous 640 px de large) pour que la carte, le curseur et la
   légende tiennent dans un écran de 768 px de haut comme sur un téléphone
   (390 px de large : barre de contexte non collante, légende courte, panneau en
   bas de l'écran). Tout le reste se passe dans le navigateur, sans relancer
   Streamlit (`dashboard/carte_troncons.py` + `carte_troncons.js`, deck.gl sur
   fond Positron) :
   - **données** (`troncons_payload`) : toutes les tranches partent en une
     fois, avec pour chaque tronçon son tracé décalé de 7 m à droite du sens de
     circulation (`offset_right`, pour séparer les deux sens d'une rue), une
     chaîne de 25 codes (`cell_code` : 24 heures puis la journée) et les valeurs
     mesurées (temps perdu, part du temps prévu, passages, part des passages
     qui perdent plus de 2 min) ;
   - **curseur horaire** (0 h à 23 h) et bouton « Toute la journée » dans un
     bandeau sous la carte, avec l'heure en grand et la légende en barre ;
     ouverture sur l'heure où le plus de tronçons perdent beaucoup de temps dans
     le périmètre (`worst_slot`) ;
   - **verdict** en haut à gauche, recalculé à chaque cran : « À 17 h,
     17 portions entre deux arrêts ralentissent nettement les bus à Mérignac, et
     20 un peu. », puis le nombre de portions mesurées ; le nom du périmètre est
     écrit au-dessus ;
   - **panneau au clic** sur un tronçon : lignes mesurées qui l'empruntent (avec
     « d'autres lignes y passent mais ne sont pas mesurables » si une ligne au
     temps réel douteux y passe aussi), temps perdu à l'heure choisie, en clair
     par rapport au temps prévu (« soit 40 % de temps en plus que prévu »,
     « plus du double du temps prévu » au-delà de 100 %, « plus du triple »
     au-delà de 200 %), part des passages qui perdent plus de 2 min, « l'un des endroits où le retard des bus se crée », heure où les
     bus y perdent le plus, frise des 24 heures ; pour un tronçon gris, la
     raison : service très peu fréquent, temps réel non fiable sur la ligne,
     début ou fin de ligne, ou aucun passage ;
   - **dessin** : couleurs des trois paliers, gris pour le non mesuré ; bus en
     trait plein, ferry en tirets, tram en gris hachuré (« mesure par tronçon
     indisponible pour le tram ») ; sur tout le réseau, le vert est fin et clair,
     les tronçons qui perdent du temps sont épais, liserés de blanc et dessinés
     par-dessus ;
   - **fil d'Ariane** (`contention_scope`, `in_scope`) : la commune, la ligne et
     l'arrêt de la barre de contexte définissent le périmètre (tronçons dont
     l'un des arrêts est dans la commune, qu'emprunte la ligne, qui touchent
     l'arrêt) ; ils restent tous épais et liserés, vert compris, et c'est le
     reste du réseau qui s'estompe (trait de 1,2 px, transparent) ; le verdict
     dit « à Mérignac », « sur la ligne 23 », « autour de l'arrêt … » ; la carte
     se cadre sur les arrêts du périmètre entre le verdict et le bandeau du bas
     (`fitPadding` mesure leur hauteur). Sur cette page, choisir une ligne
     ou un arrêt dans la barre ne change pas de page.
3. **Lignes** — les 15 lignes les moins fiables (`ranking_chart` affiché par
   `graphique.graphique_cliquable` : l'infobulle se termine par « Cliquer pour
   analyser cette ligne ↓ » et un clic sur une barre ouvre la fiche,
   `_on_ranking_click`) ; en vue détaillée, tableau de toutes les lignes du
   périmètre (`lines_table` : ligne, score, retards > 5 min, arrêts non
   desservis, passages ; une sélection ouvre la fiche) ; puis **fiche ligne**
   de la ligne choisie dans la barre de contexte, le graphique ou le tableau
   (`st.session_state["line_id"]` ; à défaut, la ligne la moins fiable)
   (§11.7), calculée sur toute la ligne quel que soit le filtre « Territoire ».
   Toute ouverture de fiche ligne fait défiler la page jusqu'à ses
   indicateurs.
4. **Quand ?** — deux vues :
   - *Selon le créneau* : verdict (créneau qui se détache, règle de
     concentration de §11.7), **score de fiabilité par créneau**
     (`period_score_chart`), retards > 5 min par mode et créneau
     (`period_mode_chart`), **retards > 5 min selon l'heure** pour l'ensemble
     et les modes choisis (`groups_hourly_chart`, choix « Afficher » :
     `_mode_choice`, `st.pills`) ; en vue détaillée, lignes les moins
     ponctuelles d'un créneau (repliable). Créneaux : Matin 6 h–10 h, Journée
     10 h–16 h, Pointe du soir 16 h–20 h, Soirée & nuit 20 h–6 h (lundi–vendredi)
     et Week-end ; leurs jours et heures sont écrits sous chaque créneau, sur
     l'axe des deux graphiques (`period_category`, `PERIOD_DETAILS`). Les
     arrêts non desservis et les courses supprimées n'ont pas d'heure dans les
     données : le score d'un créneau est sa part à l'heure × le service assuré
     des jours qu'il couvre (semaine ou week-end, `period_day_rates`). Loaders `load_period_stats`,
     `load_period_mode`, `load_period_lines`, `load_hourly_metrics`.
   - *Dans le temps* : la période est comparée à sa période de comparaison
     (le mois précédent pour un mois, voir le sélecteur de période ci-dessous).
     Verdict (score de fiabilité et son évolution), indicateur jour par jour
     au choix (« Indicateur » : score de fiabilité par défaut, ponctualité,
     retards > 5 min, retard moyen en secondes, arrêts non desservis ;
     `TREND_METRICS`), pour l'ensemble et les modes choisis
     (`groups_trend_chart` ; moyenne glissante 7 jours quand seul l'ensemble
     est affiché), lignes qui se dégradent ou s'améliorent
     (`engagement_progression_chart`, détail repliable). Loaders
     `load_daily_metrics` (`group_metrics` : une ligne « Ensemble » et une par
     mode) et `load_engagement_progression`.
5. **Modes de transport** — même contenu dans les deux vues : carte de risque
   des lignes (`scatter_chart`, seuils de `risk_zone`, libellé de chaque zone
   dans son coin : « Retards rares mais longs », « Zone critique », « Retards
   fréquents mais courts », « Risque faible »), verdict (mode le moins
   ponctuel), cartes par mode (glyphe ● ■ ▲, bordure au palier), comparaison
   d'indicateurs ; répartition des écarts, tableau détaillé des lignes et
   tableau par mode (repliables).
6. **Perturbations** — alertes actives à l'instant courant + historique
   (dédupliqué : une même annonce peut être publiée sous plusieurs
   `alert_id`) ; indication explicite que l'alerte n'implique **pas** de
   causalité démontrée avec les statistiques.
7. **Données & méthode** — quatre vues : *Lexique* (`LEXIQUE`, chaque terme a un
   identifiant `lexique_slug` ; un lien `?lexique=<terme>`, venu d'une aide « ? »
   ou d'une infobulle de graphique, ouvre cette vue sur le terme surligné en
   crème et le fait défiler à l'écran, sans afficher le guide ; mots du
   tableau de bord et des rapports en langage courant), *Méthode* (définitions, seuils,
   stabilisation 20 min, jours et lignes écartés, lecture des fiches, arrêts non
   desservis, bloc « Le score de fiabilité de la période » : score ± marge, à
   l'heure ± marge, service assuré, départs en avance, jours exclus ou dégradés
   et lignes écartées de la période et du territoire, loader `load_method_v2`), *Données
   ouvertes* (boutons de téléchargement CSV de la période, loader
   `load_open_dataset`, qui passe par `src/scripts/export_open_data.py` — voir
   §11.6) et *Suivi de la collecte* (observations brutes, passages analysés,
   courses suivies, observations par minute sur 7 jours glissants en
   Highcharts Stock, répartition horaire des 7 derniers jours).

8. **Rapports mensuels** — `render_page_reports` : choix du mois
   (`list_reports(REPORTS_DIR)` parcourt `reports/output/AAAA-MM/` et ne
   retient que les mois qui ont au moins un PDF, du plus récent au plus
   ancien) ; bouton de téléchargement du rapport réseau et, pour une commune
   choisie (présélectionnée sur le territoire de la barre du haut ; dossiers
   nommés par `generate_all_reports.slug`, noms affichés depuis
   `stop_municipalities`), de son rapport. Mention de la licence et de la
   citation attendue.
9. **À propos** — `render_page_about` : objet du projet, indépendance,
   contact, sources et licences, mentions légales et confidentialité
   (section 16).

Sélecteur de période (`period_picker`, liste « Période » de la barre du
haut) : les **mois complets** couverts par les données, du plus récent au
plus ancien (« Septembre 2026 », « Octobre 2026 (jusqu'au 01/10) » pour le mois
en cours), puis « 7 derniers jours », « 30 derniers jours », « Toute la période
collectée » et « Dates précises… » (deux dates). Par défaut : le mois en cours
s'il compte au moins 15 jours de données, sinon le mois précédent
(`default_period_choice`). Chaque choix définit aussi sa **période de
comparaison** (`resolve_period`, objet `Period`) : le mois précédent pour un
mois, les N jours d'avant pour les jours glissants, la même durée juste avant
pour des dates précises, aucune pour toute la période ; aucune non plus si
elle tombe avant le premier jour de données. Cette période sert à toutes les
évolutions : carte « Évolution » des fiches, arrêt et ligne en baisse du bloc
« Priorités du moment », vue *Dans le temps*. Bornes : jours de service, fin exclue
(`load_service_days` donne le premier et le dernier jour disponibles).

**Barre de contexte** (`render_context_bar`, conteneur `ctx_bar`), en haut de
toutes les pages et collée en haut de l'écran au défilement (CSS `sticky` sur
son `stLayoutWrapper`) : fil d'Ariane « ← Revenir » · Commune › Ligne › Arrêt,
puis la période :

- **Commune** (`commune_topbar`, « Tout le réseau » puis les communes de
  `stop_municipalities`) : restreint toutes les pages ; en changer ferme
  l'arrêt ouvert (`_on_ctx_commune`).
- **Ligne** (`ctx_line`, `line_options` : « Toutes les lignes », puis les lignes
  du périmètre, la moins fiable d'abord, avec leur état) : ouvre la fiche ligne
  sur la page « Lignes », quelle que soit la page en cours (`_on_ctx_line`).
- **Arrêt** (`ctx_stop`, `stop_options`) : les arrêts de la ligne choisie dans
  la commune (« Arrêt de la ligne »), sinon ceux de la commune ; libellés
  « nom — direction » identiques au titre de la fiche ; ouvre la fiche arrêt
  sous la carte de « Mon territoire » (`_on_ctx_stop`).
- **← Revenir** (`go_back`) : chaque changement de ce qui est regardé (barre,
  clic sur la carte, tableaux, priorités, boutons des fiches) mémorise
  d'abord la page, la commune, la ligne et l'arrêt affichés (`remember`,
  `_ctx_history`, 20 étapes au plus, `HISTORY_MAX`) ; le bouton restaure la
  dernière étape, son infobulle dit laquelle (`back_label` : « Revenir à la
  ligne 1 »…) et il est grisé quand il n'y a rien avant.

Toutes les listes déroulantes de l'application (`stSelectbox`) ont un cadre
vert foncé à 45 % d'opacité, un chevron agrandi et un cadre plein au survol,
pour qu'on les reconnaisse comme des menus (le thème rendait leur bordure
blanche sur fond blanc).

Les listes des widgets sont resynchronisées à chaque affichage sur
`line_id` / `stop_id` (un widget dont les options changent avec la période
serait sinon remis à zéro par Streamlit).

### 11.4 Graphiques Highcharts (`dashboard/highcharts.py`)

Injection de HTML via `st.components.v1.html` : charge
`highstock.js` (et `highcharts-more.js` pour les bulles) depuis jsDelivr,
version figée 13.1.1 (`HIGHCHARTS_CDN`). `code.highcharts.com` n'est plus
utilisé : son usage en production n'est pas prévu par l'éditeur, et il
refuse les navigateurs sans interface (« Violation of license/fair-usage
policy »), ce qui empêchait aussi les contrôles automatiques de rendu ;
applique `LIGHT_THEME` (fonds blanc, bordures Sunlit Clay, texte Olive Leaf à
70 %), en français (`lang` : virgule décimale, espace fine pour les milliers,
mois et jours en français, dates d'infobulle « lundi 15 septembre 2026 »).
Chaque infobulle nomme ce qu'elle montre et porte son unité (`unit_tooltip` :
« Retards de plus de 5 min : 12,3 % », « 1 234 passages », « 45 s », « / 100 » ;
heures en « De 7 h à 7 h 59 », `HOUR_HEADER`). Chaque graphique désigne un
terme du lexique (clé `lexique` de sa configuration, retirée avant l'envoi à
Highcharts) : l'infobulle se termine par « Définitions dans le lexique ↗ »,
lien absolu vers l'application calculé depuis la page parente, et reste
ouverte quand la souris s'y pose (`stickOnContact`). Fonctions : `ranking_chart`, `scatter_chart`,
`commune_ranking_chart`, `mode_comparison_chart`, `period_score_chart`,
`period_mode_chart`, `groups_trend_chart`, `groups_hourly_chart`, `engagement_progression_chart`,
`delay_distribution_chart`, `collection_minutely_chart` (Stock), et
`hourly_distribution_chart`. Les couleurs par palier sont calculées par
`palette.hex(value, kind)` — cohérentes avec les rapports.

Graphique cliquable (`dashboard/graphique.py`) : les graphiques de
`st.components.v1.html` ne renvoient rien à Python. `graphique_cliquable`
affiche une configuration Highcharts dans un composant `st.components.v2`
(Highcharts chargé depuis `HIGHCHARTS_CDN`, thème `LIGHT_THEME`) ; un clic
sur un point renvoie son `id` (`setTriggerValue("clicked", …)`) au callback
`on_click`. Pied d'infobulle : « Cliquer pour analyser cette ligne ↓ »
(`CLICK_FOOTER`). Utilisé par le classement des lignes (les points de
`ranking_chart` portent `id` = `route_id`). Après modification du JavaScript,
redémarrer Streamlit.

Modes : `scatter_chart` (une série de bulles par mode, `marker.symbol` =
`mode_marker`, couleur de chaque bulle = palier du score, seuils de
`risk_zone` tracés en `plotLines`, zone indiquée dans l'infobulle, libellé
des quatre zones dans les coins : `zone_labels_series`) ;
`mode_comparison_chart` et `period_mode_chart` (colonnes colorées par palier,
glyphe du mode en étiquette de donnée) ; `groups_trend_chart` et
`groups_hourly_chart` (courbe « Ensemble » épaisse en Black Forest, une
courbe par mode avec trait `MODE_DASH` et marqueur propres au mode, points
colorés par palier).

Légendes : quand la légende dessine déjà la forme du mode (bulles, courbes),
le nom de série est le mode seul ; pour les colonnes, dont le symbole de
légende serait toujours un rectangle, le nom porte le glyphe
(`mode_series_name`) et le symbole est masqué (`GLYPH_LEGEND`). La carte de
risque affiche une échelle de taille des bulles (`bubbleLegend` : nombre de
passages analysés de la ligne). Les graphiques à une seule série n'ont pas de
légende, et la couleur par défaut du thème (`LIGHT_THEME["colors"]`) est Black
Forest. Formats de nombres des infobulles : `{point.z:,.0f}` (un format
`{…:,}` sans `f` est traité par Highcharts comme un format de date et
n'affiche rien d'utile).

Accessibilité : HTML rendu avec `<html lang="fr">`, module Highcharts
`accessibility.js` chargé pour les graphiques non-Stock (Stock l'intègre de
série), `accessibility.enabled` forcé et description générée
(`_accessibility_description`) à partir du type et des noms de séries ;
`json.dumps(..., ensure_ascii=False)` pour garder le texte en clair.

Fiches (§11.7) : `line_profile_chart` (retard pris par tronçon en colonnes,
tronçons dominants en Copperwood, retard moyen à l'arrêt en courbe, arrêt
consulté marqué d'un trait vertical, arrêts de la commune sélectionnée sur
fond Cornsilk bordé via `commune_bands`), `slot_profile_chart` (retard moyen à
chaque arrêt sur un créneau jour × heure, comparé au reste du temps en
tirets), `skip_profile_chart` (taux
d'arrêts non desservis arrêt par arrêt, palier `pourcent`), `stop_lines_chart`
(passages > 5 min et arrêts non desservis par ligne, en nombre), `risk_by_label_chart`
(retards > 5 min par créneau ou par jour de la semaine), `daily_status_chart`
(jour par jour, seuil du jour dégradé à 15 % tracé) et `cancellations_chart`
(courses supprimées par jour).

`delay_distribution_chart` colore chaque classe de retard par l'écart absolu
médian de la classe (ex. `+1 à +2` → 90 s → palier « retard ») ; seules les
3 couleurs de palier sont utilisées, pas de dégradés.

### 11.5 Dépendances externes du dashboard

- Highcharts 13.1.1 depuis jsDelivr (JS) — requiert un accès Internet côté
  navigateur. Licence Highcharts : gratuite pour un usage non commercial
  (projet personnel, site d'une association) ; une licence est nécessaire
  avant tout usage commercial (section 23).
- Carte des arrêts : deck.gl 9.1.14 et MapLibre GL 4.7.1 chargés depuis
  jsDelivr par le navigateur, style vectoriel « Positron » de Carto
  (`basemaps.cartocdn.com/gl/positron-gl-style/style.json`). Les tuiles raster
  `light_all` de Carto renvoient désormais une image « API KEY REQUIRED » sans
  clé : elles ne sont pas utilisées. Si les bibliothèques ne se chargent pas,
  la carte affiche un message et la recherche et le tableau des arrêts restent
  utilisables. La carte est suivie d'une légende textuelle (alternative de
  lecture pour lecteurs d'écran).
- Logo local `assets/logo/urban-vision-logo-white.png` (data-URI base64).
- **Aucun appel API ni `os.getenv`** dans `app.py`.

### 11.6 Export open data (`src/scripts/export_open_data.py`)

Lecture seule des tables d'agrégation (jamais la table brute) ; intervalles
demi-ouverts `[since, end)` sur `date_service`. Ecrit dans `data/open_data/`
des **CSV UTF-8 (BOM, séparateur virgule, en-tête stable)** plus un
`METADATA.json` (date de génération, bornes, nombre de lignes, licence
« Licence Ouverte / Open Licence 2.0 (Etalab) » et source « Urban Vision,
d'après les données TBM »).

Quatre datasets :

| Dataset | Fichier | Contenu |
|---|---|---|
| `lignes_journalier` | `lignes-journalier.csv` | Par (date, ligne) : observations, retards moyen/médian (médiane exacte via histogramme), à l'heure (−1 à +5 min), retards > 5 min, en avance, arrêts non desservis, score de fiabilité, histogramme JSON |
| `arrets_journalier` | `arrets-journalier.csv` | Par (date, ligne, arrêt) : + nom, commune, direction, coordonnées |
| `horaire` | `horaire.csv` | Par (date, ligne, heure) : observations, retard moyen, à l'heure, retards > 5 min |
| `communes_journalier` | `communes-journalier.csv` | Par (date, commune) : code Insee, observations, retards, arrêts non desservis, score de fiabilité, nombre de lignes |

Mêmes règles que le dashboard (section 10.3) : jours et lignes écartés de la
période exportée, lignes Flex' exclues ; `METADATA.json` en donne la liste
(`exclusions`) et les définitions (`definitions`).

Commandes :

```bash
.venv/bin/python src/scripts/export_open_data.py --print-datasets
.venv/bin/python src/scripts/export_open_data.py --since 2026-09-10 --until 2026-09-14
# options : --db <chemin> (défaut : DATA_ROOT/urban_vision.db), --out <dossier>
```

Connexion avec `PRAGMA query_only = ON` et une transaction `BEGIN...ROLLBACK`
pour un instantané cohérent pendant que le collecteur écrit. La régénération
périodique est confiée au collecteur (cron côté `ek-hub`, procédure §20) ; le
dashboard fournit les mêmes exports à la demande via `load_open_dataset` pour
la période sélectionnée. La médiane est calculée avec la même règle que le
dashboard (`_median_seconds`), les tables optionnelles (`routes`, `stops`,
`stop_municipalities`, `stop_direction`) sont détectées avant jointure.

### 11.7 Fiches diagnostic (arrêt et ligne)

Objectif : dire **d'où vient** un problème de fiabilité, pas seulement qu'il
existe. La logique est dans `dashboard/diagnostic.py` (fonctions pures, testées
par `tests/test_diagnostic.py`) ; `app.py` charge les données
(`load_stop_daily`, `load_stop_hourly`, `load_route_segments`,
`load_route_hourly_stops`, `load_line_cancellations`, `load_line_stops`,
`segments_available`, `load_method_v2`, tous en cache 60 s) et met en page
(`render_stop_panel`, `render_line_panel`). Les profils de ligne écartent les arrêts desservis par
moins de 5 % des passages de leur direction (`keep_served_stops`) : une
variante de course marginale ajouterait sinon des arrêts intercalés et un faux
terminus. Les
sous-vues passent par `st.segmented_control` : seule la sous-vue affichée est
calculée.

**Détail du score** : sous les cartes de la fiche ligne (vue détaillée), une
ligne de texte (`method_v2_caption`) donne le score ± marge, le service assuré,
la part des départs de plus d'1 min en avance et, pour une ligne fréquente,
l'attente excédentaire (`load_method_v2`, section 10.4). Une ligne écartée de la
période (temps réel douteux) n'est dans aucun classement ; ouverte par un lien
`?ligne=`, sa fiche le dit (`excluded_line_message`). Les cartes « Points perdus :
horaires » et « Points perdus : service non rendu » suivent la décomposition
exacte de la section 10.3 ; « Courses supprimées » lit `agg_daily_trips`, comme
le score.

**Lien direct** : la fiche affichée est reflétée dans l'URL (`?arret=<stop_id>`
ou `?ligne=<route_id>`, sans mention dans la fiche) ; `apply_query_params`
rouvre la fiche au premier affichage d'un lien partagé.

**Type de retard** : sous les indicateurs de la fiche ligne, dans les deux
vues, un bandeau donne la zone de risque de la ligne (`zone_banner_html` :
nom de la zone en grand, explication, retard médian et part > 5 min). La
sous-vue « Quel type ? » de la fiche arrêt l'affiche aussi.

**Fiche arrêt** — en-tête (nom, direction, lignes avec leur glyphe de mode),
boutons vers les **autres quais du même arrêt** (autre sens ou autres lignes
regroupés sur la carte, avec leur score) et « ↑ Revenir à la carte », 5
indicateurs (score comparé au réseau, **évolution** : score de la période
moins celui de la période de comparaison, avec le score de comparaison ;
passages > 5 min, arrêts non desservis, taille de l'échantillon), bloc
« En bref », bloc « Pistes », bouton principal « Ouvrir la fiche de la ligne
… » (ligne responsable ; « ← Revenir » dans la barre de contexte ramène à
l'arrêt), puis 4 sous-vues :

| Sous-vue | Contenu |
|---|---|
| Où ? | passages problématiques par ligne ; profil de la ligne responsable autour de l'arrêt (8 arrêts en amont, 2 en aval) |
| Quand ? | filtre par ligne ; **grille jour de la semaine × heure** (`week_hour_grid_html` : % de passages > 5 min par case, couleur du palier `pourcent`, totaux par jour et par heure, case du moment qui ressort encadrée, détail en infobulle) ; phrase sur le moment qui ressort (`find_peak`) et la ligne la plus touchée à ce moment (`slot_lines`) ; **répercussion** : pour une ligne et un créneau (par défaut celui qui ressort ; listes Jour / Heure), retard moyen à chaque arrêt de la direction comparé au reste du temps (`slot_profile`, 10 arrêts avant, 11 après) et phrase disant d'où vient le surcroît et jusqu'où il se prolonge (`propagation`) ; bande jour par jour (repliable) |
| Quel type ? | bandeau « Type de retard » (`palette.risk_zone`) et rang de l'arrêt parmi ceux du réseau (≥ `MIN_OBSERVATIONS` passages) ; répartition des écarts (repliable) |
| Contexte | alertes TBM des lignes de l'arrêt, avec mention de celles qui recoupent un jour dégradé |

**Fiche ligne** — en-tête (glyphe, terminus, communes desservies, passages par
jour), 5 indicateurs (score comparé au réseau et à la médiane du mode, évolution
par rapport à la période de comparaison, points perdus par les retards, points
perdus par les arrêts non desservis, courses supprimées), « En bref », « Pistes », puis
4 sous-vues : **Retards : où ?**
(profil de **tous les arrêts de la ligne sur le réseau**, par direction ; ouverte depuis une fiche arrêt, la fiche présélectionne la direction où se trouve l'arrêt (`stop_direction_in`, dans les trois sous-vues à choix de direction) et le marque d'un trait vertical sur les profils ; 3
tronçons qui prennent le plus de retard avec leur commune), **Service non
rendu** (courses supprimées par jour, arrêts non desservis le long de la ligne),
**Quand ?** (grille jour × heure, moment qui ressort, puis la phrase « À ce
moment-là, le retard s'aggrave surtout entre … » en encadré au-dessus du
profil du créneau le long de la ligne : `slot_hotspot`), **Contexte**
(alertes, répartition des écarts). Quand une commune est sélectionnée, ses arrêts
sont sur fond Cornsilk dans les profils et « En bref » donne la part du retard
de la ligne prise sur la commune et son tronçon le plus pénalisant
(`commune_share`). En bas, les 10 arrêts les plus touchés (passages > 5 min +
arrêts non desservis) ; une sélection ouvre la fiche arrêt.

Règles (`diagnostic.py`, constantes en tête de module) :

| Règle | Définition |
|---|---|
| Ligne responsable d'un arrêt | la plus grande somme passages > 5 min + arrêts non desservis, **en nombre** |
| Retard local / importé (`locate_cause`) | sur la ligne responsable : part du retard pris sur le tronçon dans (retard importé + retard pris) ; ≥ 50 % → local, < 25 % → importé, sinon mixte ; aucun verdict si le retard moyen à l'arrêt est < 60 s |
| Tronçon amont dominant | le tronçon amont qui prend le plus de retard n'est cité comme origine que s'il pèse au moins 25 % du retard importé (`is_dominant_hotspot`) ; sinon « accumulation progressive » |
| Jour dégradé, récurrence | part des passages > 5 min ≥ 15 % (palier négatif `pourcent`), jours d'au moins 5 passages ; ponctuel < 25 % des jours, fréquent < 50 %, chronique au-delà ; aucun verdict sous 5 jours observés |
| Concentration | créneau (ou jour) dont la part de passages > 5 min atteint 1,5 fois celle du reste et au moins 5 % |
| Répartition des points perdus | points perdus par le service non rendu = 100 − service assuré, par les horaires non tenus = service assuré − score (section 10.3) |
| Origine du retard d'une ligne | par direction : « départ » si le retard au premier arrêt atteint 50 % du maximum atteint, « localisé » si les 3 tronçons qui prennent le plus de retard en concentrent ≥ 50 %, sinon « diffus » ; aucun verdict si le maximum reste < 60 s |
| Arrêts non desservis | « extrémités » (≥ 60 % des sauts sur les 15 % premiers ou derniers arrêts), « bloc » (≥ 60 % sur une suite d'arrêts consécutifs à taux double de la moyenne), sinon « dispersé » ; rien sous 0,5 % |
| Déséquilibre de direction | une direction porte ≥ 65 % des passages > 5 min de la ligne |
| Moment de la semaine (`find_peak`) | case jour × heure d'au moins 10 passages et 3 par occurrence en moyenne, part > 5 min dans le palier négatif et au moins 1,5 fois celle du reste de la semaine ; parmi ces cases, celle qui cumule le plus de passages > 5 min ; « récurrent » si l'heure a été dégradée (≥ 3 passages, ≥ 15 % > 5 min) au moins une fois sur deux sur au moins 3 occurrences, « ponctuel » sinon, « à confirmer » sous 3 occurrences ; « période courte » si aucune case n'a 3 occurrences. Un pic récurrent remplace, dans « En bref », la phrase de concentration par créneau |
| Répercussion d'un créneau (`propagation`) | retard moyen de chaque arrêt de la direction sur le créneau (au moins 2 passages), comparé au reste du temps ; autour de l'arrêt, on suit les arrêts voisins tant que leur surcroît reste ≥ 50 % de celui de l'arrêt et ≥ 60 s ; aucun verdict sous 60 s de surcroît à l'arrêt |
| Aggravation sur un créneau (`slot_hotspot`) | tronçon où le surcroît de retard augmente le plus d'un arrêt au suivant, s'il augmente d'au moins 30 s |
| Part de la commune (`commune_share`) | somme des retards positifs pris sur les tronçons qui mènent aux arrêts de la commune, rapportée à celle de toute la ligne |

Les phrases présentent des **indices** et des **pistes** (« à confirmer sur le
terrain ») avec leur interlocuteur naturel (commune ou Bordeaux Métropole pour
la voirie, exploitant pour la régulation et les moyens), jamais une causalité
établie.

---

## 12. Rapports mensuels

### 12.1 Vue d'ensemble

Trois scripts dans `reports/` :

| Script | Rôle |
|---|---|
| `generate_single_report.py` | Un rapport (réseau **ou** une commune) |
| `generate_all_reports.py` | Tout : réseau + chaque commune, un dossier par rapport, génère `compile_all.sh` si `--compile` ; `--previous-month` (mois précédant la date du jour) et `--pdf-only` (ne garde que les PDF) servent à la génération automatique |
| `generate_monthly_report.py` | **Moteur** — à ne pas appeler directement (mais rien ne l'interdit) |

`--compile` compile en PDF via **xelatex** (deux passages, `-interaction=nonstopmode
-halt-on-error`) puis nettoie `.aux`/`.log`. Sans `--compile`, seul le `.tex`
est écrit. Nom de sortie :
`urban-vision-<AAAA-MM>-<slug du destinataire>.{tex,pdf}`.

Publication : les PDF de `reports/output/` sont téléchargeables dans le
dashboard (page « Rapports mensuels », section 11.3). La ligne « Source » de
chaque rapport mentionne la Licence Ouverte 2.0 des données TBM et la
citation attendue en cas de réutilisation (« Urban Vision, d'après les
données TBM »).

### 12.2 Commande d'exemple

```bash
# Réseau complet
.venv/bin/python reports/generate_single_report.py --month 2026-08 --network --compile

# Commune
.venv/bin/python reports/generate_single_report.py --month 2026-08 --commune "Mérignac" --compile

# Tout (réseau + 28 communes), avec compilation des PDF
.venv/bin/python reports/generate_all_reports.py --month 2026-08 --compile

# Recompiler manuellement les .tex d'un batch déjà généré
bash reports/output/2026-08/compile_all.sh

# Génération automatique du 1er du mois (timer urban-vision-rapports, section 14)
.venv/bin/python reports/generate_all_reports.py --previous-month --compile --pdf-only
```

Structure de sortie :

```
reports/output/<AAAA-MM>/
├── compile_all.sh
├── reseau/bordeaux-metropole/urban-vision-<mois>-bordeaux-metropole-et-tbm.tex|pdf
├── communes/<slug-de-la-commune>/urban-vision-<mois>-mairie-de-<ville>.tex|pdf
└── (PNG des graphiques : reliability, risk_scatter, stops, evolution, hourly, distribution)
```

Avec `--pdf-only` (après une compilation réussie de tous les rapports), chaque
dossier ne garde que son PDF et `compile_all.sh` est supprimé :
`<AAAA-MM>/reseau/bordeaux-metropole/<pdf>` et
`<AAAA-MM>/communes/<slug>/<pdf>`. Si une compilation échoue, tous les fichiers
intermédiaires sont conservés pour le diagnostic.

### 12.3 Moteur (`generate_monthly_report.py` : 1439 lignes)

- **Périmètre** : `Scope(recipient, routes, communes, description)`. En CLI :
  `--recipient`, `--routes` (séparées par virgules), `--communes`, `--profile`
  + `--recipients-file`. Validation des noms de communes contre
  `stop_municipalities` (sinon erreur explicite).
- **Mois** : `--month AAAA-MM` ou auto := dernier mois présent dans
  `observations` (max `departure_time`).
- **Interrogations** (`query_*`) : observations SCHEDULED/SKIPPED du mois (avec
  filtre lignes/communes, seuil de stabilisation de 20 min et exclusions du mois,
  `month_exclusions` : jour du départ pour les passages, jour de la course pour
  les arrêts non desservis), stats par arrêt
  (uniquement si ≥ `MIN_PASSAGES_FOR_RANKING` passages, sinon tout le périmètre),
  évolution mensuelle (score de fiabilité et ponctualité de chaque mois, avec
  les exclusions de ce mois, lue dans `agg_daily`, ou `agg_daily_stop` pour une
  commune ; le graphique trace le score), scores (`query_scores` : score du
  périmètre ± marge et de chaque ligne, par `indicateurs`), trous de collecte, alertes ServiceAlerts actives sur la
  période pour les lignes du périmètre. **Les lignes à la demande** (Flex',
  Flex'Night) ne sont **pas traitées** dans ces requêtes ni dans les classements
  ni dans le graphique « Arrêts les plus problématiques ».
- **KPIs** : `kpis(scheduled, skipped, fiabilité)` calcule passages, part à
  l'heure (−1 à +5 min, `on_time_share`), retard moyen/médian, > 5 min, arrêts
  non desservis + taux ; la **fiabilité** vient de `query_scores`.
  `make_line_stats(scheduled, skipped, scores des lignes)` fait de même par
  ligne (une ligne sans score n'est pas classée). Les nombres sont écrits à la
  française (`number`, `pct`, `fr` : virgule décimale). `comparison()` calcule la variation
  vs mois précédent. Les rapports communaux comparent **aussi** la ligne au
  réseau (valeurs en olive « Réseau : … »).
- **Synthèse exécutive** : score de fiabilité et son état (fiable, à surveiller,
  problématique : `palette.tier_label`), part à l'heure, complété si
  `skip_rate > 5 %`, puis la ligne la moins fiable.
- **Sections du PDF** : couverture (logo, rapport, date, périmètre),
  synthèse exécutive (6 KPI colorés + évolution), alertes prioritaires (top 3
  des lignes les plus en difficulté ; le glyphe ⚠ n'apparaît que pour les
  lignes ayant un ServiceAlert TBM actif sur la période — cohérent avec
  l'annexe et le tableau de bord), annexe
  résultats détaillés (longtable par ligne, triée par score croissant — les
  plus prioritaires en premier), annexe graphique (5 graphiques matplotlib Antialias ;
  l'arrêté « Arrêts les plus problématiques » oppose retard moyen et retard médian,
  deux barres par arrêt avec légende),
  profil opérationnel (risque horaire, distribution), Infos trafic (page dédiée
  des ServiceAlerts, dédoublonnées par contenu :
  route × titre × période), méthode (formule, exemple, marge ± 60 s, trous de
  collecte, non-interférence des alertes travaux), page « Détail du score de
  fiabilité » entre la méthode et les Infos trafic (les renvois
  `\pageref{v2page}` n'apparaissent que si elle existe).
- **Détail du score** : `query_method_v2` calcule, avec
  `src/scripts/indicateurs.py` (section 10.4) et sur le même périmètre
  (lignes, communes), les indicateurs du mois, la comparaison au mois
  précédent par type de jour (semaine, samedi, dimanche), les indicateurs
  par ligne et la régularité ; `None` si les tables 2.0 manquent ou sont
  vides, et la page n'est alors pas produite. `method_v2_section` rédige la
  page : 3 KPI (score ± marge, à l'heure ± marge, service
  assuré), une phrase de lecture, le tableau par type de jour (écart ± marge,
  « baisse significative », « hausse significative » ou « dans la marge
  d'incertitude »), la régularité des 8 lignes fréquentes (au moins un jour
  sur deux) à l'attente excédentaire la plus forte, les 8 lignes les moins
  fiables avec marge et décomposition (lignes au temps réel douteux exclues), la qualité des données
  (jours exclus, jours dégradés, lignes écartées) et les définitions.
  `method_v2_summary` ajoute la marge du score à la synthèse exécutive,
  avec le renvoi à cette page. Le moteur importe `indicateurs` et `db` par
  `sys.path` (`src/scripts/`) ; l'import de `db` applique `init_db` à
  `data/urban_vision.db`, comme pour les autres scripts.
- **Performance** : le mois est borné par `month_bounds()` (minuit local du 1er
  au 1er du mois suivant) sur `departure_time` (ou `last_seen_at` quand
  `departure_time` est nul, `MONTH_SQL`), ce qui passe par
  `idx_observations_departure_time` ; `+o.schedule_relationship` empêche
  l'usage d'un index commençant par `schedule_relationship`. Le seuil de
  stabilisation exclut les lignes
  vues dans les 20 dernières minutes via `idx_observations_last_seen_at`
  (`RECENT_ROWS_SQL`, `rowid NOT IN …`) au lieu de lire `last_seen_at` ligne à
  ligne. Résultats identiques à l'ancienne formulation (`strftime(…
  'localtime')` évalué sur toute la table) ; requêtes d'un rapport de commune
  sur la VM : plus de 6 min → ≈ 1 à 3 min.
- **Trous de collecte (méthode)** : seules les interruptions d'au moins
  10 min (`SIGNIFICANT_GAP_SECONDS`) sont présentées comme pouvant faire perdre
  des passages ; les plus courtes sont comptées à part, comme rattrapées par
  le relevé suivant. Les durées sont rognées aux bornes du mois local. La ligne
  rappelle aussi le nombre de passages analysés sur les observations brutes,
  sans les qualifier d'« exclues » (`gap_methodology_line`).
- **Rapport « sans données »** : `build_no_data_latex` produit un document
  court et transparent si aucun passage exploitable.
- **Sécurité** : `latex()` échappe tout texte externe (XSS/LaTeX) — correction
  récente (commit `13cf796`).
- Formatage : `number()` (séparateur de milliers `\,`, virgule décimale),
  `duration()` (+2 min 05 s), `pct()`.

### 12.4 `--profile` (destinataires personnalisés)

```bash
cp reports/recipients.example.json reports/recipients.json
.venv/bin/python reports/generate_single_report.py \
    --month 2026-08 --profile mairie_merignac --compile
```

Le contexte « territorial » nécessite que `stop_municipalities` soit rempli
(`assign_stop_municipalities.py`) ; sinon message d'erreur explicite.

### 12.5 Prérequis système pour la compilation

`xelatex` + `fonts-inter` (+ `texlive-lang-french`, `texlive-xetex`…), et
`matplotlib` (dans `requirements.txt`). Sans LaTeX installé, les `.tex` sont générés et une erreur explicite est levée à la
compilation : `xelatex/lualatex introuvable…` (`compile_pdf`).

---

## 13. Référence des scripts et commandes

| Script | Usage | Arguments principaux |
|---|---|---|
| `src/scripts/collect.py` | Collecte TripUpdates (continu) | *aucun* ; constantes en tête de fichier |
| `src/scripts/collect_alerts.py` | Collecte ServiceAlerts (continu) | *aucun* |
| `src/scripts/gtfs_static.py` | Charge `routes`/`stops` | *aucun* ; URL constante |
| `src/scripts/analyze.py` | Bilan quotidien → `daily_line_stats` | *aucun* |
| `src/scripts/assign_stop_municipalities.py` | Rattache arrêts ↔ communes | `--db-path`, `--boundaries-file`, `--boundaries-url`, `--no-reverse-fallback`, `--unassigned-csv` |
| `src/scripts/db.py` | Schéma + agrégats (auto-porteur) | *aucun* (l'import suffit) |
| `src/scripts/export_open_data.py` | Export CSV open data (30 derniers jours) | `--db`, `--out`, `--since`, `--until`, `--print-datasets` |
| `src/scripts/veille_collecte.py` | Veille de la collecte, alertes email (timer 5 min) | `--dry-run`, `--test-email`, `--db`, `--log-dir`, `--state` |
| `src/scripts/rafraichir_agregats.py` | Recalcul des agrégats d'hier et d'aujourd'hui (timer 5 min) | `--db`, `--log` |
| `src/scripts/sauvegarde.py` | Sauvegarde quotidienne, contrôle, rotation, copie hors VM | `--db`, `--dest`, `--verifier [archive]`, `--sans-envoi` |
| `src/scripts/archive_gtfs.py` | Archive du GTFS statique quand il change (timer quotidien), puis recharge les tracés | `--dest`, `--db` |
| `src/scripts/route_shapes.py` | Tracés des lignes depuis un GTFS archivé (`route_shapes`) | `--zip`, `--db` |
| `src/scripts/troncons.py` | Carte des points de contention (timer nocturne, section 10.5) | `--db`, `--log`, `--depuis` |
| `src/scripts/analyse_troncons.py` | Faisabilité de la carte des points de contention : temps perdu par tronçon entre deux arrêts consécutifs, bruit, seuils (lecture seule, sorties dans `data/analyses/troncons/`) | `--db`, `--lignes`, `--temoin`, `--depuis`, `--jusqu-a`, `--seuils`, `--reseau-jours`, `--sortie` |
| `deploy/deployer.sh` | Déploiement sur la VM (section 20.2) | `--sans-pull`, `--sans-redemarrage` |
| `src/scripts/veille_visiteurs.py` | Veille des visiteurs humains (cron 5 min) | `--logs-dir`, `--state`, `--html`, `--since`, `--no-lookup` |
| `dashboard/app.py` | Dashboard Streamlit | `streamlit run dashboard/app.py` |
| `reports/generate_single_report.py` | Rapport unique | `--month` (obligatoire), `--commune` XOR `--network`, `--db-path`, `--output-dir`, `--compile` |
| `reports/generate_all_reports.py` | Tous les rapports | `--month` ou `--previous-month` (l'un des deux), `--db-path`, `--output-dir`, `--compile`, `--pdf-only`, `--communes …` |
| `reports/generate_monthly_report.py` | Moteur de rapport | `--month`, `--db-path`, `--output-dir`, `--recipient`, `--routes`, `--communes`, `--profile`, `--recipients-file`, `--compile` |

Détails de `assign_stop_municipalities.py` :

- Source des contours par défaut : opendata.bordeaux-metropole.fr
  (`fv_commu_s`, 28 communes).
- Algorithme pur Python (ray-casting + trous, bbox préfiltre) — pas de
  bibliothèque géospatiale : `point_on_segment`, `point_in_ring`,
  `point_in_polygon`, `geometry_contains`, `bounding_box` (couverts par
  `tests/test_geometry.py`).
- Arrêt hors contours et `--no-reverse-fallback` absent → API Adresse
  (`api-adresse.data.gouv.fr/reverse/`), délai 0,05 s entre appels ; code
  INSEE préféré à l'orthographe officielle de la Métropole quand il correspond.
- Export CSV des non rattachés : `reports/output/stops_outside_bordeaux_metropole.csv`.

---

## 14. Services système (systemd)

Depuis le 01/10/2026, les unités sont **versionnées dans `deploy/systemd/`**
(copie exacte de la VM pour celles qui existaient) et installées dans
`/etc/systemd/system/` par `deploy/deployer.sh` (section 20.2).

| Unité | Type | Rôle | Planification |
|---|---|---|---|
| `urban-vision-collect.service` | service continu | collecte TripUpdates (section 9.1) | permanent |
| `urban-vision-collect-alerts.service` | service continu | collecte ServiceAlerts (section 9.2) | permanent |
| `urban-vision-dashboard.service` | service continu | Streamlit sur `127.0.0.1:8501` (sections 11 et 15) | permanent |
| `urban-vision-rafraichir.service` + `.timer` | ponctuel | recalcul des agrégats (section 10.2) | toutes les 5 min (minutes 2, 7, 12…) |
| `urban-vision-veille-collecte.service` + `.timer` | ponctuel | veille et alertes email (section 9.4) | toutes les 5 min |
| `urban-vision-sauvegarde.service` + `.timer` | ponctuel | sauvegarde de la base (section 19) | chaque nuit à 2 h 30 |
| `urban-vision-archive-gtfs.service` + `.timer` | ponctuel | archive du GTFS statique (section 9.5) | chaque jour à 6 h 15 |
| `urban-vision-rapports.service` + `.timer` | ponctuel | rapports du mois précédent (section 12) | le 1er du mois à 3 h |
| `urban-vision-troncons.service` + `.timer` | ponctuel | carte des points de contention (section 10.5) | chaque nuit à 3 h 40 |

Réglages communs : `User=ubuntu`, `WorkingDirectory=/home/ubuntu/Urban-Vision`,
`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=full`,
`ProtectKernelTunables=true`, `ProtectKernelModules=true`,
`ProtectControlGroups=true`, `RestrictRealtime=true`,
`RestrictAddressFamilies=AF_INET AF_INET6` (la veille ajoute `AF_UNIX` pour
interroger systemd ; l'unité des rapports n'a pas de restriction réseau). Les
services continus redémarrent seuls (`Restart=always`, `RestartSec=10`). Les
tâches lourdes tournent en basse priorité : recalcul `Nice=5`, sauvegarde et
archive `Nice=10` (sauvegarde en E/S `idle`), rapports et tronçons `Nice=19` et E/S
`idle` (tronçons : `MemoryHigh=900M`, la mémoire est freinée au-delà plutôt que d'être prise à la collecte). Secrets : la veille charge `/etc/urban-vision/alertes.env`
(section 6.5), la sauvegarde `/etc/urban-vision/sauvegarde.env` s'il existe
(section 6.6).

Commandes usuelles :

```bash
systemctl list-timers 'urban-vision-*'
sudo systemctl status urban-vision-collect.service urban-vision-collect-alerts.service urban-vision-dashboard.service
journalctl -u urban-vision-rafraichir.service -n 20
sudo systemctl start --no-block urban-vision-rapports.service   # rapports du mois précédent, à la demande
sudo systemctl start urban-vision-sauvegarde.service            # sauvegarde immédiate
```

> **I2 — `data/dashboard.log`** : ce log local montrait
> `Uvicorn server started on 0.0.0.0:8501`. C'était un reliquat d'une exécution
> **locale de dev** ; sur la VM, l'unité force `--server.address=127.0.0.1` et
> Streamlit journalise vers **journald** (pas de `data/dashboard.log` en prod,
> cf. section 17).

---

## 15. Nginx et HTTPS

Chaîne en production :
**`https://urban-vision.duckdns.org` → nginx :443 → `127.0.0.1:8501`** (dashboard Streamlit).

- **Vhost** : `/etc/nginx/sites-available/urban-vision` (lié dans
  `sites-enabled/`), `server_name urban-vision.duckdns.org` :
  - `listen 443 ssl` — certificat Let's Encrypt
    `/etc/letsencrypt/live/urban-vision.duckdns.org/fullchain.pem`,
    `ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem`, options standards
    (`options-ssl-nginx.conf`) ;
  - `location /` : `proxy_pass http://127.0.0.1:8501` avec les headers
    WebSocket requis par Streamlit (`Upgrade` / `Connection: upgrade`) ;
  - bloc `:80` : `return 301 https://$host$request_uri` si
    `$host == urban-vision.duckdns.org`, sinon `return 404`.
- **Certificat** : Let's Encrypt, émis pour `urban-vision.duckdns.org`
  (RSA, **expiration le 11/12/2026**, valide 87 j au 14/09/2026).
  Renouvellement par le timer systemd **`certbot.timer`** (quotidien) — pas de
  cron dédié.
- **En-têtes de sécurité** (depuis le 01/10/2026, fichier versionné
  `deploy/nginx/urban-vision`) : `Strict-Transport-Security: max-age=31536000`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
  `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy: geolocation=(), microphone=(), camera=()`. Pas de
  politique CSP : Streamlit et les graphiques chargent des scripts en ligne et
  depuis des CDN. Le déploiement garde une copie datée de l'ancien vhost et la
  remet en place si `nginx -t` refuse le nouveau.
- **`default` vhost** : le vhost de stock nginx sur :80 (page par défaut) —
  ne sert pas l'application.
- **Firewall** : le port **8501 est fermé** (cf. section 16) ; le dashboard
  n'est joignable que via nginx.
- **DuckDNS** : le nom est géré sur duckdns.org (enregistrement A vers l'IP
  de la VM). **Aucun mécanisme de mise à jour dynamique n'a été trouvé sur la
  VM** (pas de cron, timer, unité ou script DuckDNS) — cohérent avec une IP
  publique statique ; le token DuckDNS n'est pas présent sur la machine.

**Passer à un nom de domaine propre** (par exemple `urban-vision.fr`) :

1. chez le registraire, créer un enregistrement A (et AAAA si besoin) du
   domaine et de `www` vers l'IP publique de la VM, puis attendre sa
   propagation (`dig +short urban-vision.fr`) ;
2. dans `deploy/nginx/urban-vision`, ajouter le nouveau nom au `server_name`
   des deux blocs, déployer (`deploy/deployer.sh`, qui teste `nginx -t`),
   puis émettre le certificat :
   `sudo certbot --nginx -d urban-vision.fr -d www.urban-vision.fr`
   (`certbot.timer` le renouvellera) ;
3. faire de l'ancien nom une redirection : un bloc `server` dédié à
   `urban-vision.duckdns.org` qui répond
   `return 301 https://urban-vision.fr$request_uri;` (en gardant son
   certificat, renouvelé par certbot), pour que les liens déjà diffusés
   fonctionnent ;
4. mettre à jour l'adresse dans `deploy/deployer.sh` (contrôle HTTPS), la
   page « À propos » (`render_page_about`), le README et ce document.

---

## 16. Réseau et sécurité

État durci appliqué le 12/09/2026 (source : ancien README racine) :

- **SSH** : authentification **par clé uniquement** (clé `~/.ssh/oracle-ek-hub.key`,
  hôte `ubuntu@88.96.51.44`, alias **`ek-hub`** défini dans `~/.ssh/config`),
  mot de passe système désactivé. `fail2ban` : 5 échecs / 10 min → ban 1 h.
- **Firewall** : iptables persistés (`netfilter-persistent`), seuls les ports
  **22/80/443** sont ouverts. `rpcbind` (port 111) désactivé. `8501` fermé.
- **Sudo** : le compte `ubuntu` dispose de `NOPASSWD:ALL`
  (`/etc/sudoers.d/90-cloud-init-users`, `/etc/sudoers`) — comportement OCI
  d'origine ; le compte n'a pas de mot de passe (verrouillé). Ce réglage est un
  choix de confort ; les fichiers concernés sont explicites si un durcissement
  ultérieur est voulu.
- **Services** : voir section 14 — les 3 unités systemd et nginx sont
  **`active`**.
- **Secrets** : `/etc/urban-vision/bdc.key` (clé BigDataCloud, `root:600`,
  section 17.2), `/etc/urban-vision/alertes.env` (identifiants SMTP et adresse
  de sonde de la veille, `root:600`, section 6.5) et, facultatif,
  `/etc/urban-vision/sauvegarde.env` (adresse d'écriture du stockage objet,
  section 6.6) ; aucun secret dans git ni dans les crontabs. `.env` est
  gitignoré et absent de prod.
- **Dépannage hors-bande** : console OCI de l'instance (`ek-hub` →
  Console connection) en cas de blocage réseau/système.
- Exposition : le dashboard étant public, les données qu'il affiche (retards,
  alertes) sont considérées publiques ; il n'y a ni authentification ni clé
  d'API applicative.
- **Données personnelles des visiteurs** (décrites sur la page « À propos ») :
  journaux nginx avec l'adresse IP complète, conservés 14 jours
  (`/etc/logrotate.d/nginx` : `daily`, `rotate 14`) ; rapport GoAccess avec
  adresses anonymisées (`--anonymize-ip`, §17.1) ; veille des visiteurs sur
  adresses tronquées, conservées 30 jours, géolocalisées en HTTPS (§17.2) ;
  télémétrie Streamlit désactivée (`gatherUsageStats = false`) ; cookie
  technique `_streamlit_xsrf` (protection XSRF) et cookie `uv_guide_vu` (guide
  de lecture déjà vu, un an) ; le navigateur charge des
  ressources sur `cdn.jsdelivr.net` et `basemaps.cartocdn.com`. Serveur dans la
  région OCI de Paris (`eu-paris-1`).

---

## 17. Logs

| Fichier | Écrit par | Contenu typique |
|---|---|---|
| `data/collect.log` | `collect.py` et `rafraichir_agregats.py` (FileHandler) | `Démarrage de la collecte Urban Vision (intervalle: 60s)`, `OK - <n> entités, <n> observations mises à jour`, `Agrégats rafraîchis en <x> s`, warnings « Trou de collecte » / « Échec de récupération » / « Rafraîchissement des agrégats lent » / « Refresh des agrégats échoué » |
| `data/alerts.log` | `collect_alerts.py` | `OK - <n> entites, <n> alertes mises a jour`, erreurs éventuelles + traceback |
| journald `urban-vision-veille-collecte` | `veille_collecte.py` | une ligne par condition (`ok` / `ALERTE`), email envoyé, erreurs SMTP |
| journald `urban-vision-sauvegarde`, `-archive-gtfs`, `-rafraichir`, `-rapports` | tâches planifiées | résumé de chaque exécution (taille, durée, contrôle, version archivée…) |
| `data/dashboard.log` | Streamlit/Uvicorn — **dev local uniquement** | démarrage serveur, warnings Streamlit ; en production Streamlit journalise vers **journald** (pas de fichier) |

Observation : `data/collect.log` et `data/alerts.log` (gitignorés) contiennent
des **tracebacks historiques** issus d'exécutions et de versions antérieures du
code — l'actuel `collect.py` logge « Urban Vision » (ligne 153).

Lecture en direct :

```bash
tail -f data/collect.log
```

Rotation (depuis le 01/10/2026, `deploy/logrotate/urban-vision` installé dans
`/etc/logrotate.d/`) : `collect.log` et `alerts.log` tournent chaque semaine,
8 semaines conservées, compressées, en `copytruncate` (les processus gardent
leur fichier ouvert). La veille lit l'état de la collecte dans
`collection_runs`, pas dans ces fichiers : une rotation ne déclenche pas de
fausse alerte. Volumes en production au 01/10/2026 : `collect.log` ≈ 8,5 Mo et
`alerts.log` ≈ 6,9 Mo.

### 17.1 Dashboards GoAccess des connexions nginx

Le trafic HTTP/HTTPS est analysé avec **GoAccess** (`--ignore-crawlers` pour
n'exclure que les bots — les visites « humaines » uniquement ;
`--anonymize-ip` pour ne faire figurer dans le rapport que des adresses
tronquées, depuis le 02/10/2026). Le HTML généré
est placé dans `reports/analytics/` (gitignoré) :

```bash
sudo goaccess --log-format=COMBINED --ignore-crawlers --anonymize-ip /var/log/nginx/access.log \
  -o /home/ubuntu/Urban-Vision/reports/analytics/visiteurs.html
sudo chown -R ubuntu:ubuntu /home/ubuntu/Urban-Vision/reports/analytics
```

Rafraîchissement automatique toutes les 5 min (cron root) :

```bash
sudo crontab deploy/cron/root.crontab   # ligne : */5 * * * * goaccess --log-format=COMBINED --ignore-crawlers --anonymize-ip \
# /var/log/nginx/access.log -o /home/ubuntu/Urban-Vision/reports/analytics/visiteurs.html
```

Sans ce cron, le fichier HTML est un **instantané** : il reflet l'état des logs
à l'instant de la dernière génération (le mode « temps réel » de GoAccess impose
un serveur WebSocket, non déployé).

GoAccess n'expose **pas** l'horodatage exact des connexions par IP (le panneau
« Hosts » agrège hits/visiteurs/volume, « Visit hours » répartit par heure) :
pour répondre « qui s'est connecté, et à quelle heure », on parse le log brut
(vision dédiée, §17.2).

### 17.2 Veille des visiteurs humains — dernières connexions

Le script `src/scripts/veille_visiteurs.py` (stdlib uniquement) détecte les
**visites humaines** et conserve pour chaque adresse tronquée la première et
la dernière connexion.

**Minimisation (depuis le 02/10/2026)** : l'adresse IP est tronquée dès la
lecture du journal (`anonymize_ip` : dernier octet à zéro en IPv4, préfixe
/48 en IPv6) ; aucune adresse complète n'est enregistrée, envoyée à un tiers
ni affichée. À chaque passage, `minimize` tronque aussi les adresses d'un
état plus ancien (en fusionnant les fiches d'un même réseau : `merge_records`)
et oublie les visiteurs absents depuis `RETENTION_DAYS` = 30 jours. Il s'applique aux logs nginx complets (`access.log*`, gzip inclus) :

- **Filtre d'entrée** : requêtes `GET/POST/HEAD` avec statut `200/101/206/304`,
  User-Agent « navigateur » (Chrome/Firefox/Safari/Edge/OPR avec numéro de
  version) et hors liste de bots (Googlebot, Odin, libredtail, Censys,
  l9scan/leakix, Infrawatch, InternetMeasurement, zgrab, …).
- **Traitement incrémental** : l'état est conservé dans
  `reports/analytics/veille_state.json` (gitignoré) ; le script ne ré-examine
  que les lignes postérieures au dernier horodatage traité.
- **Géolocalisation** : pour toute adresse tronquée jamais vue, appel HTTPS de
  `ipwho.is` (gratuit, sans clé, usage non commercial ; `GEO_URL`, champs
  pays, région, ville, coordonnées, code postal, fournisseur et AS, convertis
  par `parse_geo` au format de l'état) — hors IPv6. Au plus
  `GEO_MAX_PER_RUN` = 50 appels par passage (≈ 25 nouveaux réseaux par jour
  en production fin septembre 2026), ré-essai après 1 h en cas d'échec.
  `ip-api.com`, utilisé jusqu'au 01/10/2026, n'acceptait que le HTTP en
  version gratuite.
- **Précision affinée (région NA)** : le script lit la clé `BigDataCloud` via
  `bdc_key()` — d'abord la variable d'environnement `BDC_API_KEY`, sinon le
  fichier `BDC_API_KEY_FILE` (défaut `/etc/urban-vision/bdc.key`). Cette passe
  affine **uniquement les IP françaises** déjà géolocalisées : `localityName`
   (sous-localité), `postcode`, coordonnées et niveau de confiance (`geo.bdc`
   dans l'état). Les IP « hébergeur / cloud (probable bot) » (profil `p-bot`,
   ISP/organisation dans `CLOUDS`) sont **exclues** de cette passe.
   Re-sollicitation au plus 1 fois/7 jours par IP ; en cas
   d'échec transitoire (rate-limit 403 du palier gratuit) l'IP est **ré-essayée
   après 2 h**, avec 0,5 s de pause entre requêtes ; sans clé, la passe est
   ignorée sans erreur.
- **Sorties** (dans `reports/analytics/`, gitignoré) : `veille_state.json`
  (données brutes, tous pays) et `visiteurs_reels.html` (page auto-raffraîchie
  toutes les 5 min). Une **bannière en tête** affiche le dernier visiteur
  **français** toutes régions confondues (`countryCode == "FR"`), même hors
  Nouvelle-Aquitaine. Le HTML ne liste ensuite en table principale que les
  visites **françaises de Nouvelle-Aquitaine** (détection région : `countryCode == "FR"`
  et `regionName` contenant « AQUITAINE ») — et place le reste (autres régions
  France, hors France ou non géolocalisé) dans des sections dépliables. Chaque
  ligne porte une couleur de fond + barre latérale associables : **violet** =
  réseau de l'utilisateur (`SELF_IPS`, actuellement `90.120.193.0`), vert =
  résidentiel/entreprise, orange = hébergeur/cloud probable, gris = hors
  France ou non géolocalisé. La colonne « Ville » affiche la **localité
  affinée** BigDataCloud quand elle existe, suivie du code postal, avec la
  commune ipwho.is en indicatif gris (`≈ Le Bouscat`) si elle diffère. Badge
  « aujourd'hui » sur les visiteurs actifs le jour même.
- **Carte des connexions** : une section dépliable (ouverte) affiche une carte
  Leaflet avec des tuiles **CARTO basemaps** (données OpenStreetMap,
  attribution incluse) et un point coloré par IP de Nouvelle-Aquitaine
  (infobulle : commune, code postal, localité affinée, IP, ISP, plage de
  connexion et nombre de requêtes). Coordonnées prises dans `geo.bdc`
  (BigDataCloud) puis `geo` (ipwho.is) si le champ affiné est absent.

Déploiement en production (VM `ek-hub`) :

```bash
sudo install -o ubuntu -g ubuntu -m 755 \
  src/scripts/veille_visiteurs.py \
  /home/ubuntu/Urban-Vision/src/scripts/
# crontab root (deploy/cron/root.crontab, aucun secret) :
# */5 * * * * /usr/bin/python3 \
#   /home/ubuntu/Urban-Vision/src/scripts/veille_visiteurs.py \
#   && chown ubuntu:ubuntu /home/ubuntu/Urban-Vision/reports/analytics/veille_state.json \
#   /home/ubuntu/Urban-Vision/reports/analytics/visiteurs_reels.html
```

Stockage **sécurisé** de la clé BigDataCloud (jamais dans le crontab) :

```bash
sudo mkdir -p /etc/urban-vision
sudo vi /etc/urban-vision/bdc.key            # coller la clé sur 1 ligne, sans retour ligne
sudo chown root:root /etc/urban-vision/bdc.key
sudo chmod 600 /etc/urban-vision/bdc.key
sudo /usr/bin/python3 /home/ubuntu/Urban-Vision/src/scripts/veille_visiteurs.py
grep -o '"bdc"' /home/ubuntu/Urban-Vision/reports/analytics/veille_state.json | head -1
```

Le fichier n'est lisible que par `root` ; le cron root lit la clé à l'exécution
(canon `bdc_key()`), aucun secret ne transite par le crontab ni par la ligne de
commande. Démarches initiales, une fois pour toutes : compte gratuit sur
`bigdatacloud.com` (sans carte bancaire, 10 k requêtes/mois, suffisant — une
passe NA < 20 requêtes) → `API Keys` → clé « IP Geolocation ».

Mise à jour en local (hors VM) : `python3 src/scripts/veille_visiteurs.py
--logs-dir <dossier>` ; `--no-lookup` désactive ip-api et BigDataCloud.

---

## 18. Maintenance

### 18.1 Mises à jour des données de référence

Le GTFS statique évolue (changements de plans 2026-07, 2026-08…) :

```bash
source .venv/bin/activate
python src/scripts/gtfs_static.py         # routes/stops (upsert)
python src/scripts/assign_stop_municipalities.py   # re-rattachement
```

Le rattachement communal étant coûteux (appels API Adresse pour les résidus
hors contours), il ne doit être relancé qu'en cas de changement de
plans/contours.

### 18.2 Taille de la base et WAL

- `data/urban_vision.db` grossit avec l'historique (**~749 Mo en dev local ;
  ~3,6 Go en production** au 01/10/2026, WAL ~143 Mo). Les agrégats sont
  reconstruits en incrémental ; la base n'est pas VACUUMed automatiquement.
  Les sauvegardes compressées pèsent environ le quart de la base (226 Mo pour
  968 Mo sur le poste de dev) ; 13 sont conservées au plus (section 19).
- Opérations possibles (à planifier, à confirmer par tests) :
  `sqlite3 data/urban_vision.db "PRAGMA wal_checkpoint(TRUNCATE);"`,
  `VACUUM;` (nécessite ~la taille de la base en espace libre). Vérifier la
  politique de rétention souhaitée AVANT tout nettoyage.

### 18.3 Tests après modification

```bash
.venv/bin/python -m pytest
```

Suite complète 431 tests, sans réseau ni données réelles (fixtures bases
temporaires, flux synthétiques). Les zones sensibles à couvrir lors d'un
changement de schéma : `test_refresh_aggregates.py` (exactitude des agrégats),
`test_refresh_segments.py` (tronçons),
`test_app_loaders.py` (requêtes du dashboard), `test_monthly_report.py`
(génération LaTeX).

### 18.4 Régénération du dashboard en cas de schéma

Le stockage `histogram` est utilisé pour les médianes ; en cas de changement du
format, penser à `refresh_aggregates(days=None)` (recalcul complet) une fois
via un Python shell. Pour `agg_daily_segment`, vider la table : au passage
suivant du timer `urban-vision-rafraichir`, `ensure_segments` la recalcule
intégralement, jour par jour. De même, vider `quality_days` fait recalculer
les courses et la qualité de tous les jours (`ensure_v2_history`, section 10.2) ; l'historique horaire des départs en avance se rattrape seul (`ensure_hourly_early`).

---

## 19. Sauvegarde et restauration

Depuis le 01/10/2026, `src/scripts/sauvegarde.py` sauvegarde la base chaque
nuit à 2 h 30 (`urban-vision-sauvegarde.timer`) :

1. copie cohérente par l'API de sauvegarde SQLite, sur une connexion en lecture
   seule (la collecte continue d'écrire pendant la copie) ;
2. `PRAGMA quick_check` sur la copie et relevé des effectifs des tables
   principales ; une copie corrompue arrête la sauvegarde en erreur ;
3. compression `zstd -3` (gzip si `zstd` est absent) dans
   `data/sauvegardes/urban_vision_<date>.db.zst`, avec un manifeste
   `urban_vision_<date>.json` (taille, empreinte SHA-256, contrôle, effectifs,
   durée, copie hors VM) ;
4. copie hors VM si `UV_BACKUP_PAR_URL` est défini (section 6.6) ; un échec de
   copie garde la sauvegarde locale et sort en code 2 ;
5. rotation : les 7 sauvegardes les plus récentes et la première de chacun des
   6 derniers mois.

Mesures sur le poste de dev : base de 968 Mo sauvegardée en 25 s (226 Mo
compressés), restauration contrôlée en 23 s.

La veille alerte si la dernière sauvegarde a plus de 26 h ou si l'unité est en
échec (section 9.4).

**Vérifier une sauvegarde** (restauration complète dans un dossier temporaire,
contrôle de l'empreinte, de l'intégrité et des effectifs) :

```bash
.venv/bin/python src/scripts/sauvegarde.py --verifier                 # la dernière
.venv/bin/python src/scripts/sauvegarde.py --verifier data/sauvegardes/urban_vision_2026-10-02.db.zst
```

**Restaurer** :

```bash
sudo systemctl stop urban-vision-collect urban-vision-collect-alerts urban-vision-dashboard urban-vision-rafraichir.timer
zstd -d data/sauvegardes/urban_vision_<date>.db.zst -o /tmp/restauration.db
mv data/urban_vision.db data/urban_vision.db.avant-restauration
rm -f data/urban_vision.db-wal data/urban_vision.db-shm
mv /tmp/restauration.db data/urban_vision.db
sudo systemctl start urban-vision-collect urban-vision-collect-alerts urban-vision-dashboard urban-vision-rafraichir.timer
```

Sans copie hors VM, les sauvegardes partagent le disque de la base : elles
protègent d'une erreur de manipulation ou d'une corruption, pas d'une perte du
disque. La partie reproductible (code, configuration de la VM, profils) est
dans git.

---

## 20. Git et déploiement

### 20.1 État et conventions

- Dépôt : `https://github.com/EliasKhallouk/Urban-Vision.git` (remote
  `origin`), branche unique `main` (suivie par `origin/main`).
- Historique récent (extrait, `git log --oneline`) : commits de type
  `feat:`, `fix:`, `docs:`, `chore:`, messages parfois en français
  (ex. `fix: escape external text against XSS and LaTeX injection`, « Ajout de
  tests pour… »).
- Intégration continue : `.github/workflows/tests.yml` installe les
  dépendances depuis zéro (Python 3.12, comme la VM), lance `ruff check
  --select E9,F63,F7,F82` (erreurs de syntaxe et noms non définis), la suite
  `pytest` et un contrôle de syntaxe de `deploy/deployer.sh`, à chaque push et
  pull request.
- `.gitignore` : `data/*`, `__pycache__/`, `*.pyc`, `.venv/`, `reports/output/`,
  `reports/recipients.json`, `data/*.log`, `data/collect.log`, `.env`,
  `opencode.json`, `.vscode/`.

### 20.2 Déploiement

Le déploiement reste déclenché à la main, mais il est scripté :

1. Sur le poste de dev : `git push` vers `main` (l'intégration continue
   rejoue les tests).
2. Sur la VM (`ssh ek-hub`) :
   ```bash
   cd ~/Urban-Vision && deploy/deployer.sh
   ```

`deploy/deployer.sh` refuse de tourner si des fichiers suivis ont été modifiés
sur la VM, puis : `git pull --ff-only` ; installe `requirements.txt` dans le
venv (avec `uv`, ou `pip` à défaut) ; installe les unités de `deploy/systemd/`
qui ont changé, `daemon-reload` et active les timers ; installe la rotation
des journaux ; installe le vhost nginx s'il a changé (copie datée de
l'ancien, `nginx -t`, retour arrière si refus) ; signale un écart entre la
crontab root et `deploy/cron/root.crontab` sans la modifier ; redémarre la
collecte, les alertes et le dashboard ; attend un relevé puis contrôle que les
services sont actifs, qu'un relevé réussi figure dans `collection_runs`, que
le dashboard répond 200 en HTTPS, et affiche les timers et le diagnostic de la
veille. Options : `--sans-pull`, `--sans-redemarrage`.

### 20.3 Rapport de production

La génération des rapports mensuels peut s'effectuer sur la VM (où `xelatex`
est installé) :

```bash
.venv/bin/python reports/generate_all_reports.py --month AAAA-MM --compile
```

Sortie dans `reports/output/AAAA-MM/` (gitignoré). Les PDF finaux sont ensuite
servis/transmis manuellement.

Génération automatique : le 1er de chaque mois à 3 h par
`urban-vision-rapports.timer` (section 14), PDF seuls (`--pdf-only`). Durée
d'un lot complet sur la VM : voir 26.2 U11.

---

## 21. Dépannage (troubleshooting)

| Symptôme | Cause probable | Actions |
|---|---|---|
| `sqlite3.OperationalError: database is locked` côté alertes | Verrou d'écriture tenu par le recalcul des agrégats (≈ 200 s avant le correctif de la section 9.3, quelques secondes depuis, dans son propre processus) | C'est géré par `busy_timeout` (180 s) + `rollback`/reprise ; consulter `data/alerts.log`. Le recalcul tourne toutes les 5 min dans son propre processus (timer `urban-vision-rafraichir`, section 10.2) |
| Base `urban_vision.db` absente ou vide | Init jamais faite / données perdues | `python src/scripts/db.py` (schéma + migration) puis relancer collecte et gtfs statique |
| Dashboard vide (warnings « pas d'observations ») | DB vide OU agrégats vides | Vérifier collecte (`tail -f data/collect.log`) ; le dashboard reconstruit les agrégats si vides, sinon relancer `python src/scripts/db.py` puis vérifier |
| `xelatex: command not found` | LaTeX absent | `apt install texlive-* fonts-inter` cf. `apt-requirement.txt` |
| Rapport d'une commune vide (« Absence de données exploitables ») | Aucun passage stabilisé dans le périmètre ce mois (couverture, vacances, arrêts sans flux RT) | Comportement normal et documenté ; vérifier le rattachement communal (`assign_stop_municipalities.py`) |
| « Commune(s) inconnue(s) » | Nom imparfait ou rattachement absent | Lancer `assign_stop_municipalities.py` ; orthographier comme dans `recipients.example.json` |
| « Le rattachement des arrêts aux communes n'a pas été calculé » | Table `stop_municipalities` absente | Exécuter `src/scripts/assign_stop_municipalities.py` |
| `dashboard.log` : `Please replace use_container_width...` | Dépréciation Streamlit | Simple warning, fonctionnel (à migrer sur `width=` à terme) |
| Le dashboard local n'est pas joignable | Il écoute sur 127.0.0.1 par défaut | Usage local OK ; pour l'exposition c'est nginx (ne pas rouvrir 8501 en prod) |
| Recopie de prod en local : DB « invisible » | `data/` et les logs sont gitignorés | Transférer manuellement le fichier et les `-wal`/`-shm` |
| `settings.json`/`opencode.json` présents mais `.gitignore` | Fichiers locaux non versionnés | Normal (config développeur) |
| Test `test_collect` « refresh trop fréquent » | Timings serrés | Relancer ; le test tolère `[1,3]` refreshes |
| « Trou de collecte détecté : 3 à 5 minutes » en rafale, toutes les 5 à 9 min | Rafraîchissement des agrégats plus long que 180 s (section 9.3) | `grep -E "Agrégats rafraîchis\|agrégats lent" data/collect.log \| tail` ; rejouer `tests/test_refresh_aggregates.py` (plan de requête) |
| Email « Flux temps réel quasi vide » | Flux TBM sans courses (incident côté TBM, grève, jour férié) | `grep "OK - " data/collect.log \| tail` : nombre d'entités proche de 0 ; consulter les alertes TBM ; rien à faire côté collecteur |
| Email « Tâche planifiée en échec » | Recalcul, sauvegarde, archive GTFS ou rapports sortis en erreur | `journalctl -u <unité> -n 50` ; relancer avec `sudo systemctl start <unité>` une fois la cause corrigée |
| Email « Sauvegarde manquante » | Sauvegarde en échec ou timer inactif | `systemctl list-timers urban-vision-sauvegarde.timer` ; `journalctl -u urban-vision-sauvegarde.service -n 20` ; place disque (`df -h /`) |
| Email « Flux temps réel figé » | TBM ne met plus à jour l'horodatage du flux | comparer `feed_ts` dans `collection_runs` ; rien à faire côté collecteur |
| Aucun email d'alerte reçu | Timer inactif, identifiants absents ou refusés | `systemctl list-timers urban-vision-veille-collecte.timer` ; `journalctl -u urban-vision-veille-collecte.service -n 20` ; `sudo .venv/bin/python src/scripts/veille_collecte.py --test-email` |

---

## 22. FAQ

**D'où viennent les données ?**
Des flux GTFS publics TBM (Bordeaux Métropole) — TripUpdates (temps réel),
ServiceAlerts et le GTFS statique. Clé d'API publique
`opendata-bordeaux-metropole-flux-gtfs-rt`. Rien n'est calculé à partir de
données nominatives.

**Qu'est-ce qu'un « passage analysé » ?**
Une observation `SCHEDULED` dont l'heure de départ et le retard sont connus et
dont le véhicule est sorti du flux temps réel depuis au moins 20 minutes
(buffer de stabilisation) : le retard est alors considéré définitif.

**Comment se calcule le score de fiabilité ?**
Sur 100 passages prévus, le nombre de passages assurés (arrêt desservi, course
non supprimée) et à l'heure (entre 1 min d'avance et 5 min de retard), voir
section 10.3.

**Le dashboard et les rapports utilisent-ils les mêmes chiffres ?**
Oui, à la granularité et au buffer près : même définition des passages
(stabilisation 20 min, fenêtre −60 s / +300 s), même score (`indicateurs`),
mêmes jours et lignes écartés, palette et seuils issus de `reports/palette.py`.
Le rapport compte les passages dans `observations` et le dashboard dans les
agrégats : les deux comptes sont égaux quand les agrégats du mois sont à jour
(un jour jamais agrégé manque au dashboard, pas au rapport). Différences constatées : le dashboard n'exclut
pas les trous de collecte de ses agrégats (contrairement à `analyze.py`), voir
section 7.3.

**Comment suis-je prévenu d'un problème de collecte ?**
Par email, via `veille_collecte.py` (section 9.4) : collecte arrêtée, trous de
collecte, flux TBM quasi vide, avertissements répétés dans les logs.
Identifiants SMTP : section 6.5.

**Pourquoi faut-il exécuter `assign_stop_municipalities.py` ?**
Les rapports communaux et la vue territoriale du dashboard filtrent par
`stop_municipalities`. Sans cette table, aucune requête communale ne fonctionne
(erreurs explicites).

**Peut-on sécuriser le dashboard ?**
Il est public par choix. L'infra n'expose que 22/80/443 et le dashboard n'est
servi qu'en HTTPS via nginx.

**Comment ajouter une commune/ville aux profils ?**
Éditer `reports/recipients.json` (copie de `recipients.example.json`) avec la
clé `mairie_<ville>` et la bonne orthographe (celle de `stop_municipalities`),
puis `--profile mairie_<ville>`.

**Les données GTFS-RT sont-elles archivées « telles quelles » ?**
Oui (upsert des dernières valeurs de chaque passage) — mais le **historique
brut à chaque poll n'est pas** conservé : une observation est écrasée à chaque
cycle (dernière valeur gagne). La rétention long terme passe par les agrégats.

---

## 23. Limites connues

1. **Marge d'incertitude ± 60 s** : le flux est interrogé toutes les 60 s ;
   l'heure de départ réelle peut précéder/suivre l'observation d'au plus une
   minute (documenté dans les rapports, section « Méthode »).
2. **Les trous de collecte** ne sont pas traités de façon uniforme par les
   trois consommateurs (analyse exclut, rapport comptabilise, dashboard
   n'exclut pas). C'est une dette méthodologique documentée à harmoniser.
   Du 22/09/2026 au 01/10/2026 (dernier trou terminé à 12 h 01, collecteur
   corrigé déployé à 12 h 09, section 9.3), `collection_gaps` a reçu ≈ 170
   trous de 3 à 5 min par jour (1 324 trous, 4 255 min). Ce sont de vraies pauses de relève, mais le flux
   TBM garde les arrêts desservis visibles après le départ et le relevé suivant
   récupère presque toutes les valeurs : volumes de passages inchangés
   (≈ 250 000 par jour de semaine), 2,4 à 2,5 % de valeurs finales issues d'une
   prévision faite plus de 2 min avant le départ, contre 1,3 % auparavant. Le
   rapport ne compte plus comme interruptions que celles d'au moins 10 min ;
   les plus courtes sont mentionnées à part, comme rattrapées (section 12.3).
3. **`daily_line_stats`** est écrite mais **jamais lue** par le dashboard ni
   les rapports (déjà obsolète).
4. **`cause` des ServiceAlerts quasi toujours `UNKNOWN_CAUSE`** : le champ est
   ignoré ; les alertes n'ont aucun lien causal établi avec les statistiques.
5. **Histogramme JSON** des agrégats : dépend d'une fonction
   `json_group_object` bien définie ; tout changement de format invalidera les
   recalculs incrémentaux (degré de liberté).
6. **Sauvegardes sur le disque de la VM** : la sauvegarde quotidienne et la
   rotation des journaux existent depuis le 01/10/2026, mais sans adresse de
   copie hors VM (section 6.6) les sauvegardes ne protègent pas d'une perte du
   disque.
7. **Le dashboard** dépend de CDN externes (Highcharts, deck.gl et MapLibre
   via jsDelivr, fond de carte Carto) : hors ligne le site reste affiché mais
   les graphiques et la carte sont dégradés.
8. **Exécution directe** : `generate_monthly_report.py` est documenté comme
   « moteur interne » ; appelé directement, il fonctionne mais peu de garde-fous
   UX.
9. **Courses supprimées estimées** : la plupart des courses marquées
   `CANCELED` dans `trip_status` n'ont aucune ligne dans `observations`
   (2 562 sur 3 078 dans la base de développement au 15/09/2026). Le score les
   compte, nettes des courses ajoutées, en estimant leurs passages au prorata
   des passages par course de la ligne ce jour-là (section 10.3).
10. **Profil de ligne approché** : l'ordre des arrêts est le rang moyen
    (`sum_seq / eligible`) ; sur une ligne à branches, les arrêts des
    différentes branches s'intercalent dans le profil (les variantes
    marginales, sous 5 % des passages, sont écartées).
11. **Profil d'un créneau approché** : `agg_hourly_stop` range chaque passage
    dans l'heure de départ **à cet arrêt** ; un véhicule vu à 18 h 50 à un
    arrêt peut être compté à 19 h aux arrêts suivants. Le profil d'un créneau
    montre donc le retard des passages de chaque arrêt sur cette heure, pas le
    suivi véhicule par véhicule.
12. **Regroupement des quais sur la carte** : même nom et même mode à moins
    de 150 m ; dans les pôles (Quinconces, Palais de Justice), des quais de
    lignes différentes sont réunis dans un même marqueur (« 6 quais ») ; la
    fiche et l'infobulle les détaillent un à un.
13. **Flux TBM vide sans trou de collecte** : le 08/09/2026 (≈ 3 h–9 h 30) et le
   24/09/2026 (≈ 3 h–11 h), le flux TripUpdates ne contenait presque aucune
   course alors que le collecteur tournait. Aucun trou n'est enregistré et les
   agrégats de ces deux journées sont incomplets (214 451 et 169 192 passages,
   contre ≈ 250 000 un jour de semaine). La veille (section 9.4) signale
   désormais ces situations ; les données manquantes ne sont pas récupérables.
   Ces deux journées sont marquées `incomplet` dans `quality_days` et exclues
   de tous les indicateurs (section 10.3).
14. **Score de fiabilité** : une course ajoutée en renfort (`DUPLICATED`) compense
    une suppression de la même ligne le même jour, même à une autre heure ;
    les arrêts qu'une course de remplacement partielle ne dessert pas ne sont
    pas comptés comme non desservis ; la marge ne couvre que la variabilité
    entre jours, pas les erreurs systématiques du flux. Sur une ligne qui n'a
    circulé que 2 ou 3 jours, la marge de Student est large et peu informative.
15. **Licence Highcharts** : gratuite pour un usage non commercial seulement.
    Proposer le service contre rémunération (par exemple à une collectivité)
    suppose une licence Highcharts ou le passage à une bibliothèque libre
    (Apache ECharts).
16. **Vue rapide non éprouvée** : la vue rapide, le guide et le lexique
    (section 11.3) n'ont pas encore été testés auprès de lecteurs non
    analystes ; seuls ces tests diront où un élu ou un agent bloque encore.

---

## 24. Informations historiques et obsolètes

### 24.1 README racine

Le README racine historique contenait les instructions d'exploitation (SSH,
services, rapports, tests) mais avec une rédaction datée (« POUR CE CONNECTER »,
« SCRIPTE »…). Il a été **entièrement refondu** en une page de
synthèse professionnelle pointant vers `docs/DOCUMENTATION_TECHNIQUE.md`.
Le présent document reprend tout le contenu factuel corrigé (URLs, procédure
SSH, commandes systemd, génération de rapports, tests).

### 24.2 Fichiers/données orphelins constatés

- `data/dashboard.log` : garde de traces d'une exécution sur `0.0.0.0:8501`
  antérieure à la config `127.0.0.1` (I2).
- `reports/plan/` : 27 PDF de plans (schémas CAO/SVG de lignes et communes) —
  documents de travail, non générés par le code.
- `reports/output` : contient également des fichiers `.tex/.log/.pdf/.png` laissés
  à la racine du dossier d'un mois (reliquats de versions antérieures).

### 24.3 « Historique en cours de constitution »

La comparaison avec le mois précédent affiche « Historique en cours de
constitution… » tant qu'aucun mois antérieur n'existe dans la base — comportement
codé dans `comparison()` (`generate_monthly_report.py:434`).

---

## 25. Glossaire

| Terme | Définition |
|---|---|
| **TBM** | réseau de transports Bordeaux Métropole (Bus, Tram, Ferry). |
| **GTFS / GTFS-RT** | formats d'échange de données transport (statique / temps réel) de l'opérateur. |
| **TripUpdates** | flux GTFS-RT des avancements de courses (retards, annulations). |
| **ServiceAlerts** | flux GTFS-RT des alertes (travaux, incidents). |
| **Observation (brute)** | une ligne `trip_update.stop_time_update` enregistrée (peu importe le statut). |
| **Passage analysé** | observation `SCHEDULED`, retard et heure de départ connus, stabilisée ≥ 20 min. |
| **Arrêt non desservi** | événement `SKIPPED` sur un arrêt prévu. |
| **Ponctualité (à l'heure)** | % de passages partis entre 60 s d'avance et 300 s de retard. |
| **Score de fiabilité** | passages assurés et à l'heure / passages prévus, courses supprimées comprises (section 10.3). |
| **Fiable / à surveiller / problématique** | état affiché selon le score : à partir de 80, de 50 à 80, sous 50 (`palette.tier_label`). |
| **Vue rapide / vue détaillée** | deux niveaux de lecture du dashboard ; la vue rapide garde les phrases et le graphique principal (section 11.3). |
| **Service assuré** | part des passages attendus effectivement desservis (ni course supprimée, ni arrêt non desservi). |
| **Attente excédentaire** | temps d'attente moyen ajouté par l'irrégularité des passages d'une ligne fréquente, par rapport à la grille prévue. |
| **Jour incomplet** | jour dont au moins 3 heures comptent moins de la moitié du volume habituel (`quality_days`) ; exclu de tous les indicateurs. |
| **WAL** | Write-Ahead Log (mode journal SQLite, base lisible + écrivain concurrent). |
| **Upsert** | `INSERT ... ON CONFLICT ... DO UPDATE` (dernière valeur gagne). |
| **Agrégats** | tables `agg_*` précalculées (jour/heure × ligne/arrêt) par `refresh_aggregates`. |
| **Histogramme JSON** | colonne `histogram` stockant `{retard_s: effectif}` pour une médiane exacte. |
| **Périmètre** | filtrage d'un rapport/dashboard par lignes et/ou communes. |
| **Rattachement communal** | jointure spatiale arrêt ↔ commune (22 contiguïtés de Bordeaux Métropole). |
| **xelatex** | compilateur LaTeX utilisé pour les PDF des rapports. |
| **DuckDNS** | service DNS dynamique donnant `urban-vision.duckdns.org`. |

---

## 26. Audit de documentation

### 26.1 Faits établis par lecture du code

- Les 3 URLs de flux + URL des contours + URL de l'API Adresse.
- Schéma complet SQLite (colonnes, PK, index) — `src/scripts/db.py`.
- Intervalles, timeouts et seuils (tableau 6.3 + 6.4).
- Chaîne d'agrégation et mode incrémental `refresh_aggregates(days=...)` ; agrégat tronçon `refresh_segments(days=...)` et rattrapage au démarrage du collecteur.
- Formule du score, seuils de la synthèse exécutive, structure des PDF.
- Score de fiabilité : `src/scripts/indicateurs.py`, tables `agg_daily_trips`,
  `agg_hourly_regularity` et `quality_days`, testées par
  `tests/test_methode_v2.py`.
- CLI complète des 6 scripts et du moteur.
- Cache dashboard 60 s, buffer 20 min, views et loaders (noms de fonctions et
  lignes exacts fournis en annexe de la section 11).
- Accessibilité dashboard : `<html lang="fr">`, module `accessibility.js`
  Highcharts (non-Stock), description auto des graphiques, légende textuelle
  sous la carte des arrêts.
- Tests : 557, isolés (suite `pytest` complète : 557 passed), flux synthétiques
  (`gtfs_factory`), fixtures `tmp_path`.
- Veille des visiteurs : `src/scripts/veille_visiteurs.py` (stdlib), testée par
  `tests/test_veille_visiteurs.py` ; sorties dans `reports/analytics/`
  (gitignoré).
- Git : branche `main`, remote GitHub ; la refonte (`refonte-ux-dashboard`)
  y est intégrée depuis le 02/10/2026. Production sur le commit `0e7d37d`
  (refonte et phase 3, `dd873e7`, déployée le 02/10/2026 à 9 h 23 par
  `deploy/deployer.sh --sans-pull` ; formulation des tronçons, `0e7d37d`, vers
  9 h 40 avec `--sans-redemarrage` et un redémarrage du seul dashboard ;
  méthode 2.0, `915fde6`, le 02/10/2026 à 0 h 09 ; protection des données,
  `3afe0d3`, le 01/10/2026 à 22 h 20).
- Veille de la collecte : `src/scripts/veille_collecte.py` (stdlib), testée par
  `tests/test_veille_collecte.py` ; état dans `data/veille_collecte.json`.
- Environnement de production : unités systemd exactes (section 14), vhost
  nginx + cert Let's Encrypt (exp. 11/12/2026, renouvelé par `certbot.timer`),
  venv Python **3.12.14**, `xelatex` + fonts-inter présents ; `crontab` root
  depuis le 16/09/2026 (GoAccess + veille des visiteurs, sections 17), pas de
  logrotate dédié, aucune sauvegarde automatique, `reports/recipients.json`
  absent.
- `requirements.txt`, `requirements-dev.txt`, `apt-requirement.txt`,
  `pytest.ini`, `.streamlit/config.toml`, `.vscode/settings.json`, `.gitignore`.

### 26.2 Environnement de production

État relevé sur la VM de production le 16/09/2026, mis à jour le 02/10/2026 et le 11/10/2026 :

| # | Point | État en production |
|---|---|---|
| U1 | Contenu exact des 3 unités systemd | Intégral en section 14 ; les 3 services sont `active` ; dashboard = `--server.address=127.0.0.1 --server.port=8501` Depuis le 02/10/2026 (`dd873e7`), le dashboard en ligne est la refonte : 8 pages, vue rapide par défaut, guide de lecture, pages « Rapports mensuels » (août et septembre 2026 téléchargeables) et « À propos » ; graphiques Highcharts chargés depuis jsDelivr. |
| U2 | Configuration nginx | Vhost `urban-vision` : 443 ssl → `proxy_pass 127.0.0.1:8501` (headers WebSocket), bloc :80 = 301 HTTPS (`$host` exact) sinon 404 ; vhost `default` de stock présent (page par défaut) |
| U3 | Sauvegarde / rotation | Aucune sauvegarde (snapshots OCI non accessibles depuis le système) ; aucune règle logrotate dédiée ; `crontab` root (ajouté le 16/09/2026) : génération GoAccess toutes les 5 min (§17.1) et veille des visiteurs toutes les 5 min (§17.2) |
| U4 | Version Python | venv **3.12.14**, Python système **3.10.12** (dev local : 3.11.2) |
| U5 | URL de geocoding | Code prod sur `3afe0d3` (01/10/2026) ; fallback API Adresse `https://api-adresse.data.gouv.fr/reverse/?` ; aucune trace d'appel dans les logs récents |
| U6 | `reports/recipients.json` | Absent sur la VM ; la génération mensuelle passe par `--network` / `--commune`, ou exige `--recipients-file` |
| U7 | `xelatex` + fonts | `/usr/bin/xelatex` et `/usr/bin/lualatex` présents ; 38 polices Inter installées (`fc-list`) |
| U8 | Veille des visiteurs | Modifiée le 16/09/2026 (déployée sur VM, sha256 vérifié) : filtre **Nouvelle-Aquitaine** en table principale du HTML (§17.2) ; IP utilisateur `90.120.193.41` en **violet** ; coordonnées `lat/lon/zip` (ip-api) + **carte Leaflet** (tuiles **CARTO**, remplacées suite au blocage tile.openstreetmap.org 16/09) ; BigDataCloud actif depuis le 17/09 (clé `root:600`), localité + CP dans la colonne « Ville » ; **bannière « Dernier visiteur en France »** (toutes régions) ajoutée en tête le 17/09 ; ré-essai BigDataCloud 2 h après erreur transitoire (403) ; les IP « probable bot » (profil `p-bot`, orange) sont exclues de la passe BigDataCloud Minimisée le 02/10/2026 (crontab de `deploy/cron/root.crontab` installée, GoAccess `--anonymize-ip`) : au premier passage, les 1 148 adresses complètes de l'état sont devenues 782 réseaux tronqués, sans aucune adresse complète restante ; géolocalisation par ipwho.is en HTTPS ; visiteurs absents depuis 30 jours oubliés. La page HTML ne contient plus d'adresse complète de visiteur (seule une URL de requête de scanner en contient une, dans la colonne des pages demandées). |
| U9 | Collecte | 3 services `active` ; base 3,6 Go (12,9 millions d'observations), WAL 143 Mo ; trous de collecte de 3 à 5 min presque continus du 22/09/2026 au 01/10/2026 à 12 h 01 (I8, 26.3). Correctif déployé le 01/10/2026 à 12 h 09 (seul `urban-vision-collect` redémarré) : de 12 h 09 à 14 h 16, 128 relevés, aucun trou, aucun warning, plus grand écart entre deux relevés 80 s, rafraîchissement des agrégats 16,8 à 19,0 s ; agrégats du 30/09 identiques au comptage direct dans `observations` (251 591 passages) ; dashboard HTTPS 200 Depuis le déploiement de `3afe0d3` (01/10/2026, 22 h 20) : une ligne de `collection_runs` par relevé (écriture moyenne ≈ 0,8 s, aucune erreur ni écart supérieur à 60 s sur les 20 premières minutes), prévisions retenues aux horizons 10, 5 et 2 min, recalcul des agrégats dans `urban-vision-rafraichir` (19,3 s au premier passage), collecteur sans recalcul Depuis `915fde6` (02/10/2026, 0 h 09) : `idx_observations_sched_delay` supprimé (100 082 pages de 4 Ko libérées) ; `ANALYZE` lancé une fois à la main (75 s pendant lesquelles les écritures du collecteur attendent) ; historique de la méthode 2.0 calculé au premier passage du recalcul (66 jours en 266 s ; `quality_days` : 08/09 et 24/09 `incomplet`, 21/08 `degrade`, 14 jours `non_evalue`, 50 `ok`) ; recalcul courant, méthode 2.0 comprise, 18 à 19 s ; septembre 2026 : score 2.0 75,1 ± 1,2, ponctualité stricte 78,4 %, service assuré 95,9 %, comme sur la copie locale ; aucun trou de collecte depuis le 01/10/2026 à 12 h 01. Depuis `dd873e7` (02/10/2026, 9 h 23) : table `agg_daily_segment` et index `idx_agg_hourly_stop_route` créés au redémarrage du collecteur ; rattrapage des tronçons au premier passage du recalcul : 68 jours en 357 s (pause d'une seconde comprise) ; ensuite, agrégats en 24 s et tronçons en 7 s par passage, à l'heure de pointe du matin. |
| U10 | Veille de la collecte | Installée le 01/10/2026 : `urban-vision-veille-collecte.timer` toutes les 5 min, identifiants dans `/etc/urban-vision/alertes.env` (`root:600`, `UV_SMTP_USER` et `UV_SMTP_PASSWORD`, serveur Gmail par défaut) ; premier passage : 4 conditions `ok` ; email de test envoyé le 01/10/2026. Deux copies laissées par l'éditeur (`alertes.env.save`, `alertes.env.save.1`) restent dans le dossier : à supprimer. Sortie SMTP vers `smtp.gmail.com` ouverte sur 465 et 587 Depuis `3afe0d3` : 7 conditions (journal de collecte, flux figé, sauvegarde, tâches planifiées en plus), unité autorisée en `AF_UNIX` pour interroger systemd ; toutes `ok` au premier passage. `UV_HEARTBEAT_URL` non défini (pas de sonde externe) |
| U11 | Tests et rapports sur la VM | Suite lancée le 01/10/2026 sur une copie du commit déployé, avec l'interpréteur du venv (Python 3.12.14, SQLite 3.53.1, aarch64) et pytest installé hors du venv : 200 passed tant que `matplotlib` manquait (`test_monthly_report.py` non importable), 262 passed après son installation (`81eb95b`). Rapports d'août et de septembre générés le 01/10/2026 sur la VM (`--compile --pdf-only`, 34 PDF chacun) : 40 min pour août, 78 min pour septembre, 1 h 35 min de CPU au total, aucun trou de collecte pendant la génération ; les 6 anciens PDF d'août sont dans `reports/output/corbeille/2026-08-ancien/`. Timer `urban-vision-rapports.timer` installé, prochain lancement le 01/11/2026 à 3 h Rapports d'août et de septembre relancés le 01/10/2026 à 22 h 22 (unité transitoire `urban-vision-rapports-regeneration`) pour la ligne « trous de collecte » corrigée Cette régénération s'est terminée le 02/10/2026 à 0 h 01 (codes retour 0, 34 PDF par mois, 1 h 28 min de CPU). Le 02/10/2026 : suite lancée sur une copie de `915fde6` (Python 3.12.14, SQLite 3.53.1, pytest hors du venv) : 341 passed en 18 s ; rapports réseau d'août et de septembre régénérés avec la page « Méthode 2.0 » (unité transitoire `urban-vision-rapports-v2`, 68 s et 85 s) ; les rapports communaux de ces deux mois n'ont pas cette page. |
| U12 | Protection des données | Première sauvegarde le 01/10/2026 à 22 h 11 : 3 628 Mo compressés en 955 Mo (zstd) en 5 min 33 s, restauration contrôlée en 3 min 13 s (12 983 514 observations) ; timers `urban-vision-sauvegarde` (2 h 30) et `urban-vision-archive-gtfs` (6 h 15) actifs ; première version du GTFS archivée (`gtfs_2026-10-01_3ea3193653b8.zip`, valide du 01/10 au 30/12/2026). Copie hors VM non configurée (`/etc/urban-vision/sauvegarde.env` absent) Sauvegarde avant le déploiement de la méthode 2.0 le 02/10/2026 à 0 h 01 (955 Mo, 6 min 46 s, contrôle ok). |
| U13 | Configuration versionnée | Unités, vhost et rotation installés le 01/10/2026 par `deploy/deployer.sh` ; en-têtes HTTP actifs (HSTS, `nosniff`, `SAMEORIGIN`, `Referrer-Policy`, `Permissions-Policy`) ; rotation hebdomadaire de `collect.log` et `alerts.log` ; crontab root identique à `deploy/cron/root.crontab` ; copie de l'ancien vhost conservée en `urban-vision.avant-<date>` Crontab root remplacée le 02/10/2026 par `deploy/cron/root.crontab` (GoAccess `--anonymize-ip`), copie de l'ancienne dans `/tmp/root.crontab.avant` le temps du déploiement. |
| U14 | Score de fiabilité unique | Déployé le 11/10/2026 (`f673240`, `deploy/deployer.sh`) : colonne `cnt_lt60` ajoutée aux tables horaires au passage suivant du timer, rattrapage de 75 jours en 1 166 s (environ 15,5 s par jour), collecte ininterrompue pendant le rattrapage (21 relevés en 21 min, aucun trou) ; score de septembre 75,1 ± 1,2, identique entre le dashboard et `indicateurs.indicators` |

### 26.3 Incohérences constatées (code vs docs vs logs)

Incohérences corrigées (I1–I4 le 14/09/2026, I5–I6 le 30/09/2026, I7 à I14 le 01/10/2026, I15 à I24 le 02/10/2026, I25 le 04/10/2026, I26 et I27 le 10/10/2026, I28 et I29 le 11/10/2026) :

| # | Incohérence | Correctif appliqué |
|---|---|---|
| I1 | `DB_PATH` de `gtfs_static.py` et `analyze.py` pointait vers `src/data/` (`parents[1]`) | `parents[2]` (aligné sur `collect.py`) + 2 tests de chemin ajoutés (`tests/test_gtfs_static.py`, `tests/test_analyze.py`) |
| I3 | `src/sql/001_add_departure_time.sql` et `db.py` appliquaient la même `ALTER` | migration versionnée supprimée — `db.py` est l'unique mécanisme (PRAGMA + ALTER à l'import) |
| I4 | Aide CLI `--compile` : « pdflatex » | texte d'aide = `xelatex/lualatex` (`generate_monthly_report.py`) |
| I5 | Carte territoriale : la note annonçait « le score de la ligne principale » alors que la couleur valait la ponctualité ≤ 5 min de l'arrêt, sans les arrêts non desservis | score de l'arrêt = ponctualité − 2 × arrêts non desservis (même formule que les lignes), note et légende textuelle réécrites (§11.3) |
| I6 | Charte : fond de page Cornsilk (`.streamlit/config.toml`, `.stApp`) alors que la charte impose un fond blanc ; modes codés par couleur (tram en Copperwood, couleur du palier négatif) ; seuils KPI codés en dur dans « Analyse d'une ligne » | fond blanc ; modes codés par forme (§6.4) ; KPI par `palette.kpi_tier` |
| I7 | Infobulles Highcharts : format `{point.z:,}` / `{point.passages:,}` (sans `f`, donc traité comme un format de date) — le nombre de passages ne s'affichait pas | `{…:,.0f}` partout, test de non-régression dans `tests/test_highcharts.py` |
| I8 | Trous de collecte presque continus depuis le 22/09/2026 alors que le collecteur tournait : le rafraîchissement incrémental des agrégats (`db.py`) lisait tout l'historique (index `idx_observations_sched_delay`, filtre `start_date` sans index, jointure quadratique de `_DAILY_STOP_SQL`) ; sa durée (≈ 200 s) dépassait le seuil de trou (180 s) | Requêtes bornées par `idx_observations_departure_time` et `idx_observations_last_seen_at`, `+o.schedule_relationship`, `UNION ALL` + `GROUP BY` (section 9.3) ; durée du rafraîchissement journalisée par `collect.py` (warning au-delà de 60 s) ; veille email `veille_collecte.py` (section 9.4) ; tests `TestRefreshIncrementalBorne`. Déployé en production le 01/10/2026 à 12 h 09 ; aucun trou depuis |
| I9 | Après un redémarrage du collecteur la nuit, le dernier succès était lu dans `MAX(last_seen_at)`, qui n'avance plus quand le flux est vide : un redémarrage à 4 h enregistrait un faux trou de plusieurs heures | Le dernier succès est lu dans `collection_runs` (repli sur `MAX(last_seen_at)`), test `TestJournalDeCollecte` |
| I10 | Méthode des rapports : « les données produites pendant l'intervalle sont exclues de l'analyse » et « N observations exclues », alors que le rapport n'exclut rien et que N mêlait arrêts non desservis et passages sans retard | Texte de méthode réécrit, seules les interruptions d'au moins 10 min sont présentées comme pouvant faire perdre des passages (`gap_methodology_line`), tests `TestLigneTrousDeCollecte` |
| I11 | `idx_observations_sched_delay` restait mis à jour à chaque relevé alors qu'aucune requête ne l'utilisait plus depuis I5, et le dashboard le recréait à chaque démarrage (`app.py::INDEX_DDL`) | Retiré de `SCHEMA_DDL` et de `INDEX_DDL`, supprimé par `init_db` (`DROP INDEX IF EXISTS`) ; écriture d'un relevé 34 % plus rapide, ≈ 400 Mo de pages libérées (section 9.3) ; test `TestAgregatsV2::test_index_piege_supprime_a_l_import` |
| I12 | La vue « Suivi de la collecte » parcourait cinq fois toute la table `observations` à chaque expiration du cache (≈ 44 s de requêtes sur la copie de production) | Totaux lus dans `MAX(rowid)`, `trip_status` et `agg_daily`, répartition horaire bornée aux 7 derniers jours (≈ 1,6 s) ; tests `TestLoadCollectionStats` |
| I13 | Tables `agg_daily`, `agg_hourly`, `agg_daily_stop`, `agg_hourly_stop` et `agg_daily_segment` définies deux fois (`SCHEMA_DDL` et `AGG_DDL`) | `SCHEMA_DDL` inclut `AGG_DDL`, seule définition des tables agrégées |
| I14 | Sections 3 et 18.4 : le rattrapage de `agg_daily_segment` était attribué au collecteur (`collect.py::ensure_segments`, redémarrage de `urban-vision-collect`), alors qu'il est fait par le recalcul planifié depuis le 01/10/2026 | Sections 3 et 18.4 corrigées (`rafraichir_agregats.py::ensure_segments`) |
| I15 | Rattrapage de l'historique de la méthode 2.0 : les transactions des jours s'enchaînaient sans pause. Le 02/10/2026 entre 0 h 12 et 0 h 14, le collecteur a attendu le verrou d'écriture : journal du relevé de 0 h 11 non écrit (« database is locked »), 175 s entre deux relevés réussis pour un seuil de trou de 180 s. `ensure_segments` avait le même défaut | Pause d'une seconde entre deux jours rattrapés dans `ensure_v2_history` et `ensure_segments` (`BACKFILL_PAUSE_SECONDS`, section 10.2) ; tests `TestHistoriqueV2::test_pause_entre_deux_jours_pour_laisser_ecrire_le_collecteur` et `TestRattrapageTroncons::test_table_vide_recalculee_jour_par_jour` |
| I16 | Page « Lignes » : la ligne « à examiner en premier » et la ligne ouverte par défaut étaient prises dans le classement complet, y compris les lignes sous 50 passages (sur la base de développement, S24 avec 25 passages et 1 jour de données) | Choix dans `visible_ranking`, comme le tableau des lignes |
| I17 | Fiche ligne, « Arrêts les plus touchés » : des arrêts d'un seul passage y figuraient à 0/100 | Seuil `MIN_OBSERVATIONS` (50 passages), message si aucun arrêt ne l'atteint |
| I18 | Légendes et textes du dashboard : noms internes des couleurs (« Olive Leaf ≥ 80 », « en Copperwood », « Fond Cornsilk ») et paliers « positif / moyen / négatif » affichés aux lecteurs | Mots courants (`palette.tier_label`, `render_tier_legend`), couleurs décrites en clair (« orange foncé », « crème ») ; test `TestMotsDesPaliers` |
| I19 | Verdict de « Mon territoire » pour une commune : moyenne pondérée des scores d'arrêts, différente du score du bandeau (calculé sur les totaux) | `network_score` pour le verdict, le bandeau et la comparaison au réseau ; `_territorial_score` supprimé |
| I20 | Highcharts chargé depuis `code.highcharts.com`, dont l'usage en production n'est pas prévu et qui refuse les navigateurs sans interface | jsDelivr, version figée 13.1.1 (`HIGHCHARTS_CDN`, §11.4) |
| I21 | Veille des visiteurs : adresses IP complètes conservées sans limite de durée et envoyées sans chiffrement (HTTP) à ip-api.com ; rapport GoAccess avec adresses complètes | Adresses tronquées dès la lecture, conservation 30 jours, géolocalisation HTTPS (ipwho.is), GoAccess `--anonymize-ip` (§17) ; tests `TestMinimisation` |
| I22 | Phrases des fiches : « entre Allende et Allende », « Tronçon Allende → Allende » quand deux arrêts consécutifs d'un parcours portent le même nom (deux quais d'un même arrêt) | « à l'arrêt Allende », « Arrêt Allende » (`diagnostic.segment_path`, `segment_title`) ; tests `TestTronconsMemeNom` |
| I23 | Bouton « Compris » du guide de lecture : `st.rerun()` relançait le calcul complet de la page (plusieurs secondes) | Bouton retiré, la boîte se ferme par ✕ ou Échap (`show_guide`) |
| I24 | Le guide de lecture se rouvrait à chaque rechargement (nouvelle session Streamlit) ; le bouton « Guide de lecture » devenait illisible au survol | Cookie `uv_guide_vu` ; couleur de survol des boutons de la sidebar (fond ocre, texte vert foncé) |
| I25 | Test utilisateur du 04/10/2026 : la commune se choisissait dans un réglage peu visible, la ligne seulement dans « Lignes », l'arrêt seulement dans « Mon territoire », et une fiche ligne ouverte autrement que depuis un arrêt n'avait pas de retour | Barre de contexte persistante en fil d'Ariane avec « ← Revenir » et historique (§11.3) ; listes « Chercher un arrêt » et « Ligne analysée » et bouton « Revenir à l'arrêt » remplacés par la barre ; tests `TestFilDAriane` |
| I26 | Dashboard : nombres affichés avec un point décimal (« 75.1 ») ; infobulles de graphiques sans unité (« > 5 min : 12.3 », heures « 7 »), dates d'infobulle en anglais ; pastille « ? » de 15 px peu visible, à infobulle native sans lien ; étiquettes de lignes superposées sur la carte | `format_number` / `number_formatter` (virgule, signe −) dans tout le dashboard ; `lang` français et unités dans chaque infobulle Highcharts ; pastilles « ? » de 20 px avec bulle et lien vers le lexique ; étiquettes réparties et filtrées par collision ; tests `TestNombresALaFrancaise`, `TestLexique`, `TestInfobulles` |
| I27 | Typographie du dashboard : 14 tailles différentes ; titres de bloc (15 px) plus petits que le texte (16 px) ; étiquettes en capitales espacées ; texte secondaire à 70 % d'opacité ; graphiques en 10-11 px dans une police non chargée (Inter) | Échelle de 8 variables CSS appliquée partout (§6.4), police de l'application dans les graphiques, capitales retirées ; tests `TestEchelleTypographique` |
| I28 | Retour utilisateur du 11/10/2026 : phrases d'introduction et de méthode redondantes sur « Mon territoire » ; « Priorités du moment » mêlant baisses et retards sans repère visuel ; recherche d'arrêt absente de la carte et légende en une phrase ; classement des lignes non cliquable ; type de retard caché dans « Contexte » ; ponctualité par créneau au lieu du score ; graphiques réseau et mode en double entre « Quand ? » et « Réseau & modes » | Quatre priorités à icônes (`diagnostic.priorities`), recherche et légende de la carte, classement cliquable (`dashboard/graphique.py`), bandeau « Type de retard », score par créneau, indicateur et modes au choix dans « Quand ? », page « Modes de transport » unique (§11.3) ; tests `TestPriorites`, `TestCreneauxEtGroupes`, `TestIndicateursParGroupe`, `TestTerritoireEtFiches` |
| I29 | Deux scores coexistaient : le score de fiabilité (ponctualité ≤ 5 min − 2 × arrêts non desservis) partout, et le score 2.0 « en test » dans « Méthode », la fiche ligne et une page du rapport ; « à l'heure » comptait les départs très en avance ; les courses supprimées n'entraient pas dans le score ; les jours incomplets et les lignes au temps réel douteux n'étaient écartés que du score 2.0 ; la carte « Courses supprimées » de la fiche ligne ne comptait pas les mêmes courses que le score ; le rapport mêlait point et virgule décimale et tronquait le score de sa carte « Fiabilité » | Un seul score (section 10.3), calculé par `indicateurs.add_components` / `score_of` dans le dashboard, les rapports et l'open data ; « à l'heure » = −1 à +5 min partout ; exclusions appliquées à tous les indicateurs ; `agg_hourly(_stop).cnt_lt60` et son rattrapage ; décomposition exacte des points perdus ; nombres du rapport à la française ; tests `TestComposantesPartagees`, `TestDepartsEnAvanceParHeure`, `TestExclusionsDuRapport`, `TestRegles`, `test_le_tableau_de_bord_et_la_methode_donnent_le_meme_score` |

### 26.4 Dette documentaire

- README racine refondu (synthèse + pointeur vers `docs/`).
- README `dashboard/` et `reports/README.md` supprimés : leur contenu est
  couvert par le présent document (sections 11 et 12).
- Unités systemd, vhost nginx, rotation des journaux et crontab de référence
  versionnés dans `deploy/` depuis le 01/10/2026 (sections 14, 15, 17 et 20.2).
- Intégration continue en place (`.github/workflows/tests.yml`) ; le contrôle
  `ruff` se limite aux erreurs bloquantes, sans formatage ni typage, et le dépôt
  n'a pas de `pyproject.toml`.
- Streamlit 1.60 signale deux API dépréciées utilisées par le dashboard :
  `st.components.v1.html` (rendu Highcharts, à remplacer par `st.iframe`) et
  `use_container_width` (à remplacer par `width`).
- Les modules se chargent par `sys.path` inséré au runtime
  (`reports/`, `src/scripts/`, `dashboard/`) sans packaging — fragile mais
  fonctionnel ; un regroupement en paquets installerables simplifierait les
  imports.

### 26.5 Recommandations

1. **Retards exactement nuls** (audit du 11/10/2026, copie de production,
   septembre 2026, à décider) : ils sont comptés comme des mesures dans la
   ponctualité, le retard moyen, les retards > 5 min, la carte des arrêts, les
   profils horaires et les rapports. Tram : 3,6 à 9,2 % de départs nuls
   (82 % viennent de trajets entiers sans temps réel) ;
   ponctualité surestimée de 0,6 à 1,9 point (ligne A : 81,3 % au lieu de
   79,4 %), retard moyen sous-estimé de 6 à 16 s ; réseau : 0,4 point de
   ponctualité, 0,3 point de score. La base de développement en compte bien
   plus (25 à 33 % pour le tram) : ne pas en tirer de conclusion. Deux règles
   comparées sur la ponctualité de septembre : (a) retirer chaque retard nul :
   réseau −0,37 point, tram −0,95, ligne A −1,90 ; (b) retirer seulement les
   trajets entièrement à zéro (sans temps réel) et garder les zéros isolés :
   réseau −0,21, bus −0,03, tram −0,77, ligne A −1,70. 82 % des zéros du tram
   sont dans des trajets entiers, 16 % de ceux des bus.
2. **Harmoniser la gestion des trous de collecte** entre dashboard, rapports
   et analyse (définition unique, désormais mesurable par `collection_runs`) ou
   documenter explicitement la divergence (déjà décrite en 7.3/23.2).
3. **Planifier une politique de rétention** de `observations` (table
   volumineuse) et un VACUUM périodique, avec tests associés.
4. **Copier les sauvegardes hors de la VM** (section 6.6) : sans cela, elles
   ne protègent pas d'une perte du disque.
5. Étendre l'intégration continue : règles `ruff` complètes, typage
   (`pyright`), contrôles d'accessibilité.
6. **Vérifier la carte de la veille des visiteurs** (`veille_visiteurs.py`,
   §17.2) : elle utilise les tuiles raster Carto
   `https://{s}.basemaps.cartocdn.com/light_all/...`, qui renvoyaient le
   01/10/2026, depuis le poste de développement, une image « API KEY
   REQUIRED ». Si c'est aussi le cas en production, passer au style
   vectoriel Positron (comme la carte du dashboard) ou à une clé Carto.
7. **Tester la vue rapide auprès de 3 à 5 lecteurs non analystes** (agents ou
   élus, proches non spécialistes) avant tout nouvel écran, sur des tâches
   précises (« trouver la ligne la plus problématique de ma commune »,
   « dire pourquoi », « retrouver le rapport du mois dernier »), sans les aider,
   en notant où ils bloquent.
8. **Prendre une licence Highcharts ou passer à Apache ECharts** avant tout
   usage commercial du service (§23, point 15).
9. **Prendre un nom de domaine propre** quand le projet est présenté aux
   communes (procédure en section 15).

---

*Fin de la documentation technique. Toute correction ou mise à jour doit
rester factuel et à jour ; toute information incertaine est signalée dans la
section 26.*