# WHOIS & SSL

- **WHOIS** : domaine → whois.iana.org, puis registre de l'extension (`refer:`), puis bureau d'enregistrement ; repli RDAP (rdap.org) quand le registre ne publie pas les dates (ex. .uk). Champs extraits : registrar, dates de création / mise à jour / expiration (jours restants), statuts, serveurs de noms, DNSSEC, réponses brutes. Adresses IP : IANA → ARIN / RIPE / APNIC… (renvois suivis).
- **Certificat SSL** : négociation TLS sur hôte:port, chaîne de certificats, sujet, émetteur, SAN, validité, clé, algorithme de signature, n° de série, empreintes SHA-256/SHA-1, version TLS et suite de chiffrement, vérification par le magasin Windows (truststore) et correspondance du nom d'hôte.
- **En-têtes HTTP** : suivi des redirections (statut, destination, temps), en-têtes finaux, contrôle HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy avec explications et note globale.
- **Surveillance** : liste de domaines (SQLite) avec expiration du certificat et du domaine, actualisation individuelle ou globale.

Dépendances : `cryptography` (analyse détaillée ; mode basique sans elle), `truststore` (vérification via le magasin de certificats Windows). Délais d'attente sur toutes les connexions.
