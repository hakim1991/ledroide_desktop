# Git Viewer

Visionneuse **en lecture seule** d'un dépôt Git local, via la commande `git` installée sur la machine (message clair si elle est absente).

- Sélection du dépôt par chemin (n'importe quel sous-dossier) et liste des dépôts récents.
- Statut : branche, branche suivie, avance/retard, fichiers indexés, modifiés, non suivis, conflits, dépôts distants.
- Historique paginé avec graphe des branches (`git log --graph`), filtre par branche/tag, recherche par message, auteur, fichier ou contenu modifié (`-S`).
- Détail d'un commit : message, auteur, parents cliquables, diff coloré par fichier (repliable), renommages, fichiers binaires ; fusions comparées au premier parent.
- Branches locales/distantes (suivi), tags (annotés ou non), liste des stash avec leur diff, blame d'un fichier (copie de travail ou révision).
- `git fetch --all` uniquement sur clic explicite ; aucune autre commande ne modifie le dépôt.

Sécurité : liste blanche de sous-commandes (`stash list` seulement), arguments passés en liste (jamais de shell), références et chemins validés, `core.fsmonitor`, textconv et diff externes désactivés, `GIT_OPTIONAL_LOCKS=0`, aucune invite de mot de passe (`GIT_TERMINAL_PROMPT=0`), fenêtre console masquée.
Raccourcis : `Entrée` ouvrir, `F5` actualiser, `Ctrl+F` rechercher, `↑`/`↓` commit précédent/suivant, `Échap` fermer le détail.
