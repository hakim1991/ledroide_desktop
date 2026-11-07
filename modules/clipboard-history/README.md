# Historique presse-papier

Surveille le presse-papier Windows en arrière-plan (thread démon, interrogation toutes les ~700 ms via `ctypes`, sans dépendance) et enregistre chaque nouvelle copie dans SQLite (`backend/clipboard-history.db`).

- Texte (CF_UNICODETEXT) ; images en option (Pillow) avec miniature PNG.
- Doublons consécutifs ignorés, nombre maximal d'entrées configurable (les éléments épinglés sont toujours conservés).
- Recherche, épingler, supprimer, vider l'historique (hors épinglés), pause / reprise de la capture.
- Un clic sur un élément le recopie dans le presse-papier (via le backend, fonctionne malgré les restrictions de l'iframe).

Windows uniquement.
