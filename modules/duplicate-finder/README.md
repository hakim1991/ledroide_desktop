# 🧬 Doublons

Recherche les fichiers en double dans un dossier (chemin absolu à coller), en local.

- Options : sous-dossiers, taille minimale.
- Méthode : regroupement par taille → pré-filtre sur les 64 premiers Kio → empreinte **SHA-256** complète (lecture par blocs de 1 Mio). Les liens symboliques et liens physiques en double sont ignorés.
- L'analyse tourne en tâche de fond ; la progression est affichée (interrogation de `/status/{job_id}`), avec possibilité d'arrêter.
- Résultats : groupes triés par espace récupérable, total récupérable, dates de modification.
- Sélection manuelle ou automatique (« Garder le plus récent / le plus ancien »).
- Suppression : envoi à la **corbeille Windows** (`send2trash`) après confirmation. Par sécurité, au moins un exemplaire de chaque groupe est toujours conservé et la taille du fichier est revérifiée avant l'envoi.

Dépendance Python : `send2trash`.
