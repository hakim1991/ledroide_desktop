# Capture d'écran

Captures et enregistrements de l'écran via Python (`mss` + `Pillow`, vidéo avec `imageio-ffmpeg`).

- **Capture** : tous les écrans, un écran (aperçus pour le choisir si plusieurs) ou une **zone** (capture de l'écran choisi puis tracé de la zone sur l'image affichée). **Retardateur** 3 / 5 / 10 s avec compte à rebours.
- **Éditeur** (canvas) : flèches (Maj = horizontale/verticale), rectangles, texte, surligneur, pixellisation et flou de zones, recadrage ; couleurs, épaisseur, sélection/déplacement/suppression d'une annotation, annuler/rétablir. Raccourcis : V A R T H P B C, Ctrl+Z / Ctrl+Y, Ctrl+C (copier), Ctrl+S (enregistrer), Suppr.
- **Sorties** : copie dans le presse-papiers Windows (formats DIB et PNG), téléchargement PNG/JPG (boîte d'enregistrement), enregistrement direct dans un dossier (PNG ou JPG avec qualité), remplacement de la capture ou nouvelle version dans l'historique.
- **Historique** avec miniatures, filtre par type, suppression multiple. Fichiers dans le dossier de données du module (`captures/`).
- **Vidéo** : enregistrement d'un écran ou de tous en MP4 (H.264), 5 à 30 images/s, durée maximale (15 s à 30 min), départ différé, arrêt à tout moment. **Export GIF** court (début, durée ≤ 30 s, images/s, largeur ; palette optimisée).

Limites : pas de raccourci clavier global (un module ne peut pas enregistrer de raccourci système : utilisez le retardateur pour capturer une autre fenêtre — sinon la fenêtre de l'application apparaît sur la capture) ; pas de capture d'une fenêtre précise ; le curseur de la souris et le son ne sont pas enregistrés ; la cadence vidéo réelle dépend de la machine (images dupliquées si la capture prend du retard).

Données : SQLite (`module.db`) + fichiers dans le dossier de données du module.
