---
name: urban-vision-visual-quality
description: Principes de qualité visuelle et de direction artistique pour Urban Vision. À consulter avant toute modification de mise en page, typographie ou structure visuelle du dashboard Streamlit ou des rapports PDF — en complément du skill urban-vision-design qui fixe la palette.
---

# Qualité visuelle Urban Vision

## Le piège à éviter : l'esthétique "générée par IA"
Repère et évite activement ces travers génériques, qu'ils soient dans le
dashboard Streamlit ou le rapport PDF :
- Labels en MAJUSCULES avec letter-spacing pour des petites étiquettes
- Cartes identiques toutes avec le même border-radius et la même ombre grise,
  quelle que soit la hiérarchie de l'information
- Un seul mot accentué en gras/italique/couleur dans un titre
- Des libellés construits avec un tiret cadratin ("Ponctualité — vue réseau")
- Markers numérotés (01 / 02 / 03) sur du contenu qui n'est pas réellement
  une séquence/un process
- Une police monospace pour des petites valeurs chiffrées, sans raison
  fonctionnelle (pas de code à afficher)
- Animation/transition hover ajoutée sur chaque carte par défaut (dans le
  dashboard) sans rôle précis

## Principes à appliquer
- **La structure visuelle doit porter du sens** : une bordure, une icône, un
  séparateur doivent encoder une information réelle (ex. ⚠ = ligne perturbée),
  jamais de la pure décoration.
- **Un seul élément porte l'audace visuelle à la fois** — le reste reste
  sobre et discipliné. Ne pas distribuer l'effet "waouh" partout.
- **Typographie** : une ou deux familles maximum, hiérarchie claire par
  poids/taille plutôt que par couleur ou gras isolé. Lignes de texte courtes
  (< 80 caractères) pour la lisibilité.
- **Rédaction orientée utilisateur final** (l'élu, pas le développeur) :
  verbes actifs, vocabulaire simple, jamais le jargon technique du système
  ("score de fiabilité" plutôt que "KPI agrégé"). Un bouton qui dit "Voir le
  détail" doit mener à un écran dont le titre reprend "Détail", pas un autre
  mot — la cohérence du vocabulaire guide la navigation.
- **Avant toute modification de mise en page** : proposer d'abord un plan
  court (palette déjà fixée par urban-vision-design ; typographie ; structure
  de la page ; ce qui rend CET écran différent d'un dashboard générique) et
  le comparer à ce qu'un générateur produirait par défaut pour un dashboard
  de transport public — si ça ressemble au générique, revoir avant de coder.
- **Restreindre le mouvement** (Streamlit) : une seule séquence d'apparition
  soignée au chargement plutôt que des transitions sur chaque élément.
  Le PDF n'a pas d'animation — la hiérarchie doit se lire à l'œil nu, à plat.

## Spécificités selon le support
- **Dashboard (Streamlit, interactif)** : priorité à la lecture rapide par un
  non-expert ; la densité d'info peut être progressive (résumé d'abord,
  détail au clic).
- **Rapport PDF (LaTeX/matplotlib, imprimé)** : tout doit se comprendre sans
  interaction ; la hiérarchie typographique et les couleurs de score portent
  seules la lecture. Pas de gris/ombre décorative inutile vu le coût
  d'impression (fond blanc imposé).
---