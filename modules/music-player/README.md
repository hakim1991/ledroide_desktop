# Lecteur audio et podcasts

- **Bibliothèque** : dossiers indexés (tâche de fond, ré-indexation incrémentale), tags lus avec `mutagen` (titre, artiste, album, n° de piste, année, genre, durée, pochette intégrée ou `cover.jpg` / `folder.jpg`). Vues Titres, Artistes, Albums, Récents ; recherche instantanée.
- **Lecture** : élément `<audio>` alimenté par la route `/stream/{id}` du module (requêtes Range prises en charge ; seuls les fichiers de la bibliothèque sont servis), file d'attente modifiable, aléatoire, répétition (tout / un), volume, égaliseur 10 bandes WebAudio avec préréglages.
- **Playlists** : création, ajout depuis n'importe quelle vue, réorganisation par glisser-déposer.
- **Podcasts** : abonnement par URL de flux RSS, liste des épisodes, lecture en streaming (relayée par le module, avec Range) ou téléchargement dans les données du module, reprise à la dernière position, marquage lu / non lu.

Raccourcis : Espace lecture/pause, ←/→ ±10 s, Ctrl+←/→ piste précédente/suivante, ↑/↓ volume, M muet, S aléatoire, R répétition, Ctrl+F recherche.

Dépendance : `mutagen`.
