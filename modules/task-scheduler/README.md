# Planificateur de tâches

Planificateur interne à LedroideDesktop (indépendant du Planificateur de tâches de Windows).

- Actions : commande `cmd`, PowerShell, Python (code ou chemin d'un script `.py`, exécuté avec l'interpréteur de l'application), ou action système : extinction, redémarrage, mise en veille, verrouillage, précédée d'un compte à rebours affiché et annulable.
- Déclencheurs : une fois à une date/heure, toutes les N minutes, chaque jour à HH:MM sur les jours choisis, expression cron à 5 champs (`*`, listes, intervalles, pas, noms `mon`/`jan`, `@daily`…). Aperçu des prochaines exécutions dans l'éditeur.
- Exécution : sous-processus sans fenêtre, délai maximum configurable (arbre de processus arrêté au-delà), dossier de travail, « Exécuter maintenant », arrêt d'une exécution en cours, activer / désactiver / dupliquer.
- Historique : date, origine (planifiée / manuelle), résultat, code retour, durée, sortie tronquée (taille et nombre d'exécutions conservées réglables dans la configuration du module).

**Les tâches ne tournent que lorsque LedroideDesktop est ouvert et ce module actif** ; une exécution manquée n'est pas rattrapée. Une tâche « une fois » se désactive après son exécution. À la désactivation du module, les comptes à rebours en cours sont abandonnés (l'action système n'est pas exécutée) et les processus lancés sont arrêtés.

Stockage : SQLite (`module.db`). Aucune dépendance. Raccourcis : `Ctrl+N` nouvelle tâche, `F5`, `Entrée` exécuter la tâche sélectionnée, `Ctrl+Entrée` enregistrer dans l'éditeur.
