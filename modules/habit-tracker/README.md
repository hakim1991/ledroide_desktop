# Habitudes

Suivi d'habitudes au quotidien.

- Habitudes **oui/non** ou **quantité** (ex. 8 verres d'eau : boutons −/＋ ou saisie directe).
- Fréquence : **chaque jour**, **x fois par semaine** (semaine du lundi au dimanche) ou **jours précis** (ex. lun., mer., ven.).
- **Coche rapide** du jour (touches `1`–`9`), navigation dans les jours passés (`←` / `→`) pour rattraper un oubli, bandeau des 7 derniers jours cliquable.
- **Séries** actuelle et record (en jours, ou en semaines réussies pour « x fois par semaine »). Un jour prévu non encore coché aujourd'hui ne casse pas la série ; les jours non prévus sont ignorés.
- **Taux de réussite** sur 30 jours (4 semaines) et depuis le début ; total réalisé ; jours réussis par mois.
- **Calendrier annuel** (heatmap) par habitude (cliquer un jour pour le cocher / le corriger) et global (part des habitudes prévues réalisées, notes signalées).
- **Objectif** de série (ex. 30 jours d'affilée) avec barre de progression.
- **Note du jour** (enregistrement automatique), liste des notes récentes.
- **Archivage** (historique conservé, restauration possible).
- **Rappels** à l'heure choisie : bandeau dans l'application + son, et notification système si elle est autorisée (menu ⋯).
- **Export / import JSON** (l'import ajoute les habitudes ; les notes existantes ne sont pas écrasées).

**Limite des rappels** : ils sont déclenchés par l'interface du module ; ils ne fonctionnent que si le module Habitudes est ouvert dans LedroideDesktop (onglet affiché ou non). Un rappel manqué est affiché une fois à la prochaine ouverture du module le même jour, si l'habitude n'est toujours pas faite.

Données : SQLite (`module.db`) dans le dossier de données du module. Le « jour » suit l'horloge locale de l'interface.
