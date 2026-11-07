# Connexions réseau

Équivalent graphique de `netstat -ano` (Windows) basé sur `psutil.net_connections` :

- Connexions TCP/UDP et ports en écoute : adresses locale/distante, état, PID, nom et chemin du processus.
- Filtres : protocole, IPv4/IPv6, état, écoute uniquement, masquage du loopback, recherche (port, processus, PID, IP, nom d'hôte).
- Regroupement par processus, tri par colonne, actualisation automatique, surbrillance des nouvelles connexions.
- Noms des ports connus (HTTP, RDP, SMB…) et DNS inverse optionnel des adresses distantes (asynchrone, en cache 1 h).
- « Terminer le processus » avec confirmation ; processus système critiques et LedroideDesktop lui-même protégés.
  En cas d'accès refusé, possibilité de réessayer en administrateur (invite UAC).
- Export CSV (séparateur `;`, UTF-8).

Sans droits administrateur, le nom de certains processus apparaît comme « accès refusé ».

Dépendance : `psutil`.
