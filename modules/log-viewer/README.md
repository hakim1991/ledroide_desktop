# Visionneuse de journaux

Lecture de fichiers de log, même très volumineux.

- Lecture par blocs : index clairsemé (un point de reprise toutes les 256 lignes) construit en arrière-plan ; l'affichage est virtualisé et ne demande au backend que les lignes visibles.
- Suivi en direct (tail -f) par interrogation de la taille ; rotation (fichier remplacé), troncature et disparition/recréation détectées, avec relecture automatique.
- Coloration par niveau (ERREUR / AVERT. / INFO / DEBUG) avec détection des formats courants : mots-clés, `[error]`, `level=…`, JSON, journaux d'accès HTTP (4xx/5xx), glog ; les lignes de pile (traceback, `at …`) héritent du niveau.
- Filtres : niveaux, texte ou regex (casse, exclusion), plage de temps (ISO, syslog, Apache/nginx, JJ/MM/AAAA, epoch JSON).
- Surlignage multiple coloré, recherche (F3 / Maj+F3), aller à la ligne (Ctrl+G), signets persistants (clic sur le numéro, Ctrl+B, F2).
- Plusieurs onglets (restaurés au redémarrage), détail de la ligne sélectionnée (JSON indenté).
- Export : téléchargement de la vue filtrée, enregistrement vers un fichier (avec numéros), copie de la sélection.
- Encodages : UTF-8, Latin-1, Windows-1252, UTF-16 LE/BE (BOM détecté), changement à la volée.

Données : SQLite (`module.db`) pour les fichiers récents, signets et préférences. Aucune dépendance Python.
