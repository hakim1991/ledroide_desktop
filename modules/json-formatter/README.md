# 🧾 JSON / YAML

Formateur, validateur et convertisseur de données structurées.

- Formatage JSON/YAML (indentation 2, 4 ou tabulation), minification, tri des clés
- Validation avec position précise de l'erreur (ligne / colonne, bouton « Aller à l'erreur »)
- Conversion JSON ↔ YAML ↔ CSV (tableau d'objets ; les objets imbriqués sont aplatis en clés pointées `a.b.c`, et reconstruits au retour)
- Vue arborescente repliable, copie et téléchargement du résultat
- Glisser-déposer d'un fichier `.json`, `.yaml`, `.csv`

Dépendance : `PyYAML` (bouton « deps »).

API : `POST /convert`, `POST /validate`, `POST /export`, `GET /file/{nom}`, `GET /health`.
