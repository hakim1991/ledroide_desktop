# CLAUDE.md — LedroideDesktop

Référence de contexte pour Claude Code. Lire avant toute implémentation.

## Ce que fait ce projet

Application desktop modulaire : shell **Electron** + backend **Python (FastAPI)**. Chaque module embarque sa propre logique Python (`backend/main.py`) et sa propre UI (`frontend/index.html`). Un écran de démarrage (Module Manager) permet d'activer, désactiver, installer des modules.

Spécification complète : `SPECIFICATION.md`.

---

## Règles d'architecture à respecter

### Communication Electron ↔ Python
- Toujours passer par **WebSocket** (`/ws`) pour les événements temps réel et par **REST HTTP** pour les appels ponctuels.
- Le `preload.js` Electron est le seul point d'exposition vers le renderer — utiliser `contextBridge` systématiquement, jamais `nodeIntegration: true`.
- Port du backend Python : variable d'environnement `LEDROIDE_BACKEND_PORT`, défaut `8765`.

### Modules
- Un module = un dossier dans `modules/<id>/` avec un `manifest.json` obligatoire.
- Les routes FastAPI d'un module sont montées dynamiquement sous `/modules/{id}/api/` à l'activation, démontées à la désactivation.
- Un module ne doit jamais importer directement `fastapi` ou `asyncpg` — tout passe par `ledroide_sdk`.
- Le SDK est dans `backend/sdk/` et installé en editable (`pip install -e backend/sdk`).

### Base de données
- Le choix CouchDB vs PostgreSQL est déclaré par module dans `manifest.json` (`database.type`).
- L'abstraction `db` du SDK route automatiquement — ne jamais appeler CouchDB ou PostgreSQL directement dans un module.
- CouchDB : pour les modules orientés documents, sync, temps réel.
- PostgreSQL : pour les modules avec relations ou agrégations complexes.

### UI des modules
- Chaque module UI est chargé dans un `<iframe>` sandboxé dans le renderer Electron.
- L'UI d'un module communique avec son backend **uniquement** via `window.ledroide.call()` et `window.ledroide.on()` (jamais de fetch direct vers le backend).

---

## Structure des dossiers

```
ledroide-desktop/
├── electron/
│   ├── main.js              # Point d'entrée Electron
│   ├── preload.js           # contextBridge IPC
│   └── renderer/            # React 18
│       ├── ModuleManager/   # Écran démarrage
│       └── ModuleFrame/     # Conteneur iframe module
├── backend/
│   ├── main.py              # FastAPI + Uvicorn
│   ├── core/
│   │   ├── registry.py      # load/unload modules
│   │   ├── ipc.py           # WebSocket handler
│   │   └── db.py            # init CouchDB / PostgreSQL
│   └── sdk/                 # ledroide-sdk (paquet editable)
├── modules/                 # Modules installés
│   └── <id>/
│       ├── manifest.json
│       ├── backend/main.py
│       └── frontend/index.html
└── modules-available/       # Catalogue local
```

---

## Conventions de code

- Python : **async/await** partout, typage Pydantic pour les payloads WebSocket et REST.
- JavaScript : **ESM** dans Electron main, React avec hooks dans le renderer.
- Pas de commentaires sauf pour les invariants non-évidents.
- Les erreurs remontent toujours sous la forme `{"error": "message", "code": "ERROR_CODE"}`.

---

## Phases de développement

1. **Socle** : Electron + FastAPI + WS IPC + Module Manager statique
2. **Registry** : chargement dynamique + montage routes
3. **SDK** : `ledroide-sdk`, abstraction DB, events
4. **Premier module** : démo complète
5. **Catalogue** : install depuis catalogue, dépendances Python

Phase actuelle : **non démarrée** (spécification posée, aucun code écrit).
