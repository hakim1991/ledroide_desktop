# Transcription audio

Transcrit un fichier audio ou vidéo en texte, entièrement hors ligne, avec **Whisper** (`faster-whisper`, CPU int8).

- Entrée : fichier choisi, glissé-déposé ou chemin local ; conversion automatique en WAV 16 kHz mono via ffmpeg (`PATH` ou `imageio-ffmpeg`)
- Modèles `tiny`, `base`, `small`, `medium` (75 Mo à 1,5 Go) téléchargés au premier usage depuis Hugging Face et mis en cache dans le dossier de données du module (suppression possible)
- Langue automatique ou forcée, traduction vers l'anglais, qualité précise/rapide, filtre des silences (VAD), contexte/vocabulaire
- Tâche de fond avec progression, **segments affichés au fil de l'eau**, annulation
- Résultat horodaté, lecture synchronisée (clic sur un segment), copie, export **TXT**, TXT horodaté, **SRT**, **VTT**
- Historique des transcriptions (taille configurable)

Dépendances Python : `faster-whisper` (≈ 250 Mo avec ctranslate2/onnxruntime/PyAV) et `imageio-ffmpeg`, installées à
l'activation. Si `faster-whisper` est absent, le module l'indique clairement ; l'historique et les exports restent
utilisables. Paramètres : précision de calcul, nombre de threads CPU, taille de l'historique.
Raccourcis : `Ctrl+Entrée` transcrire, `Ctrl+Maj+C` copier le texte.
