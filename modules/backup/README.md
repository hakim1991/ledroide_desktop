# Sauvegarde

Sauvegardes incrémentales par instantanés horodatés.

- **Jeux de sauvegarde** : un ou plusieurs dossiers sources, exclusions par motifs glob (`*.tmp`, `node_modules`, `Projet/build`), destination, mode de stockage, conservation, planification. Sélecteur de dossiers intégré (lecteurs, dossier personnel, création de dossier).
- **Destinations** : dossier local, disque externe, partage réseau UNC (`\\NAS\partage`), ou serveur **SFTP** (facultatif : nécessite `asyncssh`, à installer depuis le menu « Bibliothèques » s'il est absent ; empreinte de la clé du serveur approuvée au premier test puis vérifiée à chaque connexion ; mot de passe / phrase de passe lus dans des variables du menu « Variables », jamais stockés par le module).
- **Stockage** (fixé à la première sauvegarde d'une destination, dans `<destination>/<jeu>/`) :
  - *liens physiques* : chaque instantané est une arborescence complète (`snapshots/<date>/files/…`) consultable dans l'Explorateur ; les fichiers inchangés (même taille et même date) sont liés au précédent instantané au lieu d'être recopiés. Ne modifiez pas les fichiers d'un instantané : ils sont partagés avec les autres.
  - *dédupliqué* : contenu rangé une seule fois par empreinte SHA-256 (`store/ab/<sha256>`) + manifeste par instantané. Utilisé automatiquement si la destination ne gère pas les liens physiques (FAT/exFAT, certains partages), avec la compression ou en SFTP.
  - *compression* : deflate (algorithme du zip) par fichier, sauf formats déjà compressés. Un instantané (ou une sélection) peut aussi être **exporté en .zip**.
- Chaque instantané contient son manifeste (`manifest.json.gz`) : la destination se suffit à elle-même. Un instantané est écrit dans `<date>.partial` puis renommé : une sauvegarde interrompue ne laisse pas d'instantané incomplet.
- **Rotation** après chaque sauvegarde : N dernières + la plus récente de chacun des N derniers jours / semaines / mois ; aperçu avant rotation manuelle ; les blocs dédupliqués orphelins sont supprimés.
- **Planification** : manuelle, toutes les N heures, quotidienne, hebdomadaire (jours choisis). Tâche de fond tant que l'application est ouverte ; une échéance manquée est **rattrapée au démarrage** ; après un échec (disque débranché…), nouvel essai toutes les heures.
- **Vérification d'intégrité** : rapide (présence et taille) ou complète (relecture et contrôle SHA-256).
- **Navigation et restauration** : parcours et recherche dans un instantané, restauration de tout ou partie vers l'emplacement d'origine ou un autre dossier. Aperçu des conflits ; rien n'est écrasé sans choix explicite (ignorer, restaurer à côté « (restauré) », écraser après confirmation). Contenu restauré contrôlé par SHA-256, dates de modification conservées.
- **Journal** des exécutions (par jeu et global) avec détail et erreurs. Progression et annulation pour toutes les opérations, y compris planifiées.

Limites : pas de sauvegarde quand l'application est fermée (rattrapage au lancement suivant) ; liens symboliques et jonctions ignorés ; pas de chiffrement des sauvegardes ; les droits NTFS (ACL) ne sont pas sauvegardés.

Données du module : SQLite (`module.db`) — jeux et journal. Les instantanés sont uniquement dans la destination.
