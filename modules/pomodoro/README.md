# 🍅 Pomodoro

Minuteur Pomodoro avec historique et statistiques.

- Durées configurables (travail, pause courte, pause longue) et nombre de cycles avant la pause longue
- Grand minuteur circulaire, Démarrer / Pause / Passer / Réinitialiser (raccourcis : Espace, S, R)
- Précis même onglet en arrière-plan : le temps est calculé à partir de l'heure de fin (horodatage), pas en comptant les ticks ; l'état survit à un rechargement
- Fin de phase : bip WebAudio, notification système si autorisée, sinon flash visuel + bandeau + titre clignotant
- Tâche associée à chaque session ; enchaînement automatique optionnel
- Historique SQLite (`pomodoro.db` : date, tâche, durée, type) et statistiques : graphique en barres 7 jours / 8 semaines, objectif quotidien, série de jours, temps par tâche

Aucune dépendance externe.

API : `GET/PUT /settings`, `GET/POST /sessions`, `DELETE /sessions/{id}`, `DELETE /sessions?confirm=true`, `GET /stats?days=&weeks=&today=`.
