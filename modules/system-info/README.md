# Inventaire système

Inventaire complet du PC (équivalent msinfo32 / systeminfo), en lecture seule :

- **Système** : nom d'hôte, domaine / groupe de travail, édition et build de Windows, date d'installation, dernier démarrage et durée de fonctionnement, utilisateur, fuseau horaire, état d'activation.
- **Matériel** : fabricant / modèle / n° de série, BIOS/UEFI, Secure Boot, TPM, processeur, barrettes mémoire, cartes graphiques (pilotes), moniteurs.
- **Stockage** : disques physiques (type, bus, santé via Get-PhysicalDisk), volumes avec barres d'utilisation et état BitLocker (lu sans droits admin).
- **Logiciels installés** (registre HKLM 64/32 bits + HKCU) : recherche, tri, export CSV.
- **Mises à jour** (Get-HotFix), **Démarrage** (clés Run, dossiers Démarrage, Win32_StartupCommand), **Variables d'environnement** (recherche).

Les sections sont collectées en parallèle (CIM/PowerShell, registre) et mises en cache ; « Actualiser » relance la collecte. Export du rapport complet en HTML autonome ou JSON. Aucune dépendance.
