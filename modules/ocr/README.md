# OCR

Extrait le texte d'images et de PDF scannés avec **Tesseract OCR**.

- Sources : fichier (bouton, glisser-déposer, chemin local), image **collée** avec `Ctrl+V`
- PDF et TIFF multi-pages (rendu PDF via `pypdfium2`), choix des pages (`1-3,5`), option « texte natif du PDF »
- Choix des langues installées (`tesseract --list-langs`) et du mode de mise en page (PSM)
- Prétraitement Pillow avec aperçu en direct : niveaux de gris, contraste, luminosité, binarisation (Otsu ou seuil), rotation, redressement, agrandissement, débruitage, netteté, inversion ; détection automatique de l'orientation (pack `osd`)
- Résultat éditable, copiable, téléchargeable en `.txt` ; aperçu avec les boîtes des mots colorées selon la confiance
- OCR en tâche de fond avec progression page par page et annulation

## Prérequis : Tesseract

Tesseract est un logiciel externe. Sous Windows, installez la version UB-Mannheim
(<https://github.com/UB-Mannheim/tesseract/wiki>) en cochant **French** dans *Additional language data*.
Le module cherche `tesseract.exe` dans le paramètre « Chemin de tesseract.exe », le `PATH`, puis
`C:\Program Files\Tesseract-OCR\`. Sans Tesseract, le module affiche un écran d'aide.

Dépendances Python : `pytesseract`, `pypdfium2` (installées à l'activation). Raccourcis : `Ctrl+Entrée` lancer,
`Ctrl+V` coller, `Ctrl+O` ouvrir, `Ctrl+Maj+C` copier le texte.
