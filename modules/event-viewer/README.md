# Journaux d'événements

Lecture des journaux Windows via `Get-WinEvent -FilterHashtable` (PowerShell) :

- Journaux Système, Application, Sécurité (droits administrateur requis — lecture possible via invite UAC),
  Installation, ou tout autre journal activé et non vide (`Get-WinEvent -ListLog *`).
- Filtres : niveau (Critique, Erreur, Avertissement, Information), période (1 h, 24 h, 7 j, 30 j, personnalisée),
  source, ID d'événement, texte du message, nombre maximal de résultats.
- Tableau coloré par niveau, panneau de détails (message complet, propriétés, XML), copie.
- Statistiques (nombre par niveau, principales sources) et tableau de bord « Erreurs récentes (24 h) ».
- Export CSV.

Aucune dépendance Python.
