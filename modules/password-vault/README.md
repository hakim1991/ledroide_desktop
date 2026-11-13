# Coffre de mots de passe

Gestionnaire de mots de passe local, chiffré par un mot de passe maître.

- **Chiffrement** : clé dérivée par scrypt (N=2^17, r=8, p=1), chaque entrée chiffrée en AES-256-GCM (`cryptography`). Titre, identifiant, URL, notes, dossier, tags et dates sont tous dans le bloc chiffré : rien n'est en clair sur disque (`vault.db` dans le dossier de données du module). Seuls les réglages (délai de verrouillage, effacement du presse-papiers) sont en clair.
- **Verrouillage** : manuel (Ctrl+L), automatique après inactivité (réglable), et quand le module est masqué (option). La clé est effacée de la mémoire au verrouillage. Le mot de passe maître et la clé ne sont jamais journalisés ni renvoyés.
- **Changement du mot de passe maître** : toutes les entrées sont re-chiffrées dans une seule transaction.
- **Entrées** : titre, identifiant, mot de passe (avec historique des 5 précédents), URL, notes, dossier (sous-dossiers avec `/`), tags, favori, code TOTP.
- **Générateur** : longueur, classes de caractères, exclusion des caractères ambigus, phrases de passe (liste de ~900 mots français), entropie.
- **Robustesse** : estimation (dictionnaire, suites, mots courants) ; vues « Faibles » et « Réutilisés ».
- **TOTP** (RFC 6238) : secret base32 ou URI `otpauth://totp/…`, SHA1/256/512, 6 à 8 chiffres, compte à rebours.
- **Presse-papiers** : copie avec effacement automatique (par défaut 20 s).
- **Import** CSV Chrome, Firefox, Bitwarden (ou générique) ; **export chiffré** (fichier `.vault.json` protégé par un mot de passe d'export) réimportable.

Mot de passe maître perdu = coffre perdu (aucune récupération possible).
