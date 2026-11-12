# Planning Gantt

Planification de tâches de projet avec diagramme de Gantt.

- Plusieurs projets (couleur par défaut, replanification automatique activable).
- Tâches : dates de début/fin, durée, avancement, responsable, couleur, notes ; jalons (◆).
- Groupes : indentation (⇥ / ⇤ ou Alt+←/→) ; dates et avancement d'un groupe calculés depuis ses sous-tâches (moyenne pondérée par la durée) ; repli/dépli.
- Diagramme : zoom jour / semaine / mois, week-ends grisés, ligne « aujourd'hui », tâches en retard en rouge.
- Glisser une barre pour la déplacer, ses bords pour la redimensionner ; glisser un groupe déplace toutes ses sous-tâches.
- Dépendances fin → début : glisser le rond au bout d'une barre vers une autre tâche, ou depuis la fiche de la tâche. Clic sur une flèche : décalage (jours) ou suppression. Cycles refusés.
- Replanification auto : quand une tâche est repoussée, ses successeurs sont décalés (durée conservée).
- Export JSON (réimportable) et CSV (Excel, séparateur `;`) ; import JSON ; projet d'exemple.
- Raccourcis : Inser (nouvelle tâche), Entrée (éditer), Suppr (supprimer la tâche ou la dépendance sélectionnée), ↑/↓ (sélection), Alt+↑/↓ (réordonner).

Données : SQLite (`module.db`) dans le dossier de données du module. Toute l'UI passe par le bridge (`window.ledroide.call`).
