# Analyseur de disque

Trouve ce qui occupe l'espace disque.

- Lecteurs : espace libre / occupé, type, nom de volume ; analyse d'un lecteur, du dossier personnel ou de n'importe quel dossier.
- Analyse en tâche de fond (`os.scandir`), avec progression (pourcentage estimé sur un lecteur entier) et annulation. Les erreurs d'accès sont comptées et ignorées ; les liens symboliques et jonctions ne sont pas suivis (pas de boucle, pas de double comptage).
- Carte proportionnelle (treemap, canvas) sur deux niveaux : clic pour entrer dans un dossier, fil d'Ariane et `Retour arrière` pour remonter, double-clic sur un fichier ou clic droit pour l'afficher dans l'Explorateur.
- Arborescence triée par taille, 100 plus gros fichiers, répartition par extension.
- Nettoyage : taille du dossier temporaire, du cache des miniatures et de la corbeille. Seul le contenu de `%TEMP%` peut être supprimé ici, après aperçu et confirmation, avec un filtre d'ancienneté (24 h par défaut) ; les fichiers verrouillés sont ignorés et les jonctions supprimées sans toucher à leur cible. Miniatures et corbeille : liens vers le Nettoyage de disque Windows et la corbeille.

Les tailles sont les tailles logiques des fichiers (les fichiers OneDrive « à la demande » comptent pour leur taille réelle même s'ils ne sont pas téléchargés). Pour les gros disques, l'arbre envoyé à l'interface est élagué ; les dossiers sont détaillés à la demande. Les 3 dernières analyses sont gardées en mémoire. Aucune dépendance.

Raccourcis : `1`–`5` onglets, `Retour arrière` remonter, `F5` relancer l'analyse, `Échap` annuler.
