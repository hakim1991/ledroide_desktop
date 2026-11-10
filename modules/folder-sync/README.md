# Synchro de dossiers

Compare un dossier **source** et un dossier **cible**, affiche un aperçu des opérations, puis synchronise.

- Comparaison en tâche de fond (progression, annulation) : fichiers nouveaux, modifiés (taille + date, tolérance configurable, ou empreinte SHA-256 en option), identiques, présents seulement dans la cible.
- Filtres d'exclusion glob (`node_modules`, `*.tmp`, `build/cache`…), appliqués au nom ou au chemin relatif.
- Modes : **Copie** (ajouts + mises à jour), **Miroir** (en plus, les fichiers absents de la source partent **à la corbeille** via `send2trash` ; sans cette bibliothèque, aucune suppression n'est faite), **Bidirectionnel** (le plus récent gagne ; dates identiques mais contenus différents = conflit listé et ignoré).
- Aperçu obligatoire : chaque opération peut être décochée avant l'exécution ; confirmation récapitulative.
- Exécution annulable, dates préservées (`shutil.copy2`, écriture via fichier temporaire puis remplacement), journal consultable dans le panneau de gauche.
- Profils de synchro enregistrables (source, cible, mode, options, exclusions).

Refuse source = cible, ou un dossier inclus dans l'autre. Les liens symboliques sont ignorés.
Raccourcis : `Ctrl+Entrée` comparer, `Ctrl+S` enregistrer le profil, `Échap` annuler la tâche en cours.
