# Traducteur de fichiers

Traduit des fichiers avec l'API Claude en préservant leur format (SDK Python officiel `anthropic`, aucune autre dépendance).

| Format | Traduit | Préservé |
|---|---|---|
| SRT / WebVTT | texte des répliques | numéros, minutages, réglages de cue, `NOTE`/`STYLE`/`REGION`, balises `<i>`, `<v …>`, `{\an8}`, fins de ligne, BOM |
| Markdown | paragraphes, titres, listes, citations, cellules de tableau, textes de liens et `alt` d'images | blocs de code, code en ligne, URL, HTML, en-tête YAML, définitions de liens, ancres `{#id}` |
| Texte | paragraphe par paragraphe | lignes vides, espaces, URL |
| Word `.docx` | texte des paragraphes (corps, tableaux, en-têtes, pieds de page, notes) | tout le reste du paquet ; le XML est modifié en place (`<w:t>` uniquement), namespaces intacts |
| JSON i18n | valeurs texte | clés, ordre, indentation, nombres ; `{x}`, `{{x}}`, `%s`, `%d`, `%1$s`, `%(x)s`, `$t(…)`, `@:clé`, balises HTML, messages ICU (laissés tels quels) |

## Fonctionnement

- Les éléments à protéger sont remplacés par des marqueurs `⟦n⟧` avant l'envoi, puis restaurés ; un marqueur perdu est réinséré et le segment signalé « à vérifier ».
- `.docx` : les runs consécutifs de même mise en forme sont regroupés ; si un paragraphe mêle plusieurs mises en forme, chaque portion est encadrée par `⟪n⟫…⟪/n⟫` et le modèle replace la traduction de chaque portion (si les balises sont perdues, tout le texte va dans la première portion : « mise en forme simplifiée »).
- Envoi **par lots** (6 000 caractères par défaut, 3 lots en parallèle) au format `<seg id="N">…</seg>`, avec le contexte des segments voisins pour les sous-titres. Langue source auto ou imposée, langue cible libre, **ton**, **glossaire** imposé (enregistrable par défaut), consignes libres.
- Tâche longue via `module.job` : **progression** en direct, **annulation**, état sauvegardé après chaque lot → **reprise** après erreur ou annulation (seuls les segments restants sont traduits). Erreurs transitoires (429, 529, 5xx, réseau) réessayées automatiquement.
- **Aperçu côte à côte** paginé (filtres : avertissements, non traduits), correction manuelle d'un segment, **téléchargement** (`film.en.srt` → `film.fr.srt`, `en.json` → `de.json`).

Clé `ANTHROPIC_API_KEY` dans le menu **« Variables »**. Options : modèle par défaut (`claude-opus-5-5`), effort (`low` par défaut, adapté à la traduction), taille des lots, parallélisme. Fichiers jusqu'à 10 Mo / 50 000 segments ; sources et état dans le dossier de données du module.
