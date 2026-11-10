# Scanner réseau

Outil d'administration de **votre propre réseau**. À utiliser uniquement sur des réseaux et machines que vous administrez.

- **Scan de ports TCP** d'un hôte : liste/plages (`22,80,443,8000-8100`) ou presets (courants, web, 1-1024, tous), connexions asyncio simultanées configurables, délai réglable, nom du service connu, bannière courte optionnelle (lecture passive, `HEAD /` sur les ports HTTP).
- **Découverte d'hôtes** sur une plage CIDR (/22 maximum) : `ping` de chaque adresse, lecture de la table ARP (adresse MAC), DNS inverse, constructeur déduit du préfixe MAC (petite table OUI intégrée).
- Réseau local détecté et pré-rempli, progression en direct, annulation, tri des colonnes, export CSV.

Raccourcis : `Ctrl+Entrée` lancer, `Échap` arrêter. Aucune dépendance (bibliothèque standard + `ping`/`arp` de Windows).
Sous Windows, une connexion vers un port fermé peut ne pas être refusée immédiatement : avec un délai court, il apparaît « sans réponse ».
