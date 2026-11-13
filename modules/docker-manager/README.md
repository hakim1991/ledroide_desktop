# Docker

Gestion de Docker via le CLI `docker` (appels asynchrones avec listes d'arguments, sorties `--format '{{json .}}'`).

- Détection de Docker : CLI absent (« Docker introuvable ») ou démon arrêté, avec marche à suivre.
- Conteneurs : liste, état, ports (liens cliquables), démarrer / arrêter / redémarrer / pause / supprimer, statistiques CPU et mémoire, inspection (résumé + JSON).
- Journaux en direct (`docker logs --follow`) diffusés par événements, avec arrêt, filtre, horodatage, enregistrement.
- Images : liste, taille, suppression, `pull` avec progression par couche (annulable).
- Volumes et réseaux : liste, inspection, suppression (réseaux intégrés protégés).
- Compose : projets détectés via les labels `com.docker.compose.*` (up / restart / stop / down), ou fichier `docker-compose.yml` choisi (fichiers récents mémorisés).
- Nettoyage (`prune`) : conteneurs, images, volumes, réseaux, cache de build, système ; confirmation obligatoire.

Aucune dépendance Python. Aucune base de données (les fichiers Compose récents sont dans `compose_files.json` du dossier de données).
