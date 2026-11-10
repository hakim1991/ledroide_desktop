# Actions presse-papier

Analyse le contenu du presse-papier et propose des actions adaptées, à copier en un clic.

- Bouton **Analyser** (`F5`) et **surveillance** optionnelle (toutes les secondes, seulement quand le module est affiché)
- Types détectés : URL, e-mail, JSON, JWT, couleur (hex / rgb / hsl), IP et CIDR, timestamp Unix et dates, chemin de fichier ou de dossier (y compris fichiers copiés dans l'Explorateur), UUID, Base64 / data URL, nombre et expression mathématique, téléphone, coordonnées GPS (décimal ou DMS), liens contenus dans un texte, et toujours « Texte »
- Exemples d'actions : JSON formaté / minifié / YAML ; couleur → aperçu, HEX/RGB/HSL/CMJN, contraste ; timestamp → dates ; URL → ouvrir, décoder, retirer les traqueurs, détail des paramètres ; JWT → en-tête et payload décodés, expiration ; Base64 → texte ou aperçu d'image ; chemin → ouvrir, afficher dans l'Explorateur, formats de chemin ; calcul → résultat (hex, binaire…) ; GPS → OpenStreetMap / Google Maps
- Les calculs utilisent un évaluateur maison (analyseur syntaxique, sans `eval`) avec garde-fous contre les résultats démesurés
- Saisie libre possible (`Ctrl+Entrée` pour analyser le texte saisi) ; touches `1`…`9` pour copier une action de la première carte

Lecture/écriture du presse-papier côté backend (API Windows via ctypes), avec repli sur `navigator.clipboard`.
Aucune dépendance Python. Les liens ne sont ouverts que pour les schémas http(s), mailto, tel et ftp.
