# Wi-Fi

- **Connexion** : SSID, BSSID, signal (% et RSSI), canal, bande, norme radio, sécurité, débits ; graphique du signal en direct (toutes les 2 s).
- **Réseaux à proximité** (`netsh wlan show networks mode=bssid`) : tableau triable, nouveau balayage, graphiques d'occupation des canaux 2,4 / 5 / 6 GHz et canal conseillé (1/6/11).
  Sous Windows 11, cette liste exige l'accès à la **localisation** (Paramètres › Confidentialité et sécurité › Localisation) : un message et un bouton d'accès aux paramètres s'affichent si elle est refusée.
- **Profils enregistrés** : afficher / copier le mot de passe (à la demande, jamais stocké), oublier un profil (confirmation, élévation UAC proposée si nécessaire), export CSV de la liste sans mots de passe.

Sorties `netsh` analysées en français et en anglais. Aucune dépendance.
