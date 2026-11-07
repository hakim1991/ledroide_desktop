# 🧮 Calculateur de sous-réseaux

Calculs IPv4 et IPv6 (module Python `ipaddress`, aucune dépendance).

- **Calcul** : saisie CIDR, IP + masque ou masque générique. Réseau, diffusion, première/dernière adresse, nombre d'hôtes, masque/wildcard, vue binaire colorée (bits réseau / hôte), classe, plage privée/publique/spéciale (RFC), zone inverse, réseaux voisins.
- **Découpage** : en N sous-réseaux, par nouveau préfixe ou par nombre d'hôtes ; export CSV (séparateur `;`, compatible Excel FR).
- **VLSM** : liste de besoins nommés → plan d'adressage optimisé, espace libre restant, export CSV.
- **Appartenance** : test d'adresses IP dans des réseaux (correspondance la plus spécifique) et détection des chevauchements.
- **Agrégation** : résumé de routes exact et super-réseau unique.
