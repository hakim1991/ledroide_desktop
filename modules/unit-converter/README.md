# 📏 Convertisseur

Conversion instantanée entre unités, avec bouton d'inversion et tableau de toutes les unités.

- Longueur, masse, température, volume, surface, vitesse, données (bits/octets, SI et binaire), temps, énergie, pression
- **Devises** : taux de https://open.er-api.com (base EUR), mis en cache 6 h dans `rates_cache.json` ; date de mise à jour affichée ; hors ligne, les derniers taux en cache sont utilisés
- **Fuseaux horaires** : conversion date/heure entre fuseaux IANA (heure d'été, heures inexistantes/ambiguës signalées), horloge « ailleurs dans le monde » personnalisable

Dépendance : `tzdata` (base des fuseaux, absente du Python embarqué Windows) — bouton « deps ».

API : `GET /rates?force=`, `GET /tz/zones`, `POST /tz/convert`, `GET /health`.
