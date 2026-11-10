# Générateur de données

Génère des jeux de données de test réalistes, sans dépendance externe (listes françaises intégrées).

- Constructeur de schéma : nom + type par champ, options propres au type, pourcentage de `NULL`, réordonnancement, duplication
- Types : séquence, UUID v4, entier / décimal (bornes, décimales), booléen (% de vrai), date (plage, heure optionnelle), prénom, nom, nom complet, email, téléphone FR (3 formats), adresse FR, ville, code postal, entreprise, **IBAN FR valide** (clé RIB + clé IBAN), **SIRET / SIREN valides** (Luhn), URL, IPv4 (publique/privée), IPv6, couleur hex, lorem ipsum (mots / phrases / paragraphes), valeur dans une liste, motif regex simple
- Cohérence par ligne : prénom / nom / nom complet / email décrivent la même personne ; ville, code postal et adresse complète correspondent
- Jusqu'à 100 000 lignes, générées en tâche de fond avec progression et annulation
- **Graine** optionnelle (nombre ou texte) : même graine + même schéma = mêmes données ; la graine utilisée est toujours affichée
- Aperçu automatique des 20 premières lignes ; export JSON (indenté ou compact), CSV (séparateur au choix), SQL `INSERT` (nom de table, lots de 100 lignes), NDJSON ; téléchargement ou copie
- Schémas enregistrables (base SQLite du module)

Regex prises en charge : littéraux, `.`, `\d \w \s \D \W`, classes `[a-z0-9_]` et `[^…]`, groupes `( | )` / `(?: )`, quantificateurs `? * + {n} {n,} {n,m}` (`*` et `+` limités à 5 répétitions).

Raccourcis : `Ctrl+Entrée` aperçu, `Ctrl+G` générer, `Ctrl+S` enregistrer le schéma, `Ctrl+Maj+A` ajouter un champ.
