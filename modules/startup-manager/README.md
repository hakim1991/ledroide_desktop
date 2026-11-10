# Démarrage Windows

Liste et gère les programmes lancés au démarrage de Windows.

- Sources : registre `HKCU` / `HKLM` `…\CurrentVersion\Run` et `RunOnce` (y compris `WOW6432Node`), dossiers Démarrage utilisateur et commun (raccourcis `.lnk` résolus).
- Pour chaque entrée : nom, commande, emplacement, portée (utilisateur / machine), éditeur et description lus dans les informations de version de l'exécutable, alerte si le fichier est introuvable.
- Désactivation **réversible**, comme dans le Gestionnaire des tâches de Windows : valeurs binaires `Explorer\StartupApproved\Run|Run32|StartupFolder` (premier octet `02` = activé, `03` = désactivé, avec la date de désactivation). Les entrées `RunOnce` ne sont pas désactivables.
- Ajout d'une entrée dans `HKCU\…\Run` (aucun droit administrateur nécessaire), suppression avec confirmation (valeur de registre ou raccourci du dossier Démarrage).
- Onglet « Tâches planifiées » : tâches du Planificateur Windows déclenchées à l'ouverture de session ou au démarrage, en lecture seule (tâches Microsoft masquées par défaut).

Les entrées « Machine » (HKLM, dossier commun) nécessitent de lancer LedroideDesktop en administrateur ; sinon un message clair est affiché. Aucune dépendance (bibliothèque standard `winreg` + `ctypes`, PowerShell pour les raccourcis et les tâches).

Raccourcis : `F5` actualiser, `Ctrl+F` rechercher, `Ctrl+N` ajouter.
