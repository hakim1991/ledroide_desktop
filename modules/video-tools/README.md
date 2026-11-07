# 🎬 Outils vidéo

Boîte à outils vidéo basée sur **ffmpeg** :

- **Informations** : durée, résolution, codecs, images/s, débit (ffprobe).
- **Couper** : début / fin (secondes ou hh:mm:ss), mode rapide sans réencodage.
- **Compresser** : curseur CRF (H.264) et réduction optionnelle en 1080p / 720p / 480p.
- **Convertir en GIF** : début, durée, images/s, largeur — deux passes avec palette optimisée.
- **Extraire des images** : une image toutes les N secondes, archive ZIP (JPEG ou PNG).
- **Extraire l'audio** : MP3 (128 à 320 kb/s).

Les traitements tournent en arrière-plan avec barre de progression et annulation.

**Prérequis** : `ffmpeg` dans le PATH (recommandé). À défaut, le module utilise `imageio-ffmpeg`
(installable via le bouton « dépendances »). Fichiers temporaires : `%TEMP%\ledroide_video-tools` (purgés après 24 h).
