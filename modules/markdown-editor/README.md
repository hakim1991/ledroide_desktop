# 📝 Éditeur Markdown

- Éditeur et **aperçu en direct** côte à côte (modes éditeur seul / divisé / aperçu seul), défilement synchronisé.
- Rendu côté serveur avec la bibliothèque Python `markdown` : tableaux, blocs de code, notes de bas de page,
  listes de tâches `- [ ]`, texte barré `~~…~~`.
- Barre d'outils : gras, italique, barré, titres, listes, tâches, citation, lien, image, code, tableau, séparateur.
  Raccourcis : Ctrl+B, Ctrl+I, Ctrl+K, Ctrl+E, Ctrl+S, Tab / Maj+Tab, continuation automatique des listes.
- Documents enregistrés dans SQLite avec **sauvegarde automatique** : liste, recherche, création, renommage,
  suppression (avec confirmation).
- **Export** `.md` et `.html` autonome (mise en page incluse, thème clair/sombre) ; **import** de fichiers `.md`
  (bouton ou glisser-déposer dans l'éditeur).

Dépendance : `markdown`.
