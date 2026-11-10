# Testeur SMTP

- **Envoi de test** : serveur, port, sécurité (aucune / STARTTLS / SSL), authentification, expéditeur, destinataires (À, Cc), sujet, corps texte et/ou HTML, pièce jointe (20 Mo max). Le dialogue SMTP complet est affiché ; les identifiants y sont masqués.
- **Tester la connexion** : connexion + EHLO, bannière, extensions ESMTP annoncées, version TLS, et test d'authentification si des identifiants sont saisis.
- **Serveur de capture** : faux serveur SMTP asyncio (HELO/EHLO, MAIL, RCPT, DATA, RSET, NOOP, QUIT, VRFY, AUTH PLAIN/LOGIN acceptés sans vérification) sur 127.0.0.1:2525 par défaut. Les mails reçus ne sont jamais délivrés : ils sont stockés (SQLite) et affichés en direct — en-têtes, texte, HTML rendu dans une iframe sandbox sans scripts (images distantes bloquées par défaut), source, `.eml` et pièces jointes téléchargeables.

Le mot de passe n'est jamais enregistré ni journalisé. Pas de STARTTLS sur le serveur de capture. Aucune dépendance.
