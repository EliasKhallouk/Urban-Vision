# Dashboard Urban Vision

Visualisation des données collectées dans [data/urban_vision.db](/home/elias/PROJECT/Urban-Vision/data/urban_vision.db).
Charte graphique « Urban Vision » alignée sur les rapports mensuels :
Black Forest `#283618` (marque, titres), Olive Leaf `#606c38` (texte secondaire),
Sunlit Clay `#DDA15E` (bordures), Cornsilk `#FEFAE0` (fond de page).
Les couleurs de performance utilisent les seuils partagés de `reports/palette.py`
(sans dégradé continu) : positif = Olive Leaf `#606c38`, moyen = Sunlit Clay
`#DDA15E`, négatif = Copperwood `#bc6c25`.

## 1) Installer la dépendance

```bash
pip install streamlit
```

(`pandas` est déjà utilisé par le projet.)

## 2) Lancer le dashboard

```bash
streamlit run dashboard/app.py
```

## Onglets

- **Vue réseau** : KPIs réseau, classement par score de fiabilité, carte de risque
  (retard **médian** par **mode de transport**), évolution quotidienne, risque horaire,
  distribution des retards et détail des lignes.
- **Modes de transport** : comparaison Tramway / Bus / Ferry (ponctualité, arrêts sautés,
  profil horaire et évolution quotidienne par mode).
- **Analyse d'une ligne** : score, retard médian, évolution quotidienne, risque horaire,
  distribution des retards.
- **Alertes** : alertes actives diffusées par TBM (Service Alerts GTFS-RT).
- **Collecte des données** : volume et continuité de la collecte (par minute et par heure).
- **Méthode & données** : explication des indicateurs et de la fenêtre d'analyse.

## Définitions

- **Observation (brute)** : toute ligne reçue du flux GTFS-RT TripUpdates
  (SCHEDULED, SKIPPED ou NO_DATA) — sert à mesurer le volume de collecte.
- **Passage analysé** : une observation `SCHEDULED` avec retard connu, sortie du flux
  depuis ≥ 20 min. C'est la définition utilisée pour tous les indicateurs de ponctualité.
- **Arrêts sautés** : arrêts annoncés `SKIPPED` rapportés aux arrêts attendus
  (`SCHEDULED` + `SKIPPED`). Les arrêts sautés n'ont pas de retard connu et ne sont donc
  pas des « passages analysés » : les diviser par les passages analysés
  sous-estimerait le taux d'arrêts sautés.
