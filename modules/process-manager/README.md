# Gestionnaire de processus

Processus en cours, façon Gestionnaire des tâches.

- Liste : nom, PID, utilisateur, CPU % (normalisé sur tous les cœurs), mémoire, threads, date de démarrage, chemin et ligne de commande ; rafraîchissement 1 à 10 s ou pause, tri par colonne, recherche, vue arborescente parent / enfants.
- Détail : parents et enfants, mémoire, E/S, priorité, connexions réseau, fichiers ouverts et modules chargés (« Accès refusé » affiché proprement pour les processus protégés).
- « Qui utilise ce port ? » : saisir un port pour retrouver les processus (TCP/UDP, port local ou distant).
- Terminer, tuer (forcer) ou terminer tout l'arbre, avec confirmation. Refus pour le backend de LedroideDesktop, ses parents (Electron) et les processus auxiliaires d'Electron, ainsi que pour les processus système critiques. Vérification que le PID n'a pas été réutilisé.
- Changement de priorité (Basse → Haute ; le temps réel n'est pas proposé).

Dépendance : `psutil` (installée à l'activation). Les PID, parents et threads proviennent d'un seul instantané Toolhelp32 (bien plus rapide que psutil sous Windows) ; les attributs fixes (chemin, ligne de commande, utilisateur) sont mis en cache.

Raccourcis : `F5`, `Ctrl+F`, `T` (arborescence), `↑`/`↓` (sélection), `Suppr` (terminer), `Maj+Suppr` (terminer l'arbre), `Échap`.
