# Convertisseur de formats

Convertit des données structurées entre **JSON, YAML, TOML, XML, CSV, .env et INI**.

- Éditeur source / résultat avec numéros de ligne, conversion en direct, détection automatique du format source.
- Validation : l'erreur indique la ligne et la colonne (clic = aller à la ligne, ligne surlignée dans la marge).
- Options : indentation (compacte, 2, 4), tri des clés, typage des valeurs (true/false/nombres pour CSV, XML, INI, .env), imbrication, séparateur CSV, nom de la racine XML.
- Échange source ⇄ cible, « Reformater » la source dans son propre format, import (bouton, `Ctrl+O` ou glisser-déposer) et export de fichier (`Ctrl+S`).

## Conventions
- **XML** : attributs → clés `@attr`, texte d'un élément qui a aussi des attributs/enfants → `#text`, éléments répétés → liste, élément vide → `null`. À l'écriture, un objet à une seule clé devient la racine, sinon la racine est `root` (configurable) ; une liste donne des éléments `<item>`. Les noms invalides sont corrigés avec un avertissement. Les espaces de noms sont ignorés ; les DTD internes sont refusées.
- **CSV** : tableau d'objets ; colonnes `a.b` ⇄ objets imbriqués ; les listes sont écrites en JSON dans la cellule.
- **.env** : `CLE=valeur`, `export`, guillemets et commentaires ; `DB__HOST` ⇄ `{"DB": {"HOST": …}}`.
- **INI** : clés hors section en tête, sections `[a.b]` ⇄ objets imbriqués, `[DEFAULT]` traité comme une section ordinaire.
- **TOML** : écriture par un sérialiseur intégré (tables, tableaux de tables, tables en ligne) ; `null` n'existe pas en TOML et est omis avec un avertissement.
- Les entiers à zéro initial (`007`, codes postaux) restent des chaînes.

Aucune dépendance à installer : bibliothèque standard Python (`json`, `tomllib`, `xml.etree`, `csv`, `configparser`) et PyYAML embarqué.

Raccourcis : `Ctrl+Entrée` convertir, `Alt+X` échanger, `Ctrl+Maj+C` copier, `Ctrl+O` importer, `Ctrl+S` exporter.
