# #️⃣ Hash & Checksum

Calcule en une fois MD5, SHA-1, SHA-256, SHA-512, SHA3-256, BLAKE2b et CRC32.

- **Texte** : calcul instantané (UTF-8)
- **Fichier** : glisser-déposer, barre de progression, hachage par blocs de 1 Mo (gros fichiers supportés)
- **Vérification** : collez la somme attendue (`abcd…`, `sha256:abcd…` ou une ligne `sha256sum`) → l'algorithme correspondant passe au vert, sinon message rouge
- **HMAC** avec clé secrète (MD5, SHA-1, SHA-256, SHA-512, SHA3-256, BLAKE2b)
- Copie individuelle ou globale, affichage en majuscules

Aucune dépendance externe (bibliothèque standard Python).

API : `POST /text`, `POST /file` (multipart, champ `hmac_key` optionnel), `GET /algorithms`.
