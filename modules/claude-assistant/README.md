# Assistant Claude

Assistant IA basé sur l'API Claude d'Anthropic (SDK Python officiel `anthropic`).

- **Discussions** : conversations multiples (liste latérale, recherche, renommer, supprimer), enregistrées dans la base SQLite du module.
- Réponses en **streaming**, bouton « Arrêter » (la partie déjà reçue est conservée), régénérer la dernière réponse, modifier le dernier message.
- Choix du modèle par conversation (Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1) et prompt système optionnel.
- Rendu Markdown maison et sûr (HTML échappé) : titres, listes, gras/italique, liens, tableaux, blocs de code avec bouton « Copier ».
- Compteur de tokens (entrée/sortie) par message et par conversation, d'après l'usage renvoyé par l'API.
- **Actions rapides** sur un texte collé : Résumer, Traduire, Corriger, Reformuler (formel/neutre/amical), Expliquer du code, Message de commit depuis un diff.

## Configuration

1. Créez une clé sur console.anthropic.com.
2. Menu **« Variables »** de l'application → ajoutez `ANTHROPIC_API_KEY`.

La clé est relue à chaque requête (modifiable à chaud), n'est jamais renvoyée à l'interface ni écrite dans les journaux.

Options du module : modèle par défaut (`claude-opus-5-5`), `max_tokens` (64 000 par défaut) et effort de réflexion (`low` → `max`, `medium` par défaut). La température n'est pas proposée : ces modèles la refusent (erreur 400). Pour Opus, Sonnet et Fable, le repli serveur en cas de refus (`fallbacks: "default"`) est activé.

Erreurs traduites en français : clé invalide (401), quota/limite de débit (429, avec délai `retry-after`), surcharge (529), réseau, contexte trop long, refus du modèle.

Raccourcis : `Entrée` envoyer, `Maj+Entrée` saut de ligne, `Échap` arrêter / annuler la modification, `Ctrl+N` nouvelle conversation, `Ctrl+Entrée` lancer une action rapide.
