# Inventaire réseau local

Inventaire persistant des appareils de votre réseau local (complément du module « Scanner réseau », qui fait des scans ponctuels).

- **Détection du sous-réseau** de la carte active (IP, masque, passerelle, MAC via `ipconfig /all`) ou plage saisie (réseaux privés, /22 au plus).
- **Balayage** : ping de chaque adresse (32 en parallèle par défaut, réglable 4–64), puis lecture de la table ARP (`arp -a`) pour retrouver aussi les appareils qui ignorent le ping.
- **Identification** : DNS inverse, nom NetBIOS (requête NBSTAT UDP 137), nom mDNS (requête PTR unicast vers le port 5353 de l'appareil), fabricant d'après le préfixe MAC (table OUI intégrée des fabricants courants ; MAC aléatoires « privées » signalées), type d'appareil deviné (modifiable).
- **Ports courants** (22 ports : SSH, HTTP(S), SMB, RDP, imprimante, RTSP…) : à la demande par appareil ou à chaque balayage (option), uniquement sur les appareils en ligne. Bouton d'ouverture de l'interface web.
- **Historique** : première/dernière vue, nombre de balayages, présence sur les 30 derniers balayages, adresses IP successives. Appareils identifiés par leur MAC.
- **Nom, type et notes** par appareil ; statut « approuvé ».
- **Alerte nouvel appareil** (bandeau + notification) ; le premier balayage constitue la référence et ne déclenche pas d'alerte.
- **Balayage périodique** facultatif (15 min à 1 jour) tant que l'application est ouverte.
- **Export CSV** (séparateur `;`, UTF-8 avec BOM pour Excel). Historique des balayages.

Charge réseau : un ping ICMP par adresse (quelques dizaines de Ko pour un /24), au plus 32 en parallèle par défaut ; noms résolus pour les seuls appareils trouvés (12 en parallèle) ; test de ports par simple ouverture de connexion TCP (48 en parallèle, délai 0,5 s), sans envoi de données. Un /24 prend 10 à 20 s. À n'utiliser que sur vos propres réseaux.

Limites : IPv4 uniquement ; la table ARP ne connaît que le segment local (pas de MAC au-delà d'un routeur) ; des appareils avec MAC aléatoire peuvent apparaître plusieurs fois ; mDNS/NetBIOS ne répondent pas toujours (pare-feu).

Données : SQLite (`module.db`) dans le dossier de données du module.
