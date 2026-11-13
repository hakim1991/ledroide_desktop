# Organiseur de photos

Organise une photothèque locale sans service en ligne.

- **Indexation** de dossiers (tâche de fond avec progression, ré-indexation incrémentale) : date de prise de vue, appareil et position GPS lus dans l'EXIF avec Pillow (date de modification à défaut), miniatures en cache dans les données du module.
- **Vues** : toutes les photos, par date (année / mois), par appareil, par lieu (regroupement des coordonnées GPS par cellules de 1, 10 ou 100 km, liste des coordonnées — pas de carte en ligne), visionneuse avec informations EXIF.
- **Doublons** : exacts (SHA-256, calculé uniquement pour les fichiers de même taille) et visuellement similaires (dHash ou pHash implémentés avec Pillow, seuil réglable) ; la meilleure version est conservée par défaut, les autres partent à la **corbeille** (`send2trash`, jamais de suppression définitive).
- **Renommage en lot** selon un modèle, par ex. `{date:%Y-%m-%d_%H%M%S}_{appareil}_{n}` (jetons : `{date:FORMAT}`, `{appareil}`, `{n}` / `{n:3}`, `{nom}`, `{annee}`, `{mois}`, `{mois_nom}`, `{jour}`, `{dossier}`), avec aperçu, gestion des conflits et annulation.
- **Tri** dans des dossiers `Année/Mois` (modèle modifiable) par déplacement ou copie, avec aperçu et annulation (les copies annulées vont à la corbeille).

Pour l'édition ou le nettoyage des métadonnées, utilisez le module **EXIF** ; pour les doublons de fichiers quelconques, le module **Doublons**.

Dépendances : `Pillow`, `send2trash`.
