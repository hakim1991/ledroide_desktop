# Terminal SSH

Terminal SSH multi-onglets avec accès enregistrés et explorateur de fichiers distant (SFTP).

- **Accès enregistrés** :
  - nom, groupe, couleur, hôte, port, utilisateur, dossier initial et notes ;
  - recherche et groupes repliables ;
  - import depuis `~/.ssh/config`.
- **Authentification** :
  - par mot de passe (ou keyboard-interactive) ;
  - par clé privée : fichier OpenSSH, PEM ou PuTTY, ou contenu collé, avec phrase de passe éventuelle ;
  - par agent SSH (OpenSSH ou Pageant) et clés par défaut de `~/.ssh`.
- **Secrets** :
  - mots de passe, clés collées et phrases de passe sont chiffrés avec le compte Windows (DPAPI) ;
  - ils ne sont jamais renvoyés à l'interface ;
  - si vous ne les enregistrez pas, ils sont demandés à chaque connexion.
- **Clés des serveurs** :
  - à la première connexion, l'empreinte de la clé du serveur est affichée pour validation, puis mémorisée ;
  - si elle change, la connexion est bloquée avant tout envoi du mot de passe, avec un avertissement ;
  - la liste des clés connues (🔑) permet d'en oublier une.
- **Terminal** :
  - xterm.js : couleurs, UTF-8, redimensionnement automatique, 5 000 lignes d'historique par défaut ;
  - copier-coller : `Ctrl+C` copie la sélection (sinon interrompt), `Ctrl+V` colle, et le clic droit ouvre un menu ;
  - plusieurs sessions en onglets ; `Ctrl+Maj+T` duplique la session, `Ctrl+Tab` passe à l'onglet suivant ;
  - les sessions restent ouvertes si l'onglet du module est rechargé et sont rattachées avec leur historique ;
  - « Reconnecter » après une déconnexion.
- **Explorateur de fichiers (SFTP)**, à côté du terminal, en dessous, ou seul :
  - liste triable (nom, taille, date, droits, propriétaire), fil d'Ariane, chemin éditable, filtre, fichiers cachés ;
  - sélection multiple (`Ctrl`/`Maj`) et menu contextuel ;
  - créer un dossier ou un fichier, renommer (`F2`), supprimer (`Suppr`, récursif pour les dossiers), changer les droits (cases ou octal) ;
  - éditeur de texte intégré (fichiers de 2 Mo maximum) :
    - `Ctrl+S` enregistre ;
    - l'encodage et les fins de ligne sont conservés ;
    - si le fichier a été modifié sur le serveur entre-temps, l'enregistrement est refusé et un écrasement est proposé ;
  - envoi par bouton ou glisser-déposer de fichiers (par blocs, avec progression, annulation, et confirmation avant de remplacer un fichier) ;
  - téléchargement de fichiers ou de dossiers entiers vers `Téléchargements\Ledroide SSH` (configurable), avec progression ;
  - « ⌨ » ouvre le dossier courant dans le terminal (`cd`).

Le module dépend de `asyncssh` et de `bcrypt` (clés OpenSSH chiffrées), installés à l'activation. xterm.js (licence MIT) est inclus dans `frontend/vendor/`.
