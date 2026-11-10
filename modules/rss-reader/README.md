# Lecteur RSS

- Abonnement par URL d'un flux **RSS 2.0 / RSS 1.0 / Atom**, ou d'une page web : le flux est découvert via `<link rel="alternate">`.
- Dossiers, renommage, désabonnement ; compteurs de non-lus ; rafraîchissement manuel (tout / un flux) et **automatique** (intervalle réglable dans la configuration, 0 = désactivé), avec requêtes conditionnelles (ETag / Last-Modified).
- Liste des articles (non lus en gras), filtres Tous / Non lus / Favoris, recherche, marquer lu / non lu / tout lu, favoris.
- Lecture : contenu HTML **assaini** (liste blanche de balises/attributs, liens http/https uniquement) puis rendu dans une iframe sandbox sans scripts avec CSP ; « Ouvrir dans le navigateur » via le navigateur par défaut.
- Import / export **OPML** (avec dossiers).

Analyse avec `xml.etree` (pas de feedparser), téléchargement `urllib` (http/https uniquement, 10 Mo max). Aucune dépendance.
Raccourcis : `J`/`K` article suivant/précédent, `S` favori, `M` lu/non lu, `O` ouvrir, `R` rafraîchir, `A` ajouter, `Maj+A` tout lu, `/` rechercher.
