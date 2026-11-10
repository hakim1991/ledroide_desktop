# Notes chiffrées

Bloc-notes protégé par un mot de passe maître.

- Création du coffre au premier lancement (mot de passe + confirmation, indicateur de force, 8 caractères minimum)
- Clé de 256 bits dérivée par **scrypt** (N = 2¹⁷, r = 8, p = 1, sel aléatoire de 16 octets stocké) ; chiffrement **AES-256-GCM** (nonce aléatoire par écriture, identifiant de la note en données associées : une note copiée sur une autre est détectée comme altérée)
- Vérification du mot de passe par un « canary » chiffré ; changement de mot de passe avec re-chiffrement de toutes les notes dans **une seule transaction**
- Verrouillage manuel (`Ctrl+L`) et automatique après X minutes d'inactivité (réglage « Verrouillage automatique », 0 = jamais), à l'arrêt du module ou du backend
- Notes : titre + contenu texte / Markdown simple (aperçu `Ctrl+E`), enregistrement automatique, recherche sur les titres (déchiffrés en mémoire uniquement)

**⚠ Mot de passe perdu = notes perdues.** Aucune récupération possible ; « Mot de passe oublié ? » permet seulement de supprimer le coffre pour en recréer un.

Sécurité : la clé ne vit qu'en mémoire du backend (effacée au verrouillage) ; rien de déchiffré n'est écrit sur disque ni journalisé. Le fichier `vault.db` (dossier de données du module) ne contient que des données chiffrées ainsi que les dates de création/modification et la taille des notes, en clair. Python ne garantit pas l'effacement complet des copies de la clé ou du texte en mémoire.

Dépendance : `cryptography` (installée à l'activation du module).
