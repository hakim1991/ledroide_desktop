# EXIF

Affiche et nettoie les métadonnées des photos (JPEG, PNG, WebP, TIFF).

- Ouverture par chemin (fichier ou dossier) ou par glisser-déposer (copie temporaire, supprimée à la fermeture du module).
- Métadonnées regroupées : appareil, objectif, exposition, dates, logiciel/auteur, image, autres (dont texte PNG et XMP).
- Position GPS convertie en degrés décimaux, altitude, lien OpenStreetMap ; miniature (orientation EXIF appliquée).
- Suppression de **toutes** les métadonnées (seule l'orientation est conservée pour que l'image reste droite) ou **seulement du GPS** → copie `nom_clean.ext`. L'original n'est remplacé que si « Écraser l'original » est coché et confirmé.
- JPEG traité sans perte (seuls les segments de métadonnées sont réécrits) ; PNG/TIFF sans perte ; WebP avec perte réencodé en qualité 95 (WebP sans perte conservé sans perte).
- Modification de la date de prise de vue (DateTimeOriginal, DateTimeDigitized, DateTime) → copie `nom_date.ext`.
- Traitement par lot d'un dossier (sous-dossiers optionnels, dossier de sortie optionnel), en tâche de fond annulable.

HEIC/HEIF/AVIF : non pris en charge (message explicite). Images animées / TIFF multipages : non modifiables.
Raccourcis : `Ctrl+O` choisir des fichiers, `↑`/`↓` image précédente/suivante.
