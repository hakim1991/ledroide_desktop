# 🖧 Configuration IP

`ipconfig` amélioré pour Windows.

- **Cartes réseau** : nom, description, état, type (Wi-Fi, Ethernet, VPN, virtuelle…), vitesse, MAC, IPv4/IPv6 + préfixe/masque, passerelle, serveurs DNS, DHCP (serveur, bail obtenu / expirant). Filtre pour masquer les cartes virtuelles ou déconnectées. Copie au format `ipconfig /all` ou affichage de la sortie brute.
- **Actions** (avec confirmation) : `ipconfig /release`, `/renew` (par carte ou toutes), `/flushdns`, `/registerdns`. En mode standard, les actions qui l'exigent sont lancées en administrateur via une invite UAC.
- **Table ARP** (`Get-NetNeighbor`), **Table de routage** (`Get-NetRoute`, métrique effective), **Cache DNS** (`Get-DnsClientCache`) avec filtres et tri.

Données obtenues via PowerShell (Get-NetAdapter, Get-NetIPAddress, Get-NetIPInterface, Win32_NetworkAdapterConfiguration). Aucune dépendance Python.
