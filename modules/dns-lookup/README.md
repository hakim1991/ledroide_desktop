# 🔎 DNS / nslookup

Outil de requêtes DNS avancé (remplace `nslookup`).

- **Requête** : types A, AAAA, MX, TXT, NS, CNAME, SOA, PTR, SRV, CAA ou « Tous ». Résolveur au choix : système, Cloudflare, Google, Quad9, OpenDNS ou IP personnalisée (UDP/TCP). Affiche TTL, temps de réponse, drapeau « autoritaire » (AA), DNSSEC (AD), chaîne CNAME, autorité (SOA / TTL négatif). Une IP saisie déclenche automatiquement une résolution inverse (PTR) ; les adresses A/AAAA obtenues sont aussi résolues en inverse.
- **Propagation** : même requête sur une dizaine de résolveurs publics en parallèle, taux de concordance et variantes.
- **Diagnostic e-mail** : MX (et leurs adresses), SPF (analyse des termes, comptage récursif des requêtes / limite 10), DMARC (balises expliquées), DKIM (sélecteur saisi ou sélecteurs courants, taille de clé), MTA-STS, TLS-RPT, BIMI.
- **Historique** des dernières requêtes (SQLite `dns-lookup.db`), rejouables en un clic.

Dépendance : `dnspython`.
