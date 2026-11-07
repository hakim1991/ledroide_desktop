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
- Port du backend Python : `LEDROIDE_BACKEND_PORT`, défaut `8765` ; si occupé, Electron choisit un port libre et le transmet (preload via `ipcRenderer.sendSync('ledroide:config')`).
- Sécurité : Electron génère un jeton (`LEDROIDE_TOKEN`). Les routes de gestion (`/modules`, `/system/*`, config…) et `/ws?token=` l'exigent ; le renderer passe par `window.ledroide.api()` qui l'ajoute. `core/security.py` vérifie aussi `Host` (anti DNS-rebinding) et `Origin`. Les routes `/modules/{id}/api|frontend|icon` sont publiques pour l'origine du backend uniquement.
- Les appels WebSocket sont traités en parallèle (une tâche par message) : un handler lent ne bloque pas les autres.
- Données utilisateur : `LEDROIDE_DATA_DIR` (= `userData/data` sous Electron) contient `state.json`, `configs.json` et `modules/<id>/`. Ne jamais écrire dans le dossier d'installation.

### Modules
- Un module = un dossier dans `modules/<id>/` avec un `manifest.json` obligatoire.
- Les routes FastAPI d'un module sont montées dynamiquement sous `/modules/{id}/api/` à l'activation, démontées à la désactivation.
- Un module ne doit jamais importer directement `fastapi` ou `asyncpg` — tout passe par `ledroide_sdk` (`from ledroide_sdk.http import UploadFile, JSONResponse, error…`).
- Squelette : `module = Module(__file__)`, `router = module.router`, `@module.on("action")`, `@module.job("action")` pour les tâches longues (`async def fn(payload, job)` + `await job.progress(pct, msg)` → événements `job:progress|done|error`, côté UI `window.ledroide.run()`), `@module.on_shutdown` pour le nettoyage.
- Fichiers de données : `module.data_path("x.db")` ou `data_path(__file__, "x.db")` (migre l'ancien fichier situé à côté du code).
- Manifest : `category` (une seule, ex. « Réseau ») et `tags` (liste, minuscules) pour les filtres de l'accueil et du Catalogue ; l'utilisateur peut les surcharger (bouton « Tags », stocké dans `module_meta.json` du dossier de données). `env` déclare les variables attendues : `["CLE"]` ou `[{"key", "description", "required", "secret"}]` → l'API renvoie `env_missing`.
- Variables d'environnement : menu « Variables » (`GET/PUT /system/env`, fichier `env.json` en clair). Appliquées à `os.environ` à chaud ; côté module, `module.env("CLE")` ou `ledroide_sdk.env("CLE")`. Événement `__system__` / `env:changed` à chaque modification. Noms `LEDROIDE_*`, `PATH`, `PYTHONPATH`… refusés.
- Un module qui ne se charge pas passe au statut `error` avec `last_error` (visible dans le Catalogue).
- Le SDK est dans `backend/sdk/` (ajouté au `sys.path` par `backend/main.py`, ou `pip install -e backend/sdk`).
- Dépendances Python : bornes de version majeure dans `manifest.json` (`"Pillow>=11,<12"`).

### Base de données
- Déclarée par module dans `manifest.json` (`database.type`) : `none` ou `sqlite` (implémenté) ; `couchdb` / `postgresql` réservés, non implémentés.
- `module.db` (SQLite, hors boucle d'événements) : API documents `save/get/query/delete` et SQL brut `execute/fetch_all/fetch_one/executescript`. Fichier dans le dossier de données du module.

### UI des modules
- Chaque module UI est chargé dans un `<iframe>` sandboxé dans le renderer Electron.
- L'UI d'un module communique avec son backend via `window.ledroide.call/run/on/off` (bridge `/ledroide-bridge.js`, source `backend/core/bridge.js`). `ModuleFrame` impose `module.id` : un module ne peut appeler que son propre backend.
- Existant : la plupart des modules font encore des `fetch` directs vers `/modules/{id}/api` (autorisé car même origine, d'où `allow-same-origin` dans le sandbox). Les nouveaux modules doivent préférer le bridge.

---

## Structure des dossiers

```
ledroide-desktop/
├── electron/
│   ├── main.mjs             # Point d'entrée Electron (ESM)
│   ├── preload.js           # contextBridge IPC (CommonJS : preload sandboxé)
│   └── renderer/            # React 18
│       ├── ModuleManager/   # Écran démarrage
│       └── ModuleFrame/     # Conteneur iframe module
├── backend/
│   ├── main.py              # FastAPI + Uvicorn
│   ├── core/
│   │   ├── registry.py      # load/unload modules
│   │   ├── ipc.py           # Modèles Pydantic WS + Connection
│   │   ├── security.py      # jeton, Host, Origin
│   │   ├── errors.py        # {"error", "code"} partout
│   │   ├── settings.py      # chemins, port, jeton, dossier de données
│   │   └── bridge.js        # bridge servi aux iframes
│   ├── tests/               # pytest (npm run test:backend)
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
- Les erreurs remontent toujours sous la forme `{"error": "message", "code": "ERROR_CODE"}` (`LedroideError` côté core, `ledroide_sdk.http.error()` côté module ; `detail` est conservé pour la compatibilité).
- Logs : module `logging` (jamais `print`), sans payloads complets.
- Tests : `npm test` (pytest backend + smoke Playwright Electron `tests/e2e/smoke.mjs`).

---

## Phases de développement

1. **Socle** : Electron + FastAPI + WS IPC + Module Manager statique
2. **Registry** : chargement dynamique + montage routes
3. **SDK** : `ledroide-sdk`, abstraction DB, events
4. **Premier module** : démo complète
5. **Catalogue** : install depuis catalogue, dépendances Python

Phase actuelle : phases 1 à 4 réalisées, phase 5 partielle (installation depuis `modules-available/<id>/` ou une archive zip déclarée dans `catalog.json` ; pas de catalogue distant ni de mise à jour auto).
