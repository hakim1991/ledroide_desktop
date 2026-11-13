# Test de débit

Mesure la qualité de la connexion Internet via les serveurs publics de **Cloudflare** (`speed.cloudflare.com`, centre de données le plus proche).

- **Latence** (médiane de 20 requêtes vides sur une connexion ouverte, temps serveur `Server-Timing` déduit) et **gigue** (écart moyen entre mesures consécutives).
- **Débit descendant** (`/__down?bytes=N`) et **montant** (`/__up`) : 1 à 8 connexions HTTPS parallèles démarrées progressivement, requêtes de taille croissante (100 ko → 50 Mo), durée par sens réglable (5 à 20 s), option « 25 Mo max. par sens » pour les connexions mobiles ; le premier quart de chaque mesure (montée en charge) est exclu.
- Progression en direct (jauge animée, échelle 0–1000 Mbit/s) via `module.job` ; annulation à tout moment.
- Infos : IP publique, centre de données et pays (`/cdn-cgi/trace`, en-têtes `cf-meta-*`) ; le FAI n'est affiché que si Cloudflare le renvoie (l'API `/meta` est réservée au site web de Cloudflare).
- Historique en SQLite (notes par test), statistiques et graphique dans le temps (7 j, 30 j, 1 an, tout), export CSV (Excel, `;`).

Aucune dépendance : bibliothèque standard (`http.client` dans des threads dédiés, pour ne pas bloquer la boucle d'événements ni le pool de threads partagé). Un test complet peut consommer plusieurs centaines de Mo sur une connexion rapide.

Paramètres de l'action `run` : `connections`, `duration`, `ping_count`, `limit_mb` (0 = illimité), `max_chunk_mb`, `download`, `upload`, `save` — les tests automatisés utilisent `duration: 2, limit_mb: 3`.
