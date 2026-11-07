# 🔀 Comparateur de texte

Comparez deux textes collés ou deux fichiers (bouton « Ouvrir » ou glisser-déposer, détection UTF-8 / UTF-16 / Windows-1252).

- Vues **côte à côte** et **unifiée**, numéros de ligne.
- Surlignage des lignes ajoutées / supprimées / modifiées et des différences **intra-ligne** (par mot ou par caractère).
- Options : ignorer les espaces, ignorer la casse, repli des lignes identiques avec contexte réglable.
- Statistiques (+ / − / ~, similarité), navigation entre les différences, copie ou téléchargement du diff unifié (.patch).

Moteur : `difflib` (bibliothèque standard Python), aucune dépendance.
