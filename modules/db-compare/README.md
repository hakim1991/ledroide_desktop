# Comparateur de bases

Compare deux bases **SQLite** (fichiers) et/ou **PostgreSQL** (URL `postgresql://utilisateur@hôte:5432/base` + mot de passe + schéma).

- **A = référence**, **B = cible** : les scripts générés transforment B pour qu'elle corresponde à A, dans le dialecte de B (⇄ inverse les deux).
- **Schéma** : tables, colonnes (type, NULL, défaut, auto-incrément), clés primaires, clés étrangères (actions ON DELETE/UPDATE), index (uniques, expressions). Entre dialectes différents, les types sont comparés par famille (entier, texte, réel, date…).
- **Données** d'une table commune : appariement par clé (clé primaire par défaut, modifiable), lecture par lots triés (pagination par clé, aucun chargement complet), lignes ajoutées / supprimées / modifiées avec les colonnes concernées. Limites réglables : taille des lots, lignes max. par base, différences détaillées max. Annulable.
- **Script SQL** : migration de schéma (CREATE/ALTER/DROP ; reconstruction de table pour SQLite quand `ALTER TABLE` ne suffit pas) et/ou synchronisation des données (INSERT/UPDATE/DELETE), à copier ou enregistrer. Les suppressions sont désactivées par défaut (lignes commentées).

Sécurité : les bases sont ouvertes en lecture seule (SQLite `mode=ro` + `query_only`, PostgreSQL `read_only`), jamais modifiées. Le mot de passe n'est ni journalisé, ni enregistré (l'historique des connexions conserve l'URL sans mot de passe).

Limites : contraintes CHECK, déclencheurs, vues, séquences et conditions d'index partiels ne sont pas comparés ; un script généré doit toujours être relu et testé sur une copie.

Dépendance : `psycopg[binary]` (PostgreSQL uniquement ; SQLite fonctionne sans).
