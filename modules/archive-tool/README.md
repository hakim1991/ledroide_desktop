# Archives

Crée, inspecte et extrait des archives depuis LedroideDesktop.

- Formats : **ZIP** (bibliothèque standard ; mot de passe ZipCrypto/AES en extraction, création chiffrée AES-256 via `pyzipper`), **7z** (`py7zr`, mot de passe et chiffrement des noms), **TAR**, **TAR.GZ**, **TAR.BZ2**, **TAR.XZ**.
- Contenu affiché sans extraire : arborescence repliable, tailles, taille compressée et gain (ZIP), dates, entrées chiffrées et dangereuses signalées.
- Extraction complète ou d'une sélection (cases à cocher), dans un sous-dossier au nom de l'archive, sans écraser par défaut ; création à partir de fichiers et dossiers avec niveau de compression 0-9.
- Tâches de fond avec progression et annulation (le fichier partiel est supprimé ; à l'extraction, les fichiers déjà terminés sont conservés).
- Sécurité : entrées `../`, chemins absolus, lecteurs (`C:`), flux alternatifs (`:`), noms réservés Windows et liens symboliques ZIP/7z ignorés ; TAR via `tarfile.data_filter` / `filter="data"` ; limite de taille décompressée configurable (taille annoncée vérifiée avant, octets réellement écrits contrôlés pendant l'extraction).

Raccourcis : `Entrée` ouvrir, `Ctrl+E` extraire, `Échap` annuler la tâche en cours.
