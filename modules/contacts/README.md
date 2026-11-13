# Contacts

Carnet d'adresses et mini CRM local (base SQLite du module).

- **Fiches** : prénom, nom, société, fonction, e-mails et téléphones multiples (avec type), adresse, anniversaire (avec ou sans année), site web, notes, tags, photo (recadrée 192 px), favori.
- **Historique des échanges** : appel, e-mail, réunion, message ou note, avec date/heure et compte rendu.
- **Relances datées** : onglet « Relances » (en retard, aujourd'hui, 7 prochains jours, plus tard), report +1 j / +7 j ; une relance faite est ajoutée à l'historique. Compteur des relances en retard dans l'onglet.
- **Recherche** sur nom, société, e-mails, téléphones (chiffres seuls acceptés), ville, notes, tags ; filtres Favoris / Avec relance / tag (clic droit sur un tag pour le renommer). Anniversaires des 30 prochains jours.
- **Import** vCard 2.1 / 3.0 / 4.0 (pliage, quoted-printable, photos intégrées) et CSV (format du module, Google Contacts, Outlook). **Export** vCard 3.0 ou 4.0 (tout ou une fiche) et CSV (séparateur `;`, UTF-8 BOM pour Excel).
- **Doublons** : détection par e-mail, téléphone (9 derniers chiffres) ou nom identique ; fusion en choisissant la fiche conservée (coordonnées, tags, notes, historique et relances sont regroupés).
