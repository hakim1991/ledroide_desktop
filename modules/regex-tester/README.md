# 🔍 Testeur Regex

Testez vos expressions régulières en direct.

- Moteur **Python** (`re`, côté backend — par défaut) ou **JavaScript** (côté client)
- Drapeaux `i`, `m`, `s`, `x` (en JavaScript, `x` est émulé : espaces et commentaires `#` ignorés ; `(?P<nom>…)` est accepté)
- Surlignage des correspondances et des groupes (y compris nommés), tableau des correspondances
- Remplacement avec aperçu : `\1`, `\g<nom>`, `\g<0>` dans les deux moteurs
- Position précise des erreurs de syntaxe (moteur Python)
- Aide-mémoire cliquable, motifs enregistrés (SQLite `regex-tester.db`)

Aucune dépendance externe.

API : `POST /match`, `POST /replace`, `GET/POST /patterns`, `DELETE /patterns/{id}`, `GET /health`.

Limite : une expression à retour arrière catastrophique (ex. `(a+)+$`) peut bloquer le calcul ; le nombre de correspondances est plafonné à 5000.
