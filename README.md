<div align="center">

# Urban Vision

**Observatoire indépendant de la fiabilité du réseau de transport TBM — Bordeaux Métropole**

</div>

Urban Vision est un projet de **données ouvertes au service de la mobilité** :
il mesure, en continu et de façon indépendante, la fiabilité effective du réseau
de transports en commun de Bordeaux Métropole (tram, bus, ferry), puis rend ces
informations visibles et accessibles.

Le projet s'appuie sur les **flux temps réel publiés par TBM** (retards,
avances, annulations, alertes travaux) pour construire un suivi objectif de la
ponctualité, au niveau du réseau, de chaque ligne et de chaque commune, mois
après mois.

## À quoi ça sert ?

- **Pour les collectivités** (mairies de la Métropole, Bordeaux Métropole) : un
  rapport mensuel par commune avec un indicateur unique de fiabilité, pour
  objectiver l'état du service sur leur territoire et suivre son évolution
  dans le temps — indépendamment des chiffres communiqués par l'exploitant.
- **Pour les acteurs de la mobilité** (associations d'usagers, élus,
  journalistes) : une donnée continue et publique là où n'existent
  aujourd'hui que des bilans annuels ou des chiffres ponctuels — de quoi
  appuyer un argumentaire avec des faits vérifiables.
- **Pour la transparence** : une mesure indépendante de l'exploitant,
  méthode documentée et reproductible, consultable librement par quiconque
  souhaite explorer les données par lui-même.

## Pourquoi une mesure indépendante ?

Le réseau TBM est exploité par Keolis, dans le cadre d'une délégation de
service public confiée par Bordeaux Métropole. Les indicateurs de
performance aujourd'hui disponibles proviennent essentiellement de bilans
annuels produits par l'exploitant lui-même. Urban Vision propose une mesure
continue et indépendante, construite directement à partir des flux temps
réel publics — sans dépendre de ce que l'exploitant choisit de publier, ni
quand.

## Fonctionnalités principales

- **Tableau de bord web public** : carte par commune où un clic sur un arrêt
  dessine le tracé des lignes qui le desservent, à la couleur de leur état, avec,
  pour chaque arrêt, une fiche qui explique d'où vient le problème (ligne en cause, retard né sur
  place ou venu de plus loin, heures et jours concernés) ; fiche de chaque
  ligne (où le retard se forme, arrêts non desservis, courses supprimées) ;
  créneaux et tendances ; réseau et modes de transport ; perturbations ;
  méthode et données ouvertes. Une **vue rapide**, par défaut, dit l'essentiel
  en mots (« fiable », « à surveiller », « problématique ») ; la **vue
  détaillée** ajoute les graphiques d'analyse. Un guide de lecture s'affiche à
  la première visite.
  → <https://urban-vision.duckdns.org>
- **Rapports mensuels PDF**, un pour le réseau, un pour chacune des
  28 communes de Bordeaux Métropole, téléchargeables dans le tableau de bord
  (page « Rapports mensuels »).
- **Indicateur clair** : un score de fiabilité par ligne, conçu pour être
  lisible par tous (pas seulement par les techniciens).
- **Données ouvertes** : collectées à partir des flux publics GTFS-RT de TBM,
  sans aucune donnée nominative.

## Pour aller plus loin

- 📚 **Documentation technique** (architecture, installation, production,
  maintenance) : [`docs/DOCUMENTATION_TECHNIQUE.md`](docs/DOCUMENTATION_TECHNIQUE.md)
- 🔧 **Prise en main pour développeurs** : une fois sur la machine, `docs/DOCUMENTATION_TECHNIQUE.md`
  (§ 5 Installation et § 13 Référence des scripts) donne toutes les commandes.
- 🧪 **Tests** : suite `pytest` complète 505 tests, isolée de toute donnée réelle.

## Le projet

- **Contexte** : renseigner l'évolution de la fiabilité du réseau de
  Bordeaux Métropole, mois après mois, avec une mesure stable et comparable.
- **Auteur** : Elias Khallouk.
- **Licences** : code sous [GNU AGPL-3.0](LICENSE), disponible sur
  <https://github.com/EliasKhallouk/Urban-Vision> ; rapports et données
  dérivées sous Licence Ouverte 2.0, comme les données TBM dont ils sont
  issus (citer « Urban Vision, d'après les données TBM »).