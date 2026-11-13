# Fichiers .env

Éditeur des fichiers `.env*` de vos projets.

- **Dossiers de projets** : ajoutez des dossiers (navigateur intégré) ; le module liste leurs fichiers dont le nom commence par `.env` (jusqu'à 4 niveaux, `node_modules`, `.git`, `venv`… ignorés). Il ne lit et n'écrit **que** des fichiers `.env*` situés dans ces dossiers (liens symboliques et sorties de dossier refusés).
- **Éditeur clé/valeur** : commentaires, lignes vides et lignes non modifiées sont conservés à l'identique ; `export`, guillemets, valeurs multilignes et commentaires en fin de ligne gérés. Mode « Texte » pour l'édition brute. Fins de ligne (CRLF/LF) et BOM préservés.
- **Secrets masqués** (clés contenant SECRET, PASSWORD, TOKEN, API_KEY, DSN…, URL avec identifiants) : révélation au clic ou globale.
- **Contrôles** : clés en double, lignes sans `=`, noms de clé invalides, guillemets non fermés, espaces autour de `=`, valeurs avec espaces sans guillemets.
- **Comparaison** de 2 à 8 fichiers avec une référence (ex. `.env.example`) : clés manquantes, en trop, valeurs différentes ; ajout des clés manquantes (valeurs de la référence ou vides).
- **Sauvegarde** : copie de sécurité avant chaque écriture (30 dernières par fichier, dans le dossier de données du module), aperçu et restauration ; détection des modifications externes avant d'écraser.
- Création d'un fichier (vide ou copie d'un autre, en vidant les valeurs secrètes).
