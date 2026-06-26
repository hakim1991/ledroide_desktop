# Spécification — LedroideDesktop

## Vue d'ensemble

Application desktop modulaire : **Electron** (shell + UI) + **Python FastAPI** (backend logique). Chaque module embarque sa propre logique Python (`backend/main.py`) et sa propre interface (`frontend/index.html`). L'écran de démarrage (Module Manager) permet d'activer, désactiver et désinstaller des modules installés localement.

---

## Architecture globale

```
┌──────────────────────────────────────────────────────────┐
│                      Electron Shell                       │
│                                                           │
│  ┌─────────────────┐     ┌──────────────────────────┐    │
│  │  Module Manager │     │  Module Frame             │    │
│  │  (React)        │     │  <iframe sandbox>         │    │
│  │  - liste modules│     │  UI du module actif       │    │
│  │  - activate /   │     │  window.ledroide.call()   │    │
│  │    deactivate   │     │  window.ledroide.on()     │    │
│  └────────┬────────┘     └────────────┬─────────────┘    │
│           │  REST HTTP                │  postMessage IPC  │
│           │                           │                   │
│  ┌────────▼───────────────────────────▼─────────────┐    │
│  │                  preload.js                       │    │
│  │  contextBridge → window.electronAPI               │    │
│  │  Proxy postMessage ↔ WebSocket                    │    │
│  └────────────────────────┬──────────────────────────┘    │
└───────────────────────────┼──────────────────────────────┘
                            │  WebSocket ws://127.0.0.1:PORT/ws
                            │  REST HTTP  http://127.0.0.1:PORT
┌───────────────────────────▼──────────────────────────────┐
│              Python Backend (FastAPI + Uvicorn)           │
│                                                           │
│  ┌──────────────────┐   ┌─────────────────────────────┐  │
│  │  Module Registry │   │  Module Runtime              │  │
│  │  - scan modules/ │   │  - importlib chargement      │  │
│  │  - mount routes  │   │  - instance Module (SDK)     │  │
│  │  - state.json    │   │  - router FastAPI monté      │  │
│  └──────────────────┘   └─────────────────────────────┘  │
│                                                           │
│  Routes statiques : /modules/{id}/frontend/**             │
│  Routes API       : /modules/{id}/api/**                  │
└──────────────────────────────────────────────────────────┘
```

---

## Structure des dossiers

```
ledroide-desktop/
├── electron/
│   ├── main.js              # Point d'entrée Electron, spawn Python
│   ├── preload.js           # contextBridge IPC (seul pont vers Node)
│   └── renderer/            # React 18
│       ├── src/
│       │   ├── components/
│       │   │   ├── ModuleManager/   # Écran de démarrage
│       │   │   └── ModuleFrame/     # Conteneur iframe module
│       │   └── main.jsx
│       └── dist/            # Build Vite (chargé par Electron)
│
├── backend/
│   ├── main.py              # FastAPI app + Uvicorn + bridge JS
│   ├── core/
│   │   ├── registry.py      # Scan, activation, routes dynamiques
│   │   └── ipc.py           # WebSocket handler
│   └── sdk/                 # ledroide-sdk (paquet local)
│       └── ledroide_sdk/
│           ├── module.py    # Classe Module
│           ├── events.py    # _Events
│           ├── config.py    # _Config
│           └── db.py        # _DB
│
├── modules/                 # Modules installés
│   └── <id>/
│       ├── manifest.json    # Obligatoire
│       ├── backend/
│       │   └── main.py
│       └── frontend/
│           └── index.html
│
├── modules-available/       # Catalogue local (catalog.json)
├── assets/
│   └── icon.ico
├── scripts/
│   └── setup-python.js      # Setup Python embeddable (prod)
├── package.json
├── vite.config.js
├── CLAUDE.md
└── SPECIFICATION.md
```

---

## Démarrage de l'application

```
Electron main.js
    │
    ├── resolvePython()         # Cherche python.exe (prod → dev → système)
    ├── spawn(python, backend/main.py)
    ├── waitForBackend()        # Poll GET /modules jusqu'à HTTP 200
    └── createWindow()          # Charge dist/renderer/index.html
```

### Résolution Python (ordre de priorité)

| Priorité | Chemin | Contexte |
|----------|--------|----------|
| 1 | `resources/python/python.exe` | Production (embeddable bundlé) |
| 2 | `backend/python-dist/python.exe` | Dev avec setup-python |
| 3 | `backend/.venv/Scripts/python.exe` | Dev avec venv |
| 4 | `python` / `python3` | Dernier recours système |

---

## Communication Electron ↔ Python

Deux canaux complémentaires :

| Canal | Usage |
|-------|-------|
| **REST HTTP** | Actions ponctuelles (activer, désactiver, lister) |
| **WebSocket `/ws`** | Événements temps réel, RPC module ↔ UI |

### Bridge IPC (modules UI)

Le backend injecte `ledroide-bridge.js` dans chaque iframe de module. Ce script installe `window.ledroide` et traduit les appels en messages `postMessage` vers le preload, qui les relaie via WebSocket vers Python.

```
Module UI (iframe)
  window.ledroide.call(moduleId, action, payload)
    └→ postMessage({ type: 'ledroide:call', ... })
         └→ preload.js → WebSocket → registry.py → module.dispatch()
              └→ WebSocket → postMessage → Promise.resolve(result)
```

```js
// Dans le frontend d'un module
const result = await window.ledroide.call('mon-module', 'get_data', {})
window.ledroide.on('mon-module', 'data_updated', (payload) => { ... })
```

### Format des messages WebSocket

```json
{
  "type": "call | response | event | subscribe",
  "module_id": "mon-module",
  "action": "get_data",
  "payload": {},
  "request_id": "abc123"
}
```

---

## Cycle de vie d'un module

### Activation

```
POST /modules/{id}/activate
    │
    ├── Monte StaticFiles  /modules/{id}/frontend/**
    ├── importlib charge   modules/{id}/backend/main.py
    ├── Attache mod_py.router à FastAPI sous /modules/{id}/api/**
    ├── Injecte broadcast (events), configs persistés (config)
    ├── Stocke l'instance dans _module_instances
    └── Persiste {id} dans state.json
```

### Désactivation

```
POST /modules/{id}/deactivate
    │
    ├── Retire les routes API   /modules/{id}/api/**
    ├── Retire le mount static  /modules/{id}/frontend/**
    ├── Supprime l'instance de  _module_instances
    ├── Purge sys.modules       module_{id}
    └── Retire {id} de         state.json
```

### Persistance entre sessions

- `backend/state.json` — liste des modules actifs au moment du dernier arrêt. Rechargés automatiquement au démarrage.
- `backend/configs.json` — configurations sauvegardées par module.

---

## API REST Backend

### Routes système

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/modules` | Liste tous les modules et leur état |
| `POST` | `/modules/{id}/activate` | Active un module |
| `POST` | `/modules/{id}/deactivate` | Désactive un module |
| `DELETE` | `/modules/{id}` | Désinstalle (supprime le dossier) |
| `GET` | `/modules/{id}/icon` | Icône PNG du module |
| `POST` | `/modules/{id}/deps` | Installe les dépendances Python |
| `GET` | `/modules/{id}/config` | Lit la configuration |
| `PUT` | `/modules/{id}/config` | Sauvegarde la configuration |
| `POST` | `/system/reload` | Hot-reload sans redémarrer |
| `GET` | `/ledroide-bridge.js` | Bridge JS injecté dans les iframes |
| `WS` | `/ws` | WebSocket bidirectionnel |

### Routes par module (dynamiques)

Montées à l'activation, démontées à la désactivation :

```
GET  /modules/{id}/frontend/**    → fichiers statiques (index.html, etc.)
*    /modules/{id}/api/**         → routes FastAPI déclarées dans le module
```

---

## SDK — `ledroide-sdk`

Paquet Python installé dans l'environnement Python de l'app. Fournit la classe `Module` et les objets `router`, `events`, `config`, `db`.

### Classe `Module`

```python
from ledroide_sdk import Module

module = Module(__file__)   # lit manifest.json en remontant l'arborescence
```

| Attribut / méthode | Description |
|--------------------|-------------|
| `module.id` | ID du module lu depuis manifest.json |
| `module.router` | `APIRouter` FastAPI (créé lazily) |
| `module.on(event)` | Décorateur pour écouter un événement UI |
| `module.dispatch(event, payload)` | Appelé par le registry sur message WS |
| `module._events` | Instance `_Events` (broadcast vers UI) |
| `module._config` | Instance `_Config` (persistance) |
| `module._db` | Instance `_DB` (abstraction base) |

### Pattern type d'un backend de module

```python
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

@router.get("/data")
async def get_data():
    return {"value": 42}

@module.on("refresh")
async def on_refresh(payload):
    await module._events.emit("data_updated", {"value": 42})
```

---

## Format d'un module — `manifest.json`

```json
{
  "id": "mon-module",
  "name": "Mon Module",
  "version": "1.0.0",
  "description": "Description courte.",
  "author": "Nom",
  "entry": {
    "backend": "backend/main.py",
    "frontend": "frontend/index.html"
  },
  "dependencies": {
    "python": ["httpx>=0.27"]
  },
  "database": {
    "type": "none"
  }
}
```

### Valeurs `database.type`

| Valeur | Base utilisée |
|--------|--------------|
| `"none"` | Aucune (SQLite local possible en autonomie) |
| `"couchdb"` | CouchDB (documents, sync offline) |
| `"postgresql"` | PostgreSQL (relationnel, agrégations) |

---

## Module Manager (UI)

Interface React chargée au démarrage. Trois onglets :

| Onglet | Contenu | Actions disponibles |
|--------|---------|---------------------|
| **Actifs** | Modules en cours d'exécution | Désactiver |
| **Installés** | Modules installés, inactifs | Activer, Désinstaller |
| **Disponibles** | Catalogue local (`modules-available/`) | Installer |

### Comportement des actions

- **Activer / Désactiver** : ouvre une modale de confirmation (React Portal, `position: fixed`). Une fois confirmé, l'action est bloquante jusqu'à la réponse du backend — impossible d'annuler.
- **Désinstaller** : appel direct sans modale, supprime le dossier du module.
- **Installer les dépendances** : déclenche `POST /modules/{id}/deps` qui exécute `pip install` via `sys.executable`.

---

## Build de production

### Python embeddable

Pour la distribution, Python n'est pas requis sur la machine de l'utilisateur. Un Python 3.12 embeddable est préparé via :

```
node scripts/setup-python.js [--force]
```

Ce script :
1. Télécharge `python-3.12.x-embed-amd64.zip`
2. Active `site-packages` (décommente `import site` dans `python312._pth`)
3. Installe `pip` via `get-pip.py`
4. Installe les dépendances de base (fastapi, uvicorn, Pillow, numpy…)
5. Copie `ledroide_sdk` directement dans `site-packages/`

Le résultat est dans `backend/python-dist/` (ignoré par git).

### Packaging Electron

```
npm run dist     # → vite build + electron-builder NSIS
```

| Élément | Destination dans le package |
|---------|----------------------------|
| `electron/**` | `resources/app/electron/` |
| `dist/renderer/**` | `resources/app/dist/renderer/` |
| `backend/**` | `resources/app/backend/` |
| `modules/**` | `resources/app/modules/` |
| `backend/python-dist/` | `resources/python/` (via `extraResources`) |

Installation NSIS : per-user (`%AppData%\Local\Programs\LedroideDesktop`), sans droits admin.

---

## Choix techniques

| Composant | Choix | Raison |
|-----------|-------|--------|
| Shell | Electron 30 | Desktop natif multiplateforme, accès OS |
| UI shell | React 18 + Vite | Composants, HMR en dev |
| UI modules | HTML/JS vanilla dans iframe | Isolation totale, sans dépendance de build |
| Backend | FastAPI + Uvicorn | Async natif, montage dynamique de routes |
| IPC | WebSocket + REST | Temps réel bidirectionnel + requêtes ponctuelles |
| Sandboxing | `contextBridge` + iframe sandbox | Sécurité : zéro `nodeIntegration` |
| DB document | CouchDB | Sync offline-first, JSON natif |
| DB relationnelle | PostgreSQL | Standard, intégrité forte |
| Distribution Python | Embeddable 3.12 | Aucune installation requise sur poste utilisateur |
| Packaging | electron-builder NSIS | Installeur Windows per-user |

---

## État d'avancement

| Phase | Périmètre | Statut |
|-------|-----------|--------|
| 1 — Socle | Electron + FastAPI + WebSocket IPC + Module Manager | ✅ Terminé |
| 2 — Registry | Chargement dynamique, montage routes, state persisté | ✅ Terminé |
| 3 — SDK | `ledroide-sdk`, events, config, bridge JS iframe | ✅ Terminé |
| 4 — Modules | Modules fonctionnels (calcul, notes, outils…) | ✅ Terminé |
| 5 — Build prod | Python embeddable, installeur NSIS | ✅ Terminé |
| 6 — Catalogue | Installation depuis catalogue distant, dépendances | 🔲 À faire |
