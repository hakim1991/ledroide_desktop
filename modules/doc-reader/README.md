# Lecteur de documents

Bibliothèque et liseuse pour fichiers **PDF** et **EPUB**.

- **Bibliothèque** : ajout d'un fichier ou d'un dossier (chemin absolu, sous-dossiers inclus) ou glisser-déposer (le fichier est alors copié dans les données du module). Couvertures en miniature, titre et auteur modifiables, progression de lecture, collections, tri et recherche.
- **PDF** : pages rendues côté serveur par `pypdfium2`, zoom (ajusté à la largeur ou libre), mode page par page ou défilement continu, recherche plein texte avec surlignage des occurrences, table des matières (signets du PDF).
- **EPUB** : sommaire, chapitres convertis en HTML assaini (aucun script, style, formulaire ni ressource externe ; seules les images internes sont servies), taille de police, interligne, thèmes sombre / clair / sépia.
- **Annotations** : marque-pages, surlignages (zone tracée sur une page PDF, sélection de texte dans un EPUB) et notes, par document ; export Markdown.
- **Reprise** : chaque document s'ouvre à la dernière position lue.

Raccourcis : ←/→ page ou chapitre précédent/suivant, Ctrl+F recherche, Ctrl+B marque-page, +/− zoom, Échap retour.

Dépendances : `pypdfium2`, `Pillow`.
