# Testeur WebSocket / MQTT

Client de test temps réel ; les connexions sont tenues par le backend Python (pas de restriction CORS).

- **WebSocket** : URL `ws://`/`wss://`, en-têtes et sous-protocoles optionnels, vérification du certificat désactivable, envoi texte / JSON (validé) / binaire (hex), ping avec latence, journal horodaté entrant/sortant (filtre, copie, réutilisation), messages enregistrés.
- **MQTT** (3.1.1 ou 5) : hôte, port, TLS, identifiants, client ID, keepalive ; abonnements multiples avec jokers `+` et `#` (QoS 0/1/2), publication QoS 0/1/2 + retain, journal par topic, reconnexion automatique avec réabonnement.
- Derniers paramètres mémorisés **sans le mot de passe** ; connexions fermées proprement à la désactivation du module.

Dépendance : `paho-mqtt>=2,<3` (installée à l'activation ; sans elle l'onglet MQTT affiche un message). `websockets` est embarqué.
Raccourci : `Ctrl+Entrée` pour envoyer / publier.
