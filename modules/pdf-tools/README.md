# 📑 Outils PDF

Boîte à outils PDF pour LedroideDesktop, entièrement locale.

- **Fusionner** : plusieurs PDF, ordre réglable par glisser-déposer, nombre de pages affiché.
- **Découper** : par plages (`1-3,5,8-`) → un PDF par plage, regroupés dans un ZIP ; ou un PDF par page.
- **Réorganiser / Pivoter** : réordonner, pivoter de 90°, supprimer des pages → nouveau PDF.
- **Compresser** : recompression sans perte des flux de contenu + suppression des objets en double ; option de recompression JPEG des images (qualité et dimension max. réglables).

Dépendances Python : `pypdf`, `Pillow` (bouton « Dépendances » du module).

Limites : les PDF protégés par mot de passe ne sont pas pris en charge ; les images avec transparence ne sont pas recompressées ; les vignettes de pages sont schématiques (pas de rendu).
Les fichiers générés sont stockés temporairement dans `%TEMP%\ledroide_pdf-tools` (nettoyés après 1 h).
