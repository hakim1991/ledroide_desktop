# Services Windows

Liste des services Windows (`psutil.win_service_iter`) : nom, nom affiché, état, type de démarrage
(dont « Automatique (début différé) », lu dans le registre), PID, exécutable, compte, description,
dépendances et services dépendants.

- Recherche et filtres (en cours / arrêtés, automatique / différé / manuel / désactivé), tri par colonne.
- Actions : démarrer, arrêter, redémarrer (option : arrêter aussi les dépendants) et changer le type de démarrage
  (`sc.exe config … start=`). Chaque action demande une confirmation.
- Sans droits administrateur, les actions passent par une invite UAC (PowerShell élevé).
- Le nom du service est validé strictement contre la liste réelle des services.
- Les services critiques (RpcSs, DcomLaunch, LSM, WinDefend, EventLog, Dhcp, Dnscache…) exigent une confirmation renforcée.

Dépendance : `psutil`.
