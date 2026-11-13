# Supervision de services

Surveillance continue de sites et de services, en arrière-plan tant que le module est actif.

- **Sondes** : HTTP(S) (méthode, statuts attendus `200-399` ou `200,301`, mot-clé présent/absent, temps max., vérification du certificat, redirections), **TCP** (`hôte:port`), **ping** (commande `ping` du système ; repli TCP si elle est indisponible), **DNS** (A/AAAA via le résolveur du système, adresse attendue optionnelle), **certificat TLS** (validité et alerte N jours avant expiration).
- Intervalle par sonde (10 s à 24 h), délai d'attente, alerte après N échecs consécutifs, pause/reprise (individuelle ou globale), « Vérifier maintenant », bouton « Tester » dans l'éditeur.
- Disponibilité 24 h / 7 j / 30 j, temps de réponse (graphique moyenne + min/max, périodes 1 h → 30 j, pannes en rouge), 40 dernières vérifications, incidents (début, fin, durée, cause).
- Temps réel : événements `monitor:update` et `alert` (bandeaux d'alerte dans l'app). Option **notifications Windows** : toast natif via PowerShell (`-EncodedCommand` fixe, textes passés par variables d'environnement), aucune dépendance ; fonctionne même fenêtre du module fermée.
- Historique en SQLite, purge automatique (durée réglable), import/export JSON des sondes, sondes d'exemple.

Fonctionnement : une tâche asyncio démarre au chargement du module (arrêtée par `on_shutdown`) ; les sondes bloquantes (HTTP, ping) passent par des threads, les autres sont asynchrones ; 16 vérifications simultanées au plus.

Limites : DNS limité aux enregistrements A/AAAA (pas de MX/TXT sans bibliothèque supplémentaire) ; le ping ICMP dépend de la commande système.
