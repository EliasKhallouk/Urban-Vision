---
name: urban-vision-design
description: Design system et règles de charte graphique d'Urban Vision. À consulter avant toute modification touchant les couleurs, graphiques, KPIs ou composants visuels du dashboard Streamlit ou des rapports PDF.
---

# Design system Urban Vision

## Palette
- Couleur de marque : #283618 (Black Forest) — headers, éléments identitaires
- Fond de page : blanc — jamais de fond sombre (contrainte coût d'impression des rapports PDF)
- Accent (usage ponctuel uniquement, jamais comme couleur de score) : #FEFAE0 (Cornsilk)
- Logo : noir et blanc pur, indépendant de la palette

## Score de fiabilité — système à 3 paliers FIXES (pas de dégradé continu)
- Positif : #606c38 (Olive Leaf)
- Moyen : #DDA15E (Sunlit Clay)
- Négatif : #bc6c25 (Copperwood)
Ces 3 couleurs sont fixes et discrètes, jamais un gradient continu entre elles.

## Différenciation des modes de transport
Ne pas coder tram/bus/ferry uniquement par couleur (risque daltonisme,
impression N&B). Utiliser une forme de marqueur distincte par mode, en plus
de la couleur de score.

## Règles générales
- Toute couleur de score doit être calculée par une fonction centralisée
  unique (pas de couleurs codées en dur dispersées dans le code)
- "Urban Vision" est le nom du projet (ex-"Vigie TBM") — TBM ne doit
  apparaître que comme mention factuelle de la source de données
  (ex. "flux GTFS-RT TBM"), jamais comme nom du projet
