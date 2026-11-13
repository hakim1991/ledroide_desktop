# Résumeur de documents

Résume et interroge des documents avec l'API Claude (SDK Python officiel `anthropic`).

- **Sources** : PDF (`pypdf`, texte par page avec repères « --- Page n --- »), Word `.docx` (lecture directe du XML, titres/listes/tableaux), texte et Markdown, HTML, **page web** (URL → extraction du texte principal : `<article>`/`<main>` ou bloc de contenu, menus, pieds de page, barres latérales, scripts et blocs de liens écartés ; un lien vers un PDF fonctionne aussi) ou **texte collé**. Fichiers jusqu'à 10 Mo.
- **Synthèses** : résumé court / moyen / détaillé, points clés, plan — dans la langue choisie (ou celle du document). Réponses en **streaming**, bouton « Arrêter », régénération.
- **Questions-réponses** ancrées au document (historique conservé, réponses partielles gardées si interrompues).
- **Historique** des documents (renommer, supprimer, rechercher), **export Markdown** de toutes les synthèses et des questions-réponses.

## Documents longs : stratégie

La taille est estimée à ~3,5 caractères par token. Le seuil est le réglage « Taille maximale envoyée d'un bloc » (150 000 tokens par défaut) :

- **Sous le seuil (cas courant)** : le document est envoyé **en entier** (lecture intégrale). C'est le plus fidèle ; le bloc document est placé en tête et marqué `cache_control`, donc les synthèses et questions suivantes sur le même document relisent le cache (moins cher, plus rapide).
- **Au-dessus** : **map-reduce hiérarchique**. Le texte est découpé en parties d'environ 35 000 tokens (sur les paragraphes, avec un léger chevauchement) ; chaque partie est condensée en notes fidèles (3 en parallèle, effort `low`), avec progression affichée. Si les notes dépassent encore le seuil, elles sont recondensées (jusqu'à 4 niveaux). Les notes sont **calculées une seule fois et stockées** : toutes les synthèses suivantes les réutilisent. Pour les questions, on ajoute aux notes les parties du texte intégral les plus pertinentes pour la question (score par mots-clés).

Même si les modèles acceptent 1 M de tokens, envoyer intégralement un document énorme à chaque question coûte cher et ralentit : le seuil par défaut est un compromis, ajustable.

## Configuration

Clé `ANTHROPIC_API_KEY` dans le menu **« Variables »** (sans clé : import, historique et lecture restent possibles ; un bandeau explique comment l'ajouter). Options : modèle par défaut (`claude-opus-5-5`, Sonnet 5.5, Haiku 5.5), `max_tokens`, effort, seuil de découpage. Repli serveur (`fallbacks: "default"`) activé pour Opus et Sonnet.

Données : SQLite (`module.db`) dans le dossier de données du module. UI entièrement via le bridge (`window.ledroide.call`).
