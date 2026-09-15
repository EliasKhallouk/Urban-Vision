# Urban Vision — Documentation technique

| | |
|---|---|
| **Projet** | Urban Vision |
| **Version du document** | 1.1 |
| **Date** | 2026-09-14 |
| **Commit de référence** | `13cf796` (branche `main`) |
| **Auteur d'origine** | Elias Khallouk (eliaskhallouk@gmail.com) |
| **Licence / dépôt** | https://github.com/EliasKhallouk/Urban-Vision |
| **Périmètre** | Dépôt local **ET** environnement de production (VM Oracle Cloud) |

Ce document décrit l'état **réel** du projet au 14/09/2026. Chaque information
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
   (histogrammes JSON des délais pour une médiane exacte). Le collecteur
   rafraîchit les agrégats toutes les 300 s (incrément sur hier/aujourd'hui) ;
   le dashboard les reconstruit intégralement s'ils sont vides/incomplets.
4. **Analyse quotidienne** : `analyze.py` calcule les statistiques par ligne
   (buffer de stabilisation 20 min, exclusion des trous de collecte) et les
   écrit dans `daily_line_stats` (usage historique, non lue par le dashboard).
5. **Visualisation** : `dashboard/app.py` (Streamlit) lit les tables agrégées
   et dessine les graphiques Highcharts (`dashboard/highcharts.py`), palette et
   seuils partagés via `reports/palette.py`.
6. **Rapports** : `reports/generate_monthly_report.py` (moteur) produit un
   rapport LaTeX par périmètre ; `generate_single_report.py` / `generate_all_reports.py`
   orchestrent les générations ; les PDF sont compilés avec **xelatex**.

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
├── .gitignore
├── .streamlit/
│   └── config.toml                 # thème + serveur 127.0.0.1:8501
├── apt-requirement.txt             # dépendances système (TeX + fonts)
├── assets/logo/
│   ├── urban-vision-logo-bw.png
│   ├── urban-vision-logo-color.png
│   └── urban-vision-logo-white.png # utilisé par le dashboard et les rapports
├── dashboard/
│   ├── app.py                      # dashboard Streamlit (1596 lignes)
│   └── highcharts.py               # configs Highcharts (charte partagée)
├── data/                           # GITIGNORÉ (100 %)
│   ├── urban_vision.db             # base SQLite (~749 Mo en dev)
│   ├── urban_vision.db-wal         # journal WAL SQLite (~39 Mo)
│   ├── urban_vision.db-shm
│   ├── collect.log                 # logs du collecteur
│   ├── alerts.log                  # logs du collecteur d'alertes
│   └── dashboard.log               # logs Streamlit — local/dev uniquement
├── docs/
│   └── DOCUMENTATION_TECHNIQUE.md  # documentation technique (ce document)
├── pytest.ini                      # testpaths=tests, addopts=-q
├── README.md                       # README racine (synthèse + pointeur docs/)
├── reports/
│   ├── generate_all_reports.py     # réseau + toutes les communes
│   ├── generate_monthly_report.py  # MOTEUR (rapport LaTeX/PDF, 1111 lignes)
│   ├── generate_single_report.py   # un rapport (réseau OU commune)
│   ├── palette.py                  # palette + seuils partagés (source unique)
│   ├── recipients.example.json     # profils de destinataires (modèle)
│   ├── plan/                       # 27 PDF de plans de réseau/communes (littéraux)
│   └── output/                     # GITIGNORÉ — résultats (2026-07, 2026-08, …)
├── requirements.txt               # dépendances Python épinglées (42 lignes)
├── requirements-dev.txt           # pytest
├── src/
│   ├── scripts/
│   │   ├── analyze.py              # bilan quotidien par ligne
│   │   ├── assign_stop_municipalities.py  # rattachement arrêts↔communes
│   │   ├── collect.py              # collecteur TripUpdates (60 s)
│   │   ├── collect_alerts.py       # collecteur ServiceAlerts (120 s)
│   │   ├── db.py                   # schéma SQLite + agrégats (source unique)
│   │   ├── export_open_data.py     # export CSV open data (lecture seule)
│   │   └── gtfs_static.py          # chargement routes/stops
└── tests/                          # 16 fichiers, 189 tests pytest
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
décodage protobuf, `protobuf` (7.35.1), `pydeck` (0.9.3) pour la carte,
`pyarrow` (24.0.0), `GitPython` (3.1.59). Le reste est des dépendances
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
.venv/bin/python -m pytest        # 189 tests (config : pytest.ini, -q)
```

Les tests n'utilisent aucune donnée réelle : bases SQLite temporaires
(`tmp_path`) + flux synthétiques (`tests/gtfs_factory.py`).

### 5.7 Installation des services en production

La procédure d'installation (contenu exact des unités en section 14) :

1. Créer les 3 fichiers d'unité systemd dans `/etc/systemd/system/` :
   `urban-vision-collect.service`, `urban-vision-collect-alerts.service`,
   `urban-vision-dashboard.service` (contenu **non versionné**, cf. section 14).
2. `sudo systemctl daemon-reload`
3. `sudo systemctl enable` puis `start` chacune des unités.
4. Vérifier : `sudo systemctl status urban-vision-collect.service
   urban-vision-collect-alerts.service urban-vision-dashboard.service`.

---

## 6. Configuration

Le projet **ne lit pas de `.env`** (aucun `os.getenv` dans le code). La
configurable via fichiers :

### 6.1 `.streamlit/config.toml`

```toml
[theme]
base = "light"
primaryColor = "#283618"
backgroundColor = "#FEFAE0"
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
| `POLL_INTERVAL_SECONDS` (TripUpdates) | 60 s | `collect.py:25` |
| `POLL_INTERVAL_SECONDS` (ServiceAlerts) | 120 s | `collect_alerts.py:24` |
| `GAP_THRESHOLD_SECONDS` | 180 s (3 × intervalle) | `collect.py:26` |
| `DB_BUSY_TIMEOUT_MS` (collect) | 120 000 ms | `collect.py:28` |
| `DB_BUSY_TIMEOUT_MS` (alertes) | 180 000 ms | `collect_alerts.py:27` |
| `AGGREGATE_REFRESH_INTERVAL_SECONDS` | 300 s | `collect.py:33` |
| `FRESHNESS_BUFFER_SECONDS` | 1200 s (20 min) | `analyze.py:17`, `generate_monthly_report.py:51`, `app.py:50` |
| `CACHE_TTL_SECONDS` (dashboard) | 60 s | `app.py:51` |
| `MIN_OBSERVATIONS` (dashboard) | 50 | `app.py:52` |
| `timeout` HTTP (collecte) | 15 s | `collect.py:48`, `collect_alerts.py:59` |
| `timeout` HTTP (gtfs statique) | 30 s | `gtfs_static.py:25` |
| Seuil « ponctuel » (retard ≤ 5 min) | 300 s | commun (SQL, rapport, palette) |
| Seuil « en avance > 1 min » | < −60 s | `db.py:231`, `analyze.py:66` |

### 6.4 Charte graphique et seuils (source unique : `reports/palette.py`)

Référence unique des couleurs **et** des seuils KPI, partagée par le dashboard,
les graphiques Highcharts et les rapports (via un ajout au `sys.path`).

Couleurs

| Nom | Hex | Usage |
|---|---|---|
| Black Forest | `#283618` | marque, titres, neutres |
| Olive Leaf | `#606c38` | positif |
| Sunlit Clay | `#DDA15E` | moyen |
| Copperwood | `#bc6c25` | négatif |
| Cornsilk | `#FEFAE0` | fond de page |
| White | `#FFFFFF` | — |
| Teal | `#2A6F6F` | couleur du mode bus |

Seuils (même valeur partout)

| Jeu de seuils | Positif | Moyen | Négatif |
|---|---|---|---|
| `score` (fiabilité/ponctualité) | ≥ 80/100 | 50–80 | < 50 |
| `retard` (en valeur **absolue**) | ≤ 60 s | 60–180 s | > 180 s |
| `pourcent` | ≤ 5 % | 5–15 % | > 15 % |

Modes de transport au dashboard (`app.py:133-138`) : `{0: Tramway, 2: Rail,
3: Bus, 4: Ferry, 5: Câble, 7: Funiculaire, 11: Trolleybus}` ; couleurs
`{0: Copperwood (tram), 3: Teal (bus), 4: Black Forest (ferry)}`.

---

## 7. Base de données

Système : **SQLite** (pas d'ORM ; `sqlite3` standard). Fichier `data/urban_vision.db`
(~749 Mo en dev local ; **~2,4 Go en production** au 14/09/2026) +
`-wal`/`-shm`. Mode **WAL** activé par les
collecteurs (`PRAGMA journal_mode=WAL;`) et par les tests ; le dashboard
n'active pas WAL lui-même mais émet `PRAGMA busy_timeout` (120 s) et
`cache_size=-65536`, `mmap_size=268435456`, `temp_store=MEMORY`
(`app.py:293-298`).

> En WAL, l'écrivain tient des verrous courts ; le collecteur (écrivain
> régulier, y compris le recalcul d'agrégats qui tient le verrou ~2 min sur une
> VM 1 vCPU) et le service d'alertes coexistent grâce aux `busy_timeout`
> élevés (120 s / 180 s). Un `rollback` après erreur est effectué côté alertes
> (`collect_alerts.py:143`) — comportement couvert par
> `tests/test_collect_alerts.py::TestRecuperationApresVerrou`.

### 7.1 Schéma (source unique de vérité)

Tout est défini dans `src/scripts/db.py::SCHEMA_DDL` (créé de manière
idempotente). Les tables `routes`/`stops` et `stop_municipalities`/`municipalities`,
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

**`daily_line_stats`** — statistiques quotidiennes par ligne (écrites par `analyze.py` ;
**non lues** par le dashboard ni les rapports — usage historique).
PK `(stat_date, route_id)`.

**`collection_gaps`** — trous de collecte `(gap_start, gap_end)` en epoch. Écrits
par `collect.py` quand l'écart entre deux succès dépasse 180 s. **Exclus**
de l'analyse (cf. 7.3).

**`trip_status`** — dernier statut connu par voyage. PK `(trip_id, start_date)`.

**`service_alerts`** — alertes du flux ServiceAlerts, une ligne par
(alerte × route informée × période). PK `(alert_id, route_id, active_period_start)`.
Colonne `cause` (entier protobuf) quasi toujours `UNKNOWN_CAUSE` — ignorée dans
le dashboard et les rapports.

**Tables agrégées** (calculées par `db.py::refresh_aggregates`)

| Table | Granularité | Colonnes clés |
|---|---|---|
| `agg_daily` | jour × ligne | `obs`, `sum_delay`, `cnt_le300`, `cnt_gt300`, `cnt_lt60`, `skipped`, `eligible`, `histogram` |
| `agg_hourly` | jour × ligne × heure | `obs`, `sum_delay`, `cnt_le300`, `cnt_gt300` |
| `agg_daily_stop` | jour × ligne × arrêt | idem `agg_daily` + `skipped`/`eligible` |
| `agg_hourly_stop` | jour × ligne × arrêt × heure | idem `agg_hourly` |

- `cnt_le300` : passages avec retard ≤ 300 s (« à l'heure »).
- `cnt_gt300` : passages avec retard > 300 s.
- `cnt_lt60` : passages en avance de plus de 60 s.
- `histogram` : JSON `{secondes_de_retard: effectif}` permettant de reconstruire
  une **médiane exacte** sur toute période (`app.py::_median_from_hists`).

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
- `idx_observations_sched_delay` (`schedule_relationship, departure_delay, last_seen_at, route_id`)
- `idx_service_alerts_period` (`active_period_start, active_period_end`)
- `idx_observations_departure_time` (`departure_time, schedule_relationship, departure_delay, route_id`)
- + index sur `agg_daily(date_service)`, `agg_hourly(date_service)`,
  `agg_daily_stop(date_service)` et `(stop_id)`, `agg_hourly_stop(date_service)` et `(stop_id)`
- `idx_stop_municipalities_commune` (sur `commune_name`, défini dans `assign_stop_municipalities.py`)

Le dashboard applique aussi en opportunité quelques index à la première
connexion (`app.py::INDEX_DDL`), erreurs avalées.

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
- `generate_monthly_report.py` : n'exclut pas les observations, mais **compte*
  les secondes de coupure et les observations écartées dans la section
  « Méthode » (grâce à `query_collection_gaps`) ;
- `app.py` (dashboard) : n'applique pas l'exclusion des trous (l'écart est
  visible sur la page « Collecte des données »).

---

## 8. Pipeline de données

Vue temporelle d'une journée-type :

```
06:00  ...  TBM publie les GTFS-RT (protobuf encodé, envoyé gzippé)
              │
 60 s        collect.py  ──► upsert observations / trip_status
 120 s       collect_alerts.py ──► upsert service_alerts
 300 s       collect.py  ──► refresh_aggregates(days=[hier, aujourd'hui])
              │
 à chaq. réexéc.    dashboard/app.py ──► lit agg_* (cache 60 s)
              │
 fin de mois  generate_all_reports.py ──► generate_monthly_report.py ──► xelatex ──► PDF
```

Détails des requêtes SQL d'agrégation (`db.py`)

- `_DAILY_SQL` / `_DAILY_STOP_SQL` : regroupent les `SCHEDULED` par
  (jour-service, ligne[, arrêt]), cumulent `obs`, `sum_delay`, compteurs ≤ 300 /
  > 300 / < −60 s, construisent l'histogramme JSON, et agrègent les `SKIPPED`
  pour donner `skipped`/`eligible` (jour-service dérivé de `start_date` au
  format `AAAAMMJJ`).
- `_HOURLY_SQL` / `_HOURLY_STOP_SQL` : même principe par heure locale
  (`strftime('%H', datetime(departure_time,'unixepoch','localtime'))`).
- `refresh_aggregates(days=None)` : **recalcul complet** (lent, ~minutes sur VM
  1 vCPU). `days=[...]` : **incrémental** — supprime puis recalcule les
  journées listées uniquement (c'est le mode utilisé par le collecteur pour
  hier et aujourd'hui).
- Jour-service : dérivé de `departure_time` (local) pour les délais, et de
  `start_date` pour les arrêts sautés ; deux clauses bornées
  (`::SCHED_BOUNDS::` / `::SKP_BOUNDS::`) paramétrées par le mode d'exécution.

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
  intervalle est inséré dans `collection_gaps` (+ warning log).
- Rafraîchissement des agrégats toutes les **300 s** sur les jours « hier » et
  « aujourd'hui » (recalcul exact, coût contrôlé ; un échec est seulement loggé
  en warning pour ne pas arrêter la collecte).
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

- La base est en mode **WAL** ; les recalculs d'agrégats gardent le verrou
  d'écriture quelques minutes sur VM 1 vCPU → intervalle d'agrégation porté à
  300 s (cf. commentaire `collect.py:29-33`) ; `busy_timeout` élevés des deux
  collecteurs.
- Les lectures du dashboard sont presque exclusivement sur les tables `agg_*`
  (petites) ; `observations` (grande table) n'est utilisée que sur la page
  « Collecte des données » (histogrammes minute par minute sur 7 jours,
  optimisés par index).

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

Cf. sections 7.1 et 8. Les valeurs réseau du dashboard sont issues d'une
lecture des `agg_*` puis de regroupements en mémoire :

- `app.py::_load_daily_core` → `agg_daily` (+ variante `agg_daily_stop`
  filtrée par `stop_municipalities` pour la vue territoriale) ;
- `app.py::_group_daily_stop_to_route` : la vue territoriale par arrêt est
  regroupée au niveau ligne (sommes + fusion des histogrammes JSON) ;
- `app.py::_daily_to_network` + `make_ranking` : calcule
  `pct_arrets_sautes` (skipped/eligible), puis
  `score_fiabilite = max(0, pct_a_l_heure − 2 × pct_arrets_sautes)`, le mode
  et sa couleur, et trie par score croissant (du plus prioritaire au meilleur).
- Médiane : reconstruite à partir des histogrammes JSON (`_median_from_hists`).

### 10.3 Méthode de la fiabilité (formule centrale)

```
Score de fiabilité = max(0 ; Ponctualité − 2 × Taux d'arrêts sautés)
```

- **Ponctualité** : % de passages avec retard ≤ 5 min (300 s).
- **Taux d'arrêts sautés** : `SKIPPED / (SCHEDULED + SKIPPED)` (en %).
- Un score faible = ligne prioritaire à corriger. Seuils de lecture : ≥ 80
  (positif), 50–80 (moyen), < 50 (négatif).

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

Navigation par `st.radio` dans la sidebar (pas d'onglets natifs). Ordre :

1. **Vue territoriale** (`app.py:1513`) — carte pydeck
   (`pdk.ScatterplotLayer`, fond « light ») des arrêts par commune, filtre
   « Territoire » en haut à droite, tableau des arrêts (retard médian, passages,
   direction…). En périmètre « Réseau complet », un bloc **Comparaison des
   communes** (loader `load_commune_stats`, agrégé depuis `agg_daily_stop`) :
   classement par score (même formule que les lignes : ponctualité ≤ 5 min
   pénalisée par les arrêts sautés), graphique `commune_ranking_chart` et
   tableau détaillé (ponctualité, retards > 5 min, retard moyen, arrêts sautés,
   lignes, passages).
2. **Vue réseau** (`1580`) — KPI band (5 cartes) + classement des lignes
   (barres, top 15), carte de risque (retard médian × retards > 5 min, bulles
   par mode), série quotidienne « retards > 5 min », colonnes du risque horaire,
   distribution des retards (11 classes), tableaux détaillés.
3. **Modes de transport** (`1649`) — comparaison d'indicateurs par mode
   (ponctualité, > 5 min, en avance, arrêts sautés), profil horaire par mode,
   évolution quotidienne par mode.
4. **Fiabilité par période** (`1697`) — fiabilité selon le créneau (jour de
   semaine × tranche horaire) : Matin 06–10, Journée 10–16, Pointe du soir
   16–20, Soirée & nuit 20–06 (lundi–vendredi) et Week-end (samedi + dimanche).
   Charge `agg_hourly` (jamais la table brute) via les loaders
   `load_period_stats` (métriques par créneau), `load_period_mode` (retards
   > 5 min par mode × créneau) et `load_period_lines` (classement des lignes
   d'un créneau, seuil `MIN_OBSERVATIONS`). Le classificateur
   `_period_labels` déduit le jour de semaine de `date_service`. Les arrêts
   sautés ne sont pas décomptés (absents de `agg_hourly`) — c'est mentionné
   dans la note de la page.
5. **Analyse d'une ligne** (`1745`) — sélecteur de ligne, timeline quotidienne,
   risque selon l'heure, profil des retards, tableau d'arrêts.
6. **Évolution & tendances** (`1791`) — suivi de la fiabilité dans le temps :
   la période sélectionnée est partagée en deux moitiés de durée égale et la plus
   récente est comparée à la précédente. Charge `agg_daily` (ou `agg_daily_stop` en
   périmètre commune) via `load_engagement_trend` (série quotidienne du réseau :
   ponctualité, retards > 5 min, arrêts sautés, retard moyen) et
   `load_engagement_progression` (score de fiabilité de chaque ligne sur les deux
   moitiés, seules les lignes ≥ `MIN_OBSERVATIONS` sur chacune). Graphiques
   `engagement_trend_chart` (avec moyenne glissante 7 jours) et
   `engagement_progression_chart`. C'est un indicateur de tendance au regard des
   engagements de service annoncés : aucun seuil d'engagement chiffré externe n'est
   retenu.
7. **Perturbations** (`1873`) — alertes actives à l'instant courant +
   historique (dédupliqué : une même annonce peut être publiée sous plusieurs
   `alert_id`) ; indication explicite que l'alerte n'implique **pas** de
   causalité démontrée avec les statistiques.
8. **Collecte des données** (`1935`) — totaux bruts (observations, trajets,
   lignes, stabilisées), graphique « observations/min » sur 7 jours glissants
   (Highcharts Stock, zoom), répartition horaire.
9. **Méthode & données** (`1971`) — définitions, seuils, sources, mention de la
   stabilisation 20 min, et bloc **Données ouvertes** : boutons de
   téléchargement CSV de la période (loader `load_open_dataset`, qui passe par
   `src/scripts/export_open_data.py` — voir §11.6).

Sélecteur de période : menu popover « Grafana-style »
(`time_range_picker`) avec presets relatifs (1/7/30/90 jours, « tout »),
périodes relatives personnalisées (jours/semaines/mois) et dates absolues
`Du`/`Au`. Valeurs initiales : 7 jours par défaut.

Top bar persistante : identité, filtre « Territoire » (communes issues de
`stop_municipalities`), sélecteur de période.

### 11.4 Graphiques Highcharts (`dashboard/highcharts.py`)

Injection de HTML via `st.components.v1.html` : charge
`highstock.js` (et `highcharts-more.js` pour les bulles) depuis le CDN,
applique `LIGHT_THEME` (fonds blanc, bordures Sunlit Clay, texte Olive Leaf à
70 %). Fonctions : `ranking_chart`, `scatter_chart`, `network_daily_chart`,
`network_hourly_chart`, `commune_ranking_chart`, `mode_comparison_chart`,
`mode_daily_chart`, `mode_hourly_chart`, `period_punctuality_chart`,
`period_mode_chart`, `engagement_trend_chart`, `engagement_progression_chart`,
`timeline_chart`, `hourly_risk_chart`,
`delay_distribution_chart`, `collection_minutely_chart` (Stock), et
`hourly_distribution_chart`. Les couleurs par palier sont calculées par
`palette.hex(value, kind)` — cohérentes avec les rapports.

`delay_distribution_chart` colore chaque classe de retard par l'écart absolu
médian de la classe (ex. `+1 à +2` → 90 s → palier « retard ») ; seules les
3 couleurs de palier sont utilisées, pas de dégradés.

### 11.5 Dépendances externes du dashboard

- CDN Highcharts (JS) — requiert un accès Internet coté navigateur.
- Pydeck : fond de carte par défaut (fournisseur Carto/Mapbox via pydeck).
- Logo local `assets/logo/urban-vision-logo-white.png` (data-URI base64).
- **Aucun appel API ni `os.getenv`** dans `app.py`.

### 11.6 Export open data (`src/scripts/export_open_data.py`)

Lecture seule des tables d'agrégation (jamais la table brute) ; intervalles
demi-ouverts `[since, end)` sur `date_service`. Ecrit dans `data/open_data/`
des **CSV UTF-8 (BOM, séparateur virgule, en-tête stable)** plus un
`METADATA.json` (date de génération, bornes, nombre de lignes).

Quatre datasets :

| Dataset | Fichier | Contenu |
|---|---|---|
| `lignes_journalier` | `lignes-journalier.csv` | Par (date, ligne) : observations, retards moyen/médian (médiane exacte via histogramme), ponctualité ≤ 5 min, retards > 5 min, en avance, arrêts sautés, histogramme JSON |
| `arrets_journalier` | `arrets-journalier.csv` | Par (date, ligne, arrêt) : + nom, commune, direction, coordonnées |
| `horaire` | `horaire.csv` | Par (date, ligne, heure) : observations, retard moyen, ponctualité, retards > 5 min |
| `communes_journalier` | `communes-journalier.csv` | Par (date, commune) : code Insee, observations, retards, arrêts sautés, nombre de lignes |

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

---

## 12. Rapports mensuels

### 12.1 Vue d'ensemble

Trois scripts dans `reports/` :

| Script | Rôle |
|---|---|
| `generate_single_report.py` | Un rapport (réseau **ou** une commune) |
| `generate_all_reports.py` | Tout : réseau + chaque commune, un dossier par rapport, génère `compile_all.sh` si `--compile` |
| `generate_monthly_report.py` | **Moteur** — à ne pas appeler directement (mais rien ne l'interdit) |

`--compile` compile en PDF via **xelatex** (deux passages, `-interaction=nonstopmode
-halt-on-error`) puis nettoie `.aux`/`.log`. Sans `--compile`, seul le `.tex`
est écrit. Nom de sortie :
`urban-vision-<AAAA-MM>-<slug du destinataire>.{tex,pdf}`.

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
```

Structure de sortie :

```
reports/output/<AAAA-MM>/
├── compile_all.sh
├── reseau/bordeaux-metropole/urban-vision-<mois>-bordeaux-metropole-et-tbm.tex|pdf
├── communes/<slug-de-la-commune>/urban-vision-<mois>-mairie-de-<ville>.tex|pdf
└── (PNG des graphiques : reliability, risk_scatter, stops, evolution, hourly, distribution)
```

### 12.3 Moteur (`generate_monthly_report.py` : 1111 lignes)

- **Périmètre** : `Scope(recipient, routes, communes, description)`. En CLI :
  `--recipient`, `--routes` (séparées par virgules), `--communes`, `--profile`
  + `--recipients-file`. Validation des noms de communes contre
  `stop_municipalities` (sinon erreur explicite).
- **Mois** : `--month AAAA-MM` ou auto := dernier mois présent dans
  `observations` (max `departure_time`).
- **Interrogations** (`query_*`) : observations SCHEDULED/SKIPPED du mois (avec
  filtre lignes/communes et seuil de stabilisation de 20 min), stats par arrêt,
  évolution mensuelle, trous de collecte, alertes ServiceAlerts actives sur la
  période pour les lignes du périmètre.
- **KPIs** : `kpis()` calcule passages, ponctualité, retard moyen/médian, > 5 min,
  arrêts sautés + taux, **fiabilité**. `comparison()` calcule la variation
  vs mois précédent. Les rapports communaux comparent **aussi** la ligne au
  réseau (valeurs en olive « Réseau : … »).
- **Synthèse exécutive** : texte rédigé selon des seuils de ponctualité
  (≥95 % « excellente », ≥90 % « bon », ≥85 % « correcte », ≥80 %
  « intermédiaire », ≥75 % « notables », ≥65 % « insuffisante », <65 %
  « retards critiques »), complété si `skip_rate > 5 %`.
- **Sections du PDF** : couverture (logo, rapport, date, périmètre),
  synthèse exécutive (6 KPI colorés + évolution), alertes prioritaires, annexe
  résultats détaillés (longtable par ligne, triée par score croissant — les
  plus prioritaires en premier), annexe graphique (5 graphiques matplotlib Antialias),
  profil opérationnel (risque horaire, distribution), Infos trafic (page dédiée
  des ServiceAlerts, dédoublonnées par contenu :
  route × titre × période), méthode (formule, marge ± 60 s, trous de collecte,
  non-interférence des alertes travaux).
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

`xelatex` + `fonts-inter` (+ `texlive-lang-french`, `texlive-xetex`…). Sans
LaTeX installé, les `.tex` sont générés et une erreur explicite est levée à la
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
| `dashboard/app.py` | Dashboard Streamlit | `streamlit run dashboard/app.py` |
| `reports/generate_single_report.py` | Rapport unique | `--month` (obligatoire), `--commune` XOR `--network`, `--db-path`, `--output-dir`, `--compile` |
| `reports/generate_all_reports.py` | Tous les rapports | `--month` (obligatoire), `--db-path`, `--output-dir`, `--compile`, `--communes …` |
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

> **Les fichiers d'unité ne sont PAS dans git** (choix historique). Le contenu
> exact ci-dessous est celui des unités installées sur la VM de production ;
> les trois services sont **`active`**.

Trois unités installées dans `/etc/systemd/system/` :

`urban-vision-collect.service`

```ini
[Unit]
Description=Urban Vision - Collecte GTFS-RT en continu
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Urban-Vision
ExecStart=/home/ubuntu/Urban-Vision/.venv/bin/python src/scripts/collect.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
RestrictRealtime=true

[Install]
WantedBy=multi-user.target
```

`urban-vision-collect-alerts.service` : identique à la précédente (description
« Collecte ServiceAlerts en continu »), avec `ExecStart=.../collect_alerts.py`.

`urban-vision-dashboard.service` : description « Dashboard Streamlit »,
`ExecStart` =
`.venv/bin/python -m streamlit run dashboard/app.py --server.address=127.0.0.1 --server.port=8501`.
Le serveur Streamlit écoute donc **explicitement sur `127.0.0.1:8501`** —
inaccessible directement, exposé uniquement via nginx (section 15).

Durcissements communs aux 3 unités : `User=ubuntu`, `Restart=always` /
`RestartSec=10`, `NoNewPrivileges=true`, `PrivateTmp=true`,
`ProtectSystem=full`, `ProtectKernelTunables=true`,
`ProtectKernelModules=true`, `ProtectControlGroups=true`,
`RestrictAddressFamilies=AF_INET AF_INET6`, `RestrictRealtime=true`.

Commandes usuelles :

```bash
sudo systemctl daemon-reload
sudo systemctl enable  urban-vision-collect.service
sudo systemctl start   urban-vision-collect.service
sudo systemctl status  urban-vision-collect.service
# idem : urban-vision-collect-alerts.service, urban-vision-dashboard.service
# vue globale :
sudo systemctl status urban-vision-collect.service urban-vision-collect-alerts.service urban-vision-dashboard.service
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
- **`default` vhost** : le vhost de stock nginx sur :80 (page par défaut) —
  ne sert pas l'application.
- **Firewall** : le port **8501 est fermé** (cf. section 16) ; le dashboard
  n'est joignable que via nginx.
- **DuckDNS** : le nom est géré sur duckdns.org (enregistrement A vers l'IP
  de la VM). **Aucun mécanisme de mise à jour dynamique n'a été trouvé sur la
  VM** (pas de cron, timer, unité ou script DuckDNS) — cohérent avec une IP
  publique statique ; le token DuckDNS n'est pas présent sur la machine.

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
- **Secrets** : `.env` est gitignoré (permissions 600) et **absent de prod**
  (le code n'en utilise aucun).
- **Dépannage hors-bande** : console OCI de l'instance (`ek-hub` →
  Console connection) en cas de blocage réseau/système.
- Exposition : le dashboard étant public, les données qu'il affiche (retards,
  alertes) sont considérées publiques ; il n'y a ni authentification ni clé
  d'API applicative.

---

## 17. Logs

| Fichier | Écrit par | Contenu typique |
|---|---|---|
| `data/collect.log` | `collect.py` (FileHandler) | `Démarrage de la collecte Urban Vision (intervalle: 60s)`, `OK - <n> entités, <n> observations mises à jour`, warnings « Trou de collecte » / « Échec de récupération » |
| `data/alerts.log` | `collect_alerts.py` | `OK - <n> entites, <n> alertes mises a jour`, erreurs éventuelles + traceback |
| `data/dashboard.log` | Streamlit/Uvicorn — **dev local uniquement** | démarrage serveur, warnings Streamlit ; en production Streamlit journalise vers **journald** (pas de fichier) |

Observation : `data/collect.log` et `data/alerts.log` (gitignorés) contiennent
des **tracebacks historiques** issus d'exécutions et de versions antérieures du
code — l'actuel `collect.py` logge « Urban Vision » (ligne 153).

Lecture en direct :

```bash
tail -f data/collect.log
```

Les logs **ne tournent pas** : aucune règle logrotate dédiée, aucun cron ; seuls
les logs système globaux sont gérés par `logrotate.timer`. Volumes en
production : `collect.log` ≈ 6,2 Mo et `alerts.log` ≈ 5,6 Mo — volumes faibles,
mais la base principale, elle, est volumineuse (section 18.2).

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
  ~2,4 Go en production** au 14/09/2026, WAL ~76 Mo). Les agrégats sont
  reconstruits en incrémental ; la base n'est pas VACUUMed automatiquement.
- Opérations possibles (à planifier, à confirmer par tests) :
  `sqlite3 data/urban_vision.db "PRAGMA wal_checkpoint(TRUNCATE);"`,
  `VACUUM;` (nécessite ~la taille de la base en espace libre). Vérifier la
  politique de rétention souhaitée AVANT tout nettoyage.

### 18.3 Tests après modification

```bash
.venv/bin/python -m pytest
```

Suite complète 189 tests, sans réseau ni données réelles (fixtures bases
temporaires, flux synthétiques). Les zones sensibles à couvrir lors d'un
changement de schéma : `test_refresh_aggregates.py` (exactitude des agrégats),
`test_app_loaders.py` (requêtes du dashboard), `test_monthly_report.py`
(génération LaTeX).

### 18.4 Régénération du dashboard en cas de schéma

Le stockage `histogram` est utilisé pour les médianes ; en cas de changement du
format, penser à `refresh_aggregates(days=None)` (recalcul complet) une fois
via un Python shell ou le collecteur.

---

## 19. Sauvegarde et restauration

Il n'existe **aucun mécanisme de sauvegarde automatisé** sur la VM de
production : pas de `crontab` (utilisateur `ubuntu` ni root), pas de timer
systemd dédié, pas de script de sauvegarde. Seuls les **snapshots OCI** de
l'instance (console du cloud) ne sont pas accessibles depuis le système.

Base minimaliste recommandée (SQLite = un fichier) :

```bash
# Sauvegarde cohérente (WAL) :
sqlite3 data/urban_vision.db ".backup '/tmp/urban_vision_$(date +%F).db'"
# ou copie froide après vérification qu'aucun écrivain ne tourne :
# (WAL actif → ne PAS copier seulement le .db sans le .db-wal)
```

Restauration : remplacer `data/urban_vision.db*` par la sauvegarde puis
relancer les services. Si la base de production doit être reproduite sur le
poste local : le contenu de `data/` est **gitignoré** et n'est pas dans le
dépôt — un transfert manuel est nécessaire (rsync/scp).

La partie reproductible (code + config + profils) est, elle, dans git.

---

## 20. Git et déploiement

### 20.1 État et conventions

- Dépôt : `https://github.com/EliasKhallouk/Urban-Vision.git` (remote
  `origin`), branche unique `main` (suivie par `origin/main`).
- Historique récent (extrait, `git log --oneline`) : commits de type
  `feat:`, `fix:`, `docs:`, `chore:`, messages parfois en français
  (ex. `fix: escape external text against XSS and LaTeX injection`, « Ajout de
  tests pour… »).
- `.gitignore` : `data/*`, `__pycache__/`, `*.pyc`, `.venv/`, `reports/output/`,
  `reports/recipients.json`, `data/*.log`, `data/collect.log`, `.env`,
  `opencode.json`, `.vscode/`.

### 20.2 Déploiement (manuel)

Le déploiement est **manuel** : pas de CI/CD, pas de Docker, pas de Makefile.
Procédure documentée/observée :

1. Sur le poste de dev : `git add/commit/push` vers `main`.
2. Sur la VM (`ssh ek-hub`) :
   ```bash
   cd ~/Urban-Vision
   git pull
   source .venv/bin/activate
   pip install -r requirements.txt        # si des dépendances ont changé
   # initialisation base si nécessaire :
   python src/scripts/db.py
   sudo systemctl restart urban-vision-collect.service
   sudo systemctl restart urban-vision-collect-alerts.service
   sudo systemctl restart urban-vision-dashboard.service
   ```

### 20.3 Rapport de production

La génération des rapports mensuels peut s'effectuer sur la VM (où `xelatex`
est installé) :

```bash
.venv/bin/python reports/generate_all_reports.py --month AAAA-MM --compile
```

Sortie dans `reports/output/AAAA-MM/` (gitignoré). Les PDF finaux sont ensuite
servis/transmis manuellement.

---

## 21. Dépannage (troubleshooting)

| Symptôme | Cause probable | Actions |
|---|---|---|
| `sqlite3.OperationalError: database is locked` côté alertes | Recalcul d'agrégats du collecteur (verrou ~2 min) | C'est géré par `busy_timeout` (180 s) + `rollback`/reprise ; consulter `data/alerts.log`. Maigrir : `AGGREGATE_REFRESH_INTERVAL_SECONDS=300` |
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
`max(0 ; ponctualité − 2 × taux d'arrêts sautés)`, avec ponctualité = % de
passages à ≤ 5 min de retard (voir section 10.3).

**Le dashboard et les rapports utilisent-ils les mêmes chiffres ?**
Oui, à la granularité et au buffer près : même définition des passages
(stabilisation 20 min, seuil 300 s), même formule de score, palette et seuils
issus de `reports/palette.py`. Différences constatées : le dashboard n'exclut
pas les trous de collecte de ses agrégats (contrairement à `analyze.py`), voir
section 7.3.

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
3. **`daily_line_stats`** est écrite mais **jamais lue** par le dashboard ni
   les rapports (déjà obsolète).
4. **`cause` des ServiceAlerts quasi toujours `UNKNOWN_CAUSE`** : le champ est
   ignoré ; les alertes n'ont aucun lien causal établi avec les statistiques.
5. **Histogramme JSON** des agrégats : dépend d'une fonction
   `json_group_object` bien définie ; tout changement de format invalidera les
   recalculs incrémentaux (degré de liberté).
6. **Pas de sauvegarde automatisée, pas de rotation de logs** — non traité
   dans le dépôt.
7. **Le dashboard.js** dépend de CDN externes (Highcharts, pydeck) : hors
   ligne le site reste affiché mais certains graphiques/carte sont dégradés.
8. **Exécution directe** : `generate_monthly_report.py` est documenté comme
   « moteur interne » ; appelé directement, il fonctionne mais peu de garde-fous
   UX.

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
| **Arrêt sauté** | événement `SKIPPED` sur un arrêt prévu. |
| **Ponctualité** | % de passages avec retard ≤ 300 s. |
| **Score de fiabilité** | `max(0 ; ponctualité − 2 × taux d'arrêts sautés)`. |
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
- Chaîne d'agrégation et mode incrémental `refresh_aggregates(days=...)`.
- Formule du score, seuils de la synthèse exécutive, structure des PDF.
- CLI complète des 5 scripts et du moteur.
- Cache dashboard 60 s, buffer 20 min, views et loaders (noms de fonctions et
  lignes exacts fournis en annexe de la section 11).
- Tests : 189, isolés (suite `pytest` complète : 189 passed), flux synthétiques
  (`gtfs_factory`), fixtures `tmp_path`.
- Git : branche `main`, remote GitHub ; la production est synchronisée sur le
  commit `13cf796` (identique au dev).
- Environnement de production : unités systemd exactes (section 14), vhost
  nginx + cert Let's Encrypt (exp. 11/12/2026, renouvelé par `certbot.timer`),
  venv Python **3.12.14**, `xelatex` + fonts-inter présents ; aucune tâche cron,
  aucun logrotate dédié, aucune sauvegarde automatique, `reports/recipients.json`
  absent.
- `requirements.txt`, `requirements-dev.txt`, `apt-requirement.txt`,
  `pytest.ini`, `.streamlit/config.toml`, `.vscode/settings.json`, `.gitignore`.

### 26.2 Environnement de production

État relevé sur la VM de production le 14/09/2026 :

| # | Point | État en production |
|---|---|---|
| U1 | Contenu exact des 3 unités systemd | Intégral en section 14 ; les 3 services sont `active` ; dashboard = `--server.address=127.0.0.1 --server.port=8501` |
| U2 | Configuration nginx | Vhost `urban-vision` : 443 ssl → `proxy_pass 127.0.0.1:8501` (headers WebSocket), bloc :80 = 301 HTTPS (`$host` exact) sinon 404 ; vhost `default` de stock présent (page par défaut) |
| U3 | Sauvegarde / rotation | Aucune : pas de `crontab` (ubuntu ni root), pas de timer systemd dédié, pas de règle logrotate dédiée ; snapshots OCI non accessibles depuis le système (console du cloud) |
| U4 | Version Python | venv **3.12.14**, Python système **3.10.12** (dev local : 3.11.2) |
| U5 | URL de geocoding | Code prod identique au dev (`13cf796`) ; fallback API Adresse `https://api-adresse.data.gouv.fr/reverse/?` ; aucune trace d'appel dans les logs récents |
| U6 | `reports/recipients.json` | Absent sur la VM ; la génération mensuelle passe par `--network` / `--commune`, ou exige `--recipients-file` |
| U7 | `xelatex` + fonts | `/usr/bin/xelatex` et `/usr/bin/lualatex` présents ; 38 polices Inter installées (`fc-list`) |

### 26.3 Incohérences constatées (code vs docs vs logs)

Les trois incohérences de l'audit ont été corrigées le 14/09/2026 :

| # | Incohérence | Correctif appliqué |
|---|---|---|
| I1 | `DB_PATH` de `gtfs_static.py` et `analyze.py` pointait vers `src/data/` (`parents[1]`) | `parents[2]` (aligné sur `collect.py`) + 2 tests de chemin ajoutés (`tests/test_gtfs_static.py`, `tests/test_analyze.py`) |
| I3 | `src/sql/001_add_departure_time.sql` et `db.py` appliquaient la même `ALTER` | migration versionnée supprimée — `db.py` est l'unique mécanisme (PRAGMA + ALTER à l'import) |
| I4 | Aide CLI `--compile` : « pdflatex » | texte d'aide = `xelatex/lualatex` (`generate_monthly_report.py`) |

### 26.4 Dette documentaire

- README racine refondu (synthèse + pointeur vers `docs/`).
- README `dashboard/` et `reports/README.md` supprimés : leur contenu est
  couvert par le présent document (sections 11 et 12).
- Fichiers d'unité systemd et config nginx non versionnés → leurs contenus
  exacts sont désormais la trace consolidée (sections 14 et 15).
- Dépôt sans CI ni lint configuré (pas de `ruff`, pas de `pyproject.toml`).
- Les modules se chargent par `sys.path` inséré au runtime
  (`reports/`, `src/scripts/`, `dashboard/`) sans packaging — fragile mais
  fonctionnel ; un regroupement en paquets installerables simplifierait les
  imports.

### 26.5 Recommandations

1. **Versionner les fichiers systemd/nginx** (ex. dossier `deploy/`) : le
   contenu exact est désormais documenté (sections 14 et 15) ; un versionnement
   des fichiers rendrait le déploiement reproductible et auditable.
2. **Harmoniser la gestion des trous de collecte** entre dashboard, rapports
   et analyse (définition unique) ou documenter explicitement la divergence
   (déjà décrite en 7.3/23.2).
3. **Planifier une politique de rétention** de `observations` (table volumineuse)
   + sauvegarde périodique + VACUUM, avec tests associés.
4. Activer un lint/type-check minimal (`ruff`, `pyright`) et une CI GitHub
   Actions exécutant `pytest` — aucun des deux n'existe.

---

*Fin de la documentation technique. Toute correction ou mise à jour doit
rester factuel et à jour ; toute information incertaine est signalée dans la
section 26.*