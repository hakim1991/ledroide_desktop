# Encodeur / Décodeur

Transformations de texte en direct, utilisables seules ou enchaînées.

- **Web** : URL (composant, formulaire avec `+`, URL complète), entités HTML (minimal ou tout non-ASCII ; décodage des entités nommées et numériques), IDNA (`münchen.de` ⇄ `xn--mnchen-3ya.de`, une ligne par domaine) et Punycode brut.
- **Octets** (texte encodé en UTF-8) : Base64 (étape de chaîne ; pour les fichiers, voir le module Base64), Base32 (RFC 4648), hexadécimal (`0x`, `\x`, espaces et `:` acceptés au décodage), binaire, quoted-printable.
- **Texte** : échappements Unicode `\uXXXX` (décodage aussi de `\u{1F600}`, `\xNN`, `\UXXXXXXXX`), ROT13.
- **Casse** : camelCase, PascalCase, snake_case, kebab-case, CONSTANT_CASE, dot.case, Titre, Phrase, minuscules, MAJUSCULES, casse inversée, texte à l'envers (ligne par ligne pour les conversions de mots).
- **Compteurs** sur l'entrée et la sortie : caractères, graphèmes, octets UTF-8, mots, lignes.

Chaîne de transformations : clic dans la liste = remplacer, `Maj`+clic = ajouter une étape ; chaque étape peut changer de sens, être déplacée ou retirée. « Chaîne inverse » inverse l'ordre et le sens de toutes les étapes pour revenir au texte d'origine. Une erreur indique l'étape fautive.

Tout est calculé dans l'interface, sauf IDNA/Punycode et quoted-printable (backend Python, bibliothèque standard). IDNA suit la norme IDNA 2003 de Python (`ß` devient `ss`). Le quoted-printable invalide en UTF-8 est relu en Latin-1.

Raccourcis : `Ctrl+K` filtrer, `Ctrl+Entrée` recalculer, `Ctrl+E` sortie → entrée, `Ctrl+Maj+C` copier la sortie.
