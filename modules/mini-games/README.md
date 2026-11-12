# Mini-jeux

Recueil de 16 petits jeux avec scores, statistiques et reprise des parties en cours.

| Groupe | Jeux |
|---|---|
| Réflexion | Démineur, Sudoku, Fusion, Taquin, Mémoire, Solitaire |
| Arcade | Serpent, Blocs, Casse-briques |
| Mots | Mot du jour, Pendu |
| Duel | Quatre en ligne, Morpion, Reversi, Échecs, Dames (contre l'ordinateur ou à deux) |

- Accueil : recherche, nom du joueur (inscrit dans les classements), son et volume.
- Chaque jeu : classement par variante (`🏆`), statistiques, aide (`❔`), clavier et manette, partie en cours sauvegardée (reprise en pause pour les jeux en temps réel), pause automatique quand la fenêtre perd le focus.

## Structure

- `frontend/index.html` : accueil et hôte des jeux (liste `GAMES`).
- `frontend/common.js` : runtime partagé — contexte `ctx` passé à chaque jeu (scores, sauvegarde, stats, préférences, overlay, dialogues, sons WebAudio, entrées clavier/manette unifiées, boucle d'animation).
- `frontend/games/<id>.js` : un module ES par jeu, `export default { mount(root, ctx) → { pause(), destroy() } }`. Les IA et solveurs lourds tournent dans des Web Workers. `games/duel.js` : cadre commun des jeux à deux joueurs (réglages, IA, annuler, sauvegarde, stats).
- `frontend/dev-mock.js` : backend simulé (localStorage), chargé seulement hors de l'application (`python -m http.server` dans `frontend/`).
- `backend/main.py` : SQLite (`module.db`) — `scores` (50 meilleurs par jeu et variante), `saves` (une partie par jeu), `stats` (objet JSON, mises à jour `inc` / `max` / `min` / `set` sérialisées), `prefs` (par jeu, `_hub` pour l'accueil).

Actions : `hub.summary`, `scores.list`, `score.submit`, `scores.clear`, `save.get|set|clear`, `stats.get|update`, `prefs.get|set`, `game.reset`.
