---
name: urban-vision-design
description: Design system et règles de charte graphique d'Urban Vision. À consulter avant toute modification touchant les couleurs, graphiques, KPIs ou composants visuels du dashboard Streamlit ou des rapports PDF.
---

# Design system Urban Vision

Source unique des couleurs et des seuils : `reports/palette.py`. Aucune couleur
codée en dur ailleurs (CSS, Highcharts, cartes deck.gl) : on importe un jeton.
Détail complet : `docs/DOCUMENTATION_TECHNIQUE.md` § 6.4.

## Deux chartes, un seul module
- **Rapports PDF** : trois paliers fixes (`HEX`, `LATEX`), Black Forest #283618,
  Olive Leaf #606c38, Sunlit Clay #DDA15E, Copperwood #bc6c25, Cornsilk #FEFAE0.
  Ne pas y toucher sans accord explicite.
- **Dashboard** : charte écran « Pierre et vin », 10 couleurs.

## Charte écran « Pierre et vin » (dashboard)
| Jeton | Hex | Rôle |
|---|---|---|
| `INK` | #2B2523 | texte, titres, séries neutres, évolutions |
| `INK_MUTED` | #6A5F58 | texte secondaire, axes |
| `SURFACE` | #F6F1EA | menu, encadrés |
| `LINE` | #E5DCD0 | bordures, grilles |
| `ACTION` | #351E8F | tout ce qui se clique (liens, boutons, menu actif) et rien d'autre |
| `GARONNE` | #7FAAA6 | repère calme |
| `PIERRE` | #E1C17E | repère « un peu » |
| `VIN` | #B1437A | repère « beaucoup / problématique » |
| `LIE` | #602D24 | repère extrême, cas vraiment rares |
| `SCREEN_NO_DATA` | #D6D6D2 | pas assez de données, toujours en pointillé |

- Fond de page blanc. Pas de mode sombre.
- Logo : `urban-vision-logo-color.png` dans le menu, blanc sur la couverture des PDF.
- Couleurs proches de TBM (cyan, bleu pétrole, corail, magenta) et de Keolis
  (orange, violet, cyan, marine) interdites : l'outil ne doit pas ressembler à
  l'exploitant.

## Échelle continue à repères FIXES
- Couleur d'un score : `score_color(score)` (repères 80, 60, 50, 35).
- Couleur d'un tronçon : `troncon_color(s, pct)` = `scale_color(retained_seconds(s, pct))`
  (repères 0, 15, 40, 60 s).
- Les repères ont été fixés une fois pour toutes par l'analyse de la
  distribution sur la rentrée 2026 (`ANCHORS_REFERENCE`). Ne pas les recalculer
  par période : une couleur doit vouloir dire la même chose d'un mois à l'autre.
- La couleur ne contredit jamais le palier : fiable = calme pur, problématique
  ou « beaucoup » = vin ou plus foncé. Les paliers (`tier`, `troncon_tier`,
  seuils 80/50, règle (b) 15 s et 25 %, 40 s et 50 %) restent les règles de
  mesure et ne changent pas.
- Autres mesures à paliers (retards, part > 5 min, arrêts non desservis) :
  `screen_hex(value, kind)`, trois couleurs.
- Légendes : barre du dégradé avec les repères écrits en secondes (tronçons)
  ou en points (scores), jamais le nom d'une couleur.

## Règles
- Texte sur une couleur d'échelle : toujours `ink_on(couleur)`.
- Une évolution n'a jamais de couleur d'état : `evolution_words` (« ▼ 0,8 point »).
- Gris = pas assez de données, jamais vert. Tram hachuré (pointillé) sur la carte des tronçons.
- Les modes se distinguent par la forme (● tram, ■ bus, ▲ ferry), jamais par la couleur.
- Taille constante des arrêts : on ne connaît pas la fréquentation.
- Tout nouveau jeu de couleurs se vérifie : contraste (WCAG), écart CIELAB ≥ 20
  entre repères successifs en vision normale, deutéranopie, protanopie et
  tritanopie.
- "Urban Vision" est le nom du projet (ex-"Vigie TBM") ; TBM n'apparaît que
  comme source des données (« flux GTFS-RT TBM »).
