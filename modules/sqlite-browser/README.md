# SQLite Browser

Explore et interroge des bases SQLite depuis LedroideDesktop.

- Ouverture par chemin, liste des fichiers récents et raccourci vers les bases des autres modules (`<dossier de données>/modules/*/*.db`)
- Schéma : tables (nombre de lignes), vues, index, déclencheurs ; colonnes, types, clés primaires, contraintes, index, clés étrangères et définition SQL
- Onglet **Données** : pagination, tri par colonne (clic sur l'en-tête), filtre texte sur toutes les colonnes
- Onglet **SQL** : éditeur (`Ctrl+Entrée` exécute la sélection ou tout le texte), plusieurs instructions séparées par `;`, résultat en tableau, lignes affectées, durée, interruption, historique
- **Lecture seule par défaut** (ouverture `file:…?mode=ro` + `PRAGMA query_only`) ; passage en écriture explicite avec confirmation (validation automatique, sauf `BEGIN` explicite)
- Export CSV / JSON d'une table (filtre et tri appliqués) ou du résultat d'une requête (ré-exécutée en lecture seule)
- Affichage limité (1000 lignes par défaut, configurable) ; BLOB affichés en taille + début hexadécimal ; double-clic sur une cellule pour voir/copier sa valeur

Bibliothèque standard uniquement (`sqlite3`, appels exécutés hors de la boucle d'événements). Raccourcis : `F5` actualiser, `Ctrl+O` champ de chemin, `Alt+1/2/3` onglets.
