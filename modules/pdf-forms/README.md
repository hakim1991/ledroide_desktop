# Formulaires PDF

Crée des modèles de formulaires dynamiques et génère des PDF avec en-tête et pied de page.

- **Champs** : texte, texte long, nombre (décimales, unité), date, e-mail, téléphone, liste, choix unique, case à cocher, tableau (colonnes texte/nombre, totaux), signature, plus des éléments de mise en page (section, paragraphe avec variables, espace, saut de page). Réordonnables par glisser-déposer, en pleine ou demi-largeur, obligatoires ou non.
- **En-tête** : logo (PNG, JPEG, WebP, GIF ; 2 Mo max.) à gauche ou à droite, titre, sous-titre, texte libre (adresse…), alignement, filet, option « première page seulement ».
- **Pied de page** : trois zones (gauche, centre, droite), avec filet.
- **Variables** : `{{cle}}` pour la valeur d'un champ, `{{cle_total}}` pour le total d'un tableau, ainsi que `{{date}}`, `{{heure}}`, `{{annee}}`, `{{page}}`, `{{pages}}`, `{{doc_title}}` et `{{template}}`. Elles sont utilisables dans le titre du document, l'en-tête, le pied de page, les paragraphes et le nom du fichier.
- **Mise en page** :
  - formats A4, A5, A3, Letter et Legal, en portrait ou en paysage ;
  - marges, police Windows (Arial, Calibri, Segoe UI, Verdana, Times, Georgia) avec repli sur Helvetica, taille du texte et couleur d'accent ;
  - champs affichés « libellé au-dessus » ou en tableau « libellé | valeur » ;
  - option pour masquer les champs vides.
- **Génération** :
  - le PDF est enregistré dans le dossier configuré (par défaut, le dossier de données du module), puis ouvert ;
  - « Aperçu » ouvre un brouillon sans vérifier les champs obligatoires ;
  - « Télécharger » enregistre le PDF via le navigateur.
- **Par lot** : à partir d'un CSV, un PDF est généré par ligne dans un sous-dossier. Les colonnes sont associées aux champs par clé ou par libellé.
- **Historique** : la liste des documents générés permet de les rouvrir et de recharger leurs valeurs dans le formulaire.
- **Brouillon** : les valeurs saisies sont enregistrées automatiquement.
- **Modèles** : import et export en JSON, duplication. Deux exemples sont fournis : « Bon d'intervention » et « Attestation ».

Raccourcis : `Ctrl+S` enregistre le modèle, `Ctrl+Entrée` génère le PDF, `Ctrl+P` ouvre l'aperçu.

Le module dépend de `fpdf2`, installé à l'activation.
