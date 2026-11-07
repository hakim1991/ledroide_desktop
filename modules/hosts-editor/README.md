# Fichier hosts

Éditeur du fichier `C:\Windows\System32\drivers\etc\hosts` :

- Vue tableau (IP, noms d'hôte, commentaire, entrée active / désactivée = ligne commentée) et onglet texte brut.
- Ajout, modification, activation/désactivation et suppression d'entrées avec validation (IPv4/IPv6, syntaxe des noms d'hôte).
  Les commentaires, lignes vides et l'ordre d'origine sont conservés.
- Préréglages : bloquer un domaine (`0.0.0.0`) ou le rediriger vers localhost.
- Enregistrement : sauvegarde horodatée automatique (10 dernières dans `backend/backups`, restaurables),
  détection des modifications externes, vidage optionnel du cache DNS.
- Sans droits administrateur, l'écriture passe par un fichier temporaire copié via une invite UAC.

Variable d'environnement `LEDROIDE_HOSTS_PATH` : utilise un autre fichier (tests).

Aucune dépendance Python.
