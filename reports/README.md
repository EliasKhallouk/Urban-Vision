# Rapports mensuels

Le rapport place une **synthèse exécutive d'une page** en tête : indicateurs clés, évolution par rapport au mois précédent et trois alertes prioritaires. Le détail par ligne, les graphiques de fiabilité, la carte de risque, le profil horaire, la distribution des retards et le détail par arrêt sont placés en annexe.

## Deux entrées, un moteur

Trois scripts se partagent le travail :

| Script | Rôle |
|---|---|
| **`generate_single_report.py`** | Un rapport à la fois : réseau entier (`--network`) ou une commune (`--commune "Mérignac"`). |
| **`generate_all_reports.py`** | Tous les rapports en une commande (réseau + chaque commune), un dossier par rapport. |
| `generate_monthly_report.py` | **Moteur interne** (toute la logique). Ne pas l'appeler directement. |

### Rapport unique — réseau ou commune

```bash
.venv/bin/python reports/generate_single_report.py --month 2026-07 --network --compile
.venv/bin/python reports/generate_single_report.py --month 2026-07 --commune "Mérignac" --compile
```

### Tous les rapports (réseau + communes)

```bash
.venv/bin/python reports/generate_all_reports.py --month 2026-07 --compile
```

`--compile` génère puis lance automatiquement `compile_all.sh` (xelatex), en
fournissant les PDF. Le script reste conservé pour un re-lancement manuel :
`bash reports/output/2026-07/compile_all.sh`.

Structure de sortie (chaque rapport dans son dossier, pas de collision des PNG) :

```
reports/output/<mois>/
├── reseau/bordeaux-metropole/…
├── communes/merignac/…
├── communes/bordeaux/…
└── compile_all.sh   (si --compile)
```

## Seuils et catégories utilisés

### Message exécutif (première phrase de la synthèse)

La phrase d'ouverture s'adapte à la ponctualité globale du périmètre (commune ou réseau) :

| Ponctualité | Message |
|---|---|
| ≥ 95 % | « affiche une ponctualité excellente (X %). » |
| ≥ 90 % | « enregistre un bon niveau de ponctualité (X %). » |
| ≥ 85 % | « présente une fiabilité correcte (X %), encore perfectible. » |
| ≥ 80 % | « montre une fiabilité intermédiaire (X %). » |
| ≥ 75 % | « connaît des difficultés de ponctualité notables (X %). » |
| ≥ 65 % | « enregistre une ponctualité insuffisante (X %). » |
| < 65 % | « subit des retards critiques (X % de passages à l'heure). » |

Si le taux d'arrêts sautés dépasse 5 %, une phrase complémentaire l'indique.

### Couleurs des indicateurs KPI

Chaque KPI évaluatif du haut de page est coloré selon sa valeur, via les seuils partagés du module `palette.py` (appliqués à l'identique dans les graphiques et le dashboard) :

| Métrique | Positif (bonne performance) | Moyen (intermédiaire) | Négatif (dégradé) |
|---|---|---|---|
| **Fiabilité, Ponctualité** | ≥ 80/100 | 50 – 80 | < 50 |
| **Retard moyen / médian** (valeur absolue) | ≤ 60 s | 60 – 180 s | > 180 s |
| **Arrêts sautés** | ≤ 5 % | 5 – 15 % | > 15 % |

Les couleurs LaTeX utilisées : `olive` (positif), `sunlitclay` (moyen), `alert` (négatif), `blackforest` (marque : KPI non évaluatif « Passages analysés », titres).

### Score de fiabilité

\[
\text{Score} = \max(0,\; \text{Ponctualité} - 2 \times \text{Taux d'arrêts sautés})
\]

- **Ponctualité** : part des passages avec un retard de départ ≤ 5 min (0–100 %).
- **Taux d'arrêts sautés** : part des passages marqués `SKIPPED` parmi les passages programmés (0–100 %).
- Le score est borné entre 0 et 100. Plus il est bas, plus la ligne est prioritaire.

### Comparaison réseau (rapports communaux uniquement)

Chaque valeur affichée est accompagnée de la valeur **Réseau TBM global** pour la même ligne et la même période, en petit texte gris. Cela permet de relativiser la performance locale par rapport à la moyenne du réseau.

## Rapport réseau

Utilisez l'entrée unique :

```bash
.venv/bin/python reports/generate_single_report.py --month 2026-07 --network --compile
```

Sans `--month`, le dernier mois présent dans la base est choisi. Le script produit toujours un fichier `.tex`; l'option `--compile` produit aussi un PDF si `xelatex`/`lualatex` est installé.

## Version destinée à une commune

Les rapports territoriaux sont filtrés sur les **arrêts réellement situés dans la commune**, et non sur les lignes : une ligne peut traverser plusieurs villes. Calculez d'abord le rattachement géographique, à partir des coordonnées GTFS et des limites communales officielles :

```bash
.venv/bin/python src/scripts/assign_stop_municipalities.py
```

Si vous travaillez avec des profils nommés, éditez le modèle puis utilisez le **moteur** directement (cas avancé) :

```bash
cp reports/recipients.example.json reports/recipients.json
# éditer communes et description du profil concerné
.venv/bin/python reports/generate_monthly_report.py --month 2026-07 --profile mairie_exemple --compile
```

Pour la version ponctuelle, préférez l'entrée unique :

```bash
.venv/bin/python reports/generate_single_report.py --month 2026-07 --commune "Mérignac" --compile
```

Les rapports générés sont placés dans `reports/output/`, qui est volontairement ignoré par Git. L'envoi doit rester une étape séparée et validée manuellement avant diffusion.
