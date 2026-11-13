# Budget personnel

Suivi des finances personnelles, en euros, montants stockés en centimes (entiers).

- **Comptes** (courant, épargne, espèces, carte, investissement…) avec solde initial, couleur, archivage ; solde au jour et opérations à venir.
- **Opérations** : date, libellé, montant, catégorie, notes ; filtres (mois, catégorie, type, recherche), solde courant par compte, sélection multiple (catégoriser, supprimer), « Enregistrer + nouvelle », raccourci `N`.
- **Virements** entre comptes : deux opérations liées (modifiées / supprimées ensemble), exclues des revenus et dépenses.
- **Catégories** sur deux niveaux, avec couleurs (jeu par défaut créé au premier lancement).
- **Règles** d'auto-catégorisation par mot-clé (insensible à la casse et aux accents, priorité ordonnable), appliquées à la saisie, à l'import, ou à la demande.
- **Import** CSV (assistant : encodage, séparateur, lignes ignorées, en-têtes, colonnes Montant ou Débit/Crédit, formats de date et décimale français), OFX/QFX et QIF ; détection des doublons (date + montant + libellé, identifiant OFX `FITID`, doublons « possibles » si seul le libellé diffère) ; vérification ligne par ligne avant import.
- **Opérations récurrentes** (jour / semaine / mois / an, intervalle, date de fin, virements) : les échéances passées sont créées à l'ouverture du module.
- **Budgets mensuels** par catégorie (montant par défaut + ajustement d'un mois précis), barres de progression avec repère de la part du mois écoulée, dépassements.
- **Tableau de bord** (SVG fait main) : dépenses par catégorie (anneau), évolution du solde sur 12 mois, revenus vs dépenses sur 12 mois, budgets, échéances à venir.
- **Export CSV** (séparateur `;`, virgule décimale, UTF-8 avec BOM pour Excel).

Données : SQLite (`module.db`) dans le dossier de données du module. L'UI passe par le bridge (`window.ledroide.call`).

Limites : les opérations récurrentes ne sont générées que lorsque le module est ouvert (rattrapage au lancement suivant) ; une seule devise (€).
