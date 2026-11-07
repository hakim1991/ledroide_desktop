# 🧩 Schémas blocs

Éditeur de schémas blocs (organigrammes, architectures, processus).

## Fonctionnalités
- **47 formes** rangées par catégories :
  - *Formes de base* : rectangle, arrondi, ellipse, cercle, triangle, pentagone, octogone, étoile, croix, trapèze, bulle, nuage, note, texte libre, groupe/cadre (placé automatiquement à l'arrière-plan) ;
  - *Flèches* : droite, gauche, haut, bas, double, chevron ;
  - *Organigramme* : début/fin, décision, entrée/sortie, préparation, sous-processus, document(s), saisie et opération manuelles, délai, affichage, données stockées, stockage interne, renvoi de page, connecteur, fusion ;
  - *Informatique & réseau* : base de données, utilisateur, ordinateur, serveur, mobile, internet, pare-feu (texte sous l'icône) ;
  - *UML* : classe, paquetage, composant.
- **Palette** : recherche (sans accents, par mots-clés : « bdd », « pc », « si »…, Entrée = ajouter la 1re), catégories repliables (mémorisées), formes récentes, aide repliable.
- **Panneaux masquables** : boutons ◧ / ◨ de la barre du haut, « » Formes » sur le canevas, `Ctrl+B` (palette), `Ctrl+Maj+B` (propriétés), `Ctrl+F` (rechercher une forme). Le dessin ne bouge pas à l'écran quand on masque/affiche.
- **Liaisons** : survolez un bloc, tirez depuis un point bleu vers un autre bloc. Relâcher dans le vide crée un nouveau bloc déjà relié.
  Tracé droit / à angles / courbe, trait plein / tirets / pointillés, flèches →, ↔, ←, aucune, couleur, épaisseur, inversion du sens.
- **Trajet des flèches** (liaison sélectionnée) :
  - glisser la ligne : la courbe (point de passage) ou, en tracé à angles, déplace le segment du milieu (double-clic sur sa poignée = recentrer) ;
  - poignées **+** : ajouter un point de passage ; **■** : le déplacer, double-clic = supprimer ;
  - extrémités **○** : glisser vers un autre côté du bloc ou vers un autre bloc (rattachement) ;
  - panneau : côté de départ / d'arrivée (Auto, ↑ → ↓ ←) et « Revenir au trajet automatique ».
  Les points de passage suivent quand on déplace ensemble les deux blocs reliés.
- **Nommage** : double-clic (ou F2) sur un bloc ou une liaison. Entrée valide, Maj+Entrée = retour à la ligne, Échap annule.
- **Position du texte** : poignée orange du bloc sélectionné à glisser (ou Alt+glisser le bloc), aimantée sur les axes centraux (Maj = libre).
  Positions prédéfinies dans le panneau : grille 3×3 dans la forme, au-dessus ou en dessous. Double-clic sur la poignée = recentrer.
- **Style** : remplissage, bordure, couleur/taille/gras du texte, taille du bloc, premier/arrière-plan. Fonctionne aussi en multi-sélection.
- **Navigation** : molette = zoom, glisser le fond = déplacement, Maj+glisser = sélection rectangle, grille magnétique.
- **Raccourcis** : Ctrl+Z / Ctrl+Y, Ctrl+C / X / V / D, Ctrl+A, Suppr, flèches (Maj = ×10), Ctrl+S.
- **Plusieurs schémas** enregistrés automatiquement (SQLite `backend/diagram-editor.db`) : créer, renommer, dupliquer, supprimer.
- **Export** PNG (×2), SVG, JSON — fond sombre, clair ou transparent. **Import** JSON.

## API
`GET/POST /diagrams`, `GET/PUT/DELETE /diagrams/{id}`, `POST /diagrams/{id}/duplicate`
