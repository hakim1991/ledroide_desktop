# Serveur d'API simulé

Serveurs HTTP/1.1 locaux (implémentation maison sur `asyncio.start_server`) qui simulent une API.

- Plusieurs serveurs, chacun sur son port ; écoute sur 127.0.0.1 par défaut, 0.0.0.0 possible (avec avertissement).
- Routes : méthode (ou ANY), chemin avec paramètres `/users/:id` (ou `{id}`) et joker `*`, ordre de priorité, activation.
- Réponses : statut, en-têtes, corps JSON / texte / HTML / XML avec gabarits `{{params.id}}`, `{{query.x}}`, `{{headers.x}}`, `{{body.champ}}`, `{{now}}`, `{{timestamp}}`, `{{uuid}}`, `{{randomInt(1,100)}}` (échappés correctement en JSON) ; aperçu du rendu.
- Délai fixe ou aléatoire, plusieurs réponses par route (première, séquentielle, aléatoire).
- CORS automatique (en-têtes + pré-vérifications OPTIONS), proxy optionnel vers une vraie API pour les routes non définies.
- Journal des requêtes en direct (en-têtes, corps, réponse, durée), rejeu, copie en cURL, création d'une route depuis une requête.
- Keep-alive, corps `chunked`, limites (en-têtes 64 Ko, corps 10 Mo), modifications appliquées à chaud.
- Import / export JSON ; import OpenAPI 3 / Swagger 2 (JSON ou YAML) avec exemples générés depuis les schémas.
- Les serveurs s'arrêtent proprement à la désactivation du module.

Dépendance : `PyYAML` (import OpenAPI en YAML). Données : SQLite (`module.db`).
