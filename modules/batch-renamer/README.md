# ✏️ Renommage en masse

Renomme des fichiers par lot dans un dossier (chemin absolu à coller), avec aperçu en direct.

Règles (appliquées dans l'ordre) :
1. Rechercher / remplacer (texte simple ou expression régulière, groupes `\1`…).
2. Changement de casse (minuscules, MAJUSCULES, Titre, Phrase).
3. Numérotation séquentielle via un modèle : `photo_{n:03}` → `photo_001`, `{name}` = nom courant ; début, pas, tri par nom (naturel) ou date.
4. Préfixe / suffixe.

L'extension est conservée par défaut (casse réglable). Filtre d'extensions et sous-dossiers en option.

- **Aperçu** ancien → nouveau avec détection des conflits : noms en double, fichier déjà existant, caractères interdits, noms réservés Windows. Le renommage est refusé tant qu'il reste un conflit.
- **Application** en deux phases (noms temporaires), ce qui gère les permutations (`a→b`, `b→c`) ; en cas d'erreur tout est annulé.
- **Annulation** de la dernière opération : la correspondance est enregistrée dans `backend/rename_log.json`.

Aucune dépendance externe.
