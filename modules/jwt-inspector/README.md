# JWT Inspector

Décode, vérifie et génère des JSON Web Tokens.

- **Décoder** : header et payload en JSON coloré, jeton coloré par partie, claims avec libellés ; `exp`, `iat`, `nbf` affichés en date locale avec « expire dans 2 heures » / « expiré depuis 3 jours ». Alertes : `alg: none`, jeton expiré ou pas encore valide, `exp` absent, dates en millisecondes, JWE (5 parties).
- **Vérifier** : HS256/384/512 avec un secret (texte, Base64 ou hexadécimal) ; RS*, PS*, ES* (P-256/384/521) et EdDSA avec une clé publique PEM, un certificat X.509 ou la clé privée.
- **Générer** : header et payload éditables, boutons `iat = maintenant`, `exp +1 h`, `exp +24 h` ; génération d'un secret ou d'une paire de clés de test ; « Modifier et re-signer » depuis un jeton décodé.

Le décodage et HS* utilisent uniquement la bibliothèque standard. Les algorithmes asymétriques nécessitent `cryptography` (installée à l'activation) ; sans elle le module reste utilisable pour le décodage et HS*. Les secrets ne sont ni enregistrés ni journalisés.

Raccourcis : `Alt+1/2/3` pour changer d'onglet, `Ctrl+Entrée` pour vérifier / générer.
