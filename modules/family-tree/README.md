# Arbre généalogique

Création et consultation d'arbres généalogiques.

- Plusieurs arbres ; une personne de référence par arbre (🏠).
- Personnes : prénoms, nom d'usage, nom de naissance, sexe, naissance et décès (date + lieu), profession, notes, photo (miniature 192 px).
- Dates complètes ou partielles : `12/03/1950`, `03/1950`, `1950`, avec « vers », « avant », « après ».
- Unions : mariage, PACS, union libre ; date, lieu, fin (divorce, séparation, veuvage). Plusieurs unions par personne, couples de même sexe possibles, parent unique possible.
- Vue arbre centrée sur une personne : ascendants (0 à 8 générations), descendants avec conjoints (0 à 8), fratrie. Double-clic pour recentrer, ▲ / pastille numérotée pour remonter ou descendre au-delà de la limite, ⚭ pour modifier une union. Zoom, déplacement, ajustement.
- Fiche latérale : faits, âge, parents, conjoints et enfants, frères et sœurs ; ajout d'un parent / conjoint / enfant ou liaison d'une personne existante.
- Vue liste triable et filtrable ; recherche rapide dans l'en-tête.
- Contrôles : pas plus de deux parents, pas de cycle (une personne ne peut pas être son propre ancêtre).
- Import / export GEDCOM 5.5.1 (UTF-8) et JSON (avec photos) ; export de la vue en SVG ou PNG ; arbre d'exemple.

Données : SQLite (`module.db`) dans le dossier de données du module. Toute l'UI passe par le bridge (`window.ledroide.call`).
Les photos sont chargées séparément (`photos.get`) pour garder la réponse de `tree.get` légère.
