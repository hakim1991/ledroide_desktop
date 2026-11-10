# Cron & Timestamps

Deux outils de manipulation du temps.

## Timestamps
- Horloge « maintenant » en direct (s, ms, ISO UTC, heure locale).
- Conversion timestamp Unix ⇄ date : unité détectée automatiquement (≤ 11 chiffres = secondes, 12-14 = ms, 15-17 = µs, 18+ = ns) ou forcée ; dates ISO 8601, `AAAA/MM/JJ`, `JJ/MM/AAAA hh:mm`, RFC 2822 et `now`. Les dates sans fuseau sont interprétées dans le fuseau source (avec alerte pour les heures inexistantes ou ambiguës lors des changements d'heure).
- Comparaison simultanée dans plusieurs fuseaux horaires (liste mémorisée).
- Durée entre deux dates : différence calendaire (ans, mois, jours…), durée exacte, totaux par unité et jours ouvrés.

## Cron
- Expression standard à 5 champs (`minute heure jour mois jour-semaine`), noms `JAN-DEC` / `SUN-SAT`, listes, plages, pas (`*/15`, `0-30/10`), alias `@daily`, `@hourly`…
- Explication en français, erreur précise avec le champ fautif surligné.
- N prochaines exécutions dans le fuseau choisi, modèles prêts à l'emploi.
- Sémantique cron standard (Vixie) : si le jour du mois **et** le jour de semaine sont restreints, l'un **ou** l'autre suffit ; si l'un commence par `*`, les deux doivent correspondre.
- Changement d'heure : une heure locale sautée n'est pas planifiée, une heure répétée ne s'exécute qu'une fois.

Non pris en charge : champ secondes (6 champs), syntaxe Quartz `L`, `W`, `#`, `@reboot`.

Le parseur cron est intégré (aucune dépendance). La base des fuseaux horaires vient du paquet `tzdata` (Windows n'en fournit pas) ; sans lui, seul UTC fonctionne.

Raccourcis : `Alt+1` / `Alt+2` pour changer d'onglet.
