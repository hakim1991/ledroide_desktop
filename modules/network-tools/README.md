# Outils réseau

Boîte à outils réseau (Windows) :

- **Infos** : nom d'hôte, interfaces et adresses locales (psutil), IP publique (api.ipify.org).
- **Ping** : `ping -n`, résultats en direct et statistiques (min / moy / max, pertes).
- **DNS** : résolution directe (A/AAAA via `getaddrinfo`) et inverse.
- **Scan de ports** : plage ou ports courants, connexions TCP concurrentes (asyncio), 1024 ports maximum par scan. À utiliser uniquement sur vos propres machines / votre réseau.
- **Traceroute** : `tracert -d`, sauts affichés au fil de l'eau.
- **Test de débit** : latence, téléchargement et envoi via speed.cloudflare.com.

Dépendance : `psutil` (onglet Infos uniquement).
