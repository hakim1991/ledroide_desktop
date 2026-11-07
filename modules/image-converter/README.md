# 🖼️ Convertisseur d'images

Conversion et redimensionnement d'images par lot, en local.

- Formats de sortie : **PNG, JPG, WebP, BMP, ICO** (entrée : tout format lu par Pillow).
- Redimensionnement : dimensions maximales (proportions conservées, sans agrandissement) ou pourcentage.
- Qualité réglable pour JPG / WebP.
- Option de suppression des métadonnées EXIF (l'orientation est appliquée aux pixels avant).
- Résultat : un fichier unique, ou une archive ZIP pour plusieurs images ; tailles avant / après affichées.

Notes : JPG et BMP ne gèrent pas la transparence (fond blanc). ICO : image rendue carrée, tailles 16 à 256 px.
Dépendance Python : `Pillow`. Fichiers temporaires dans `%TEMP%\ledroide_image-converter` (nettoyés après 1 h).
