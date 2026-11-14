# Sticky Notes

Tableaux de post-its colorés.

- Plusieurs tableaux (onglets) ; déplacement d'une note d'un tableau à l'autre.
- Vue « Tableau » libre (glisser, redimensionner, zoom Ctrl + molette, glisser le fond pour se déplacer, double-clic sur le fond pour créer une note) et vue « Grille » triée par date de modification. Bouton « Ranger » (annulable).
- Texte : cliquer pour modifier. `- [ ] tâche` devient une case à cocher cliquable (avec compteur), `- élément` une puce, `# Titre` et `**gras**` sont mis en forme ; Entrée continue la liste.
- Par note : 8 couleurs, 3 tailles de texte, épingler (position verrouillée), réduire au titre, dupliquer, copier le texte.
- Recherche sans accents (les autres tableaux affichent leur nombre de résultats).
- Corbeille : toute suppression est annulable ; restauration ou suppression définitive depuis le menu ⋯.
- Export JSON (réimportable, sans écraser l'existant) et Markdown.
- Raccourcis : `N` nouvelle note, `Ctrl+F` rechercher, `Ctrl+D` dupliquer, `Suppr` corbeille, `Échap`.

Données : `notes.db` (SQLite) dans le dossier de données du module ; la base de la version 1 est migrée automatiquement (colonnes ajoutées, tableau « Tableau 1 » créé). Les modifications sont enregistrées par note, 0,5 s après la dernière frappe et quand le module est masqué.

Actions : `state.get`, `list_notes`, `create_note`, `update_note`, `raise_note`, `duplicate_note`, `arrange_notes`, `delete_note`, `delete_all_notes`, `restore_note`, `trash.list`, `trash.empty`, `board.save`, `board.delete`, `board.reorder`, `export`, `import`.
