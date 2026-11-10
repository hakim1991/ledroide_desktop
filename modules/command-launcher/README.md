# Lanceur de commandes

Boutons favoris pour lancer vos commandes Windows depuis LedroideDesktop.

- Chaque commande : nom, icône emoji, couleur, interpréteur (**cmd**, **PowerShell**, **Python**, **exécutable direct**), dossier de travail, variables d'environnement, délai maximum, description, confirmation optionnelle
- **Paramètres demandés à l'exécution** : `{{nom}}` ou `{{nom|défaut}}` dans la commande, le dossier ou les variables → formulaire (dernières valeurs mémorisées) ; valeurs aussi exposées en `LP_NOM`
- **Sortie en direct** (stdout / stderr en rouge), code retour, durée, bouton **Arrêter** (tue l'arbre de processus via `taskkill /T /F`), plusieurs exécutions simultanées en onglets, relance
- Historique des exécutions avec leur sortie (taille configurable)
- Groupes en onglets (renommer, réordonner, supprimer), réordonnancement par glisser-déposer, déplacement en déposant sur un onglet, recherche
- Import / export JSON ; exemples inoffensifs préremplis (ipconfig, ping, dir, git status, processus, disques, Python)

Exécution côté backend sans fenêtre console (`CREATE_NO_WINDOW`). Pour « exécutable direct », les arguments sont passés
en liste (pas de shell) ; pour cmd/PowerShell/Python, les valeurs des paramètres sont insérées telles quelles dans le
script. PowerShell est lancé via `-EncodedCommand` avec sortie UTF-8 ; la sortie de cmd est décodée en UTF-8 ou dans la
page de code OEM. Raccourcis : `Ctrl+N` nouvelle commande, `Ctrl+F` recherche, `Échap` arrêter l'exécution affichée,
clic droit sur une tuile pour la modifier. Aucune dépendance Python.
