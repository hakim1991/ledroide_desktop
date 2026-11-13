# Cartes de révision

Cartes recto / verso avec répétition espacée.

- **Paquets** : description, couleur, nouvelles cartes par jour, révisions max. par jour, mode « saisie de la réponse » par défaut.
- **Cartes** : éditeur avec mise en forme simple (gras, italique, souligné, barré, listes, code, surlignage) et **images collées** (Ctrl+V), glissées ou choisies (redimensionnées à 1000 px). Le HTML est filtré côté serveur (liste blanche de balises, images `data:` uniquement). Étiquettes.
- **Révision** : Encore / Difficile / Bien / Facile avec l'intervalle prévu affiché sur chaque bouton ; raccourcis `Espace`/`Entrée` (retourner), `1`–`4`, `E` (modifier), `Ctrl+Z` (annuler), `Échap`. Mode **saisie de la réponse** avec comparaison caractère par caractère (insensible à la casse) et réponse suggérée. Option « continuer en avance » quand seules restent des cartes en apprentissage.
- **Parcourir** : recherche, filtres (nouvelles, en apprentissage, dues, suspendues), tri, actions groupées (suspendre, réinitialiser, déplacer, supprimer).
- **Statistiques** : révisions par jour (par réponse), temps passé, série de jours, rétention sur 30 jours (et cartes mûres), prévision des 30 prochains jours, répartition nouvelles / apprentissage / jeunes / mûres.
- **Import CSV / TSV** (séparateur au choix, en-têtes, choix des colonnes recto / verso / étiquettes, texte brut ou HTML, doublons ignorés) et **export** TSV / CSV (texte ou HTML).

## Algorithme (SM-2, variante proche d'Anki)

- Nouvelle carte : étapes de 1 min puis 10 min ; « Bien » à la dernière étape → révision le lendemain ; « Facile » → 4 jours ; « Encore » → retour à 1 min.
- Révision : Bien = (intervalle + retard/2) × facilité ; Difficile = intervalle × 1,2 et facilité −0,15 ; Facile = (intervalle + retard) × facilité × 1,3 et facilité +0,15. Facilité initiale 2,5, minimum 1,3.
- Oubli : facilité −0,2, intervalle × 0,5 (min. 1 jour), réapprentissage de 10 min.
- Aléa de ±5 % sur les intervalles ≥ 7 jours ; intervalle max. 100 ans. Une carte est « mûre » à partir de 21 jours.
- Ordre quotidien : cartes en apprentissage dues, puis révisions dues (limite par paquet), puis nouvelles (limite par paquet). Le jour suit l'horloge locale (minuit).

Données : SQLite (`module.db`) dans le dossier de données du module (images incluses dans les cartes).
