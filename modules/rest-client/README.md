# REST Client

Teste des API HTTP/REST depuis LedroideDesktop.

- Méthodes GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS
- Paramètres d'URL synchronisés avec l'URL, en-têtes, corps JSON / texte / XML / formulaire urlencoded
- Authentification Bearer, Basic, clé d'API (en-tête ou paramètre)
- Environnements de variables : `{{base_url}}`, `{{token}}`… substitués partout
- Réponse formatée (coloration JSON, XML/HTML indenté), brute, aperçu d'image, en-têtes, redirections, durée et taille
- Historique (taille configurable) et requêtes enregistrées par dossier
- Export en commande cURL ; coller une commande `curl …` dans la barre d'URL l'importe
- Options : redirections, vérification SSL, délai maximum

Les requêtes partent du backend Python (bibliothèque standard, aucune dépendance), donc sans restriction CORS. Seuls `http://` et `https://` sont acceptés. Raccourcis : `Ctrl+Entrée` pour envoyer, `Ctrl+S` pour enregistrer.
