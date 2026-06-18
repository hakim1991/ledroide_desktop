# Spécification — LedroideDesktop

## Vue d'ensemble

Application desktop modulaire : **Electron** (shell + UI) + **Python** (backend logique), où chaque module embarque sa propre logique Python et son propre interface graphique. Un écran de démarrage liste les modules disponibles (actifs, installés, disponibles).

---

## Architecture globale

```
┌─────────────────────────────────────────────────┐
│                  Electron Shell                  │
│  ┌────────────┐  ┌────────────────────────────┐ │
│  │  Module    │  │     Module UI (React)       │ │
│  │  Manager   │  │  iframe sandboxé par module │ │
│  │  (écran    │  │                            │ │
│  │  démarrage)│  └────────────────────────────┘ │
│  └─────┬──────┘              │ IPC              │
└────────┼─────────────────────┼──────────────────┘
         │ HTTP/WebSocket       │ HTTP/WebSocket
┌────────▼─────────────────────▼──────────────────┐
│              Python Backend (FastAPI)            │
│  ┌─────────────────┐  ┌────────────────────────┐│
│  │  Module Registry │  │  Module Runtime        ││
│  │  (load/unload)  │  │  (1 sous-process/module)││
│  └─────────────────┘  └────────────────────────┘│
│  ┌─────────────────────────────────────────────┐ │
│  │               Base de données               │ │
│  │  CouchDB (docs/sync) │ PostgreSQL (relation) │ │
│  └─────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

---

## Structure des dossiers

```
ledroide-desktop/
├── electron/                  # Shell Electron
│   ├── main.js                # Point d'entrée Electron
│   ├── preload.js             # Bridge IPC sécurisé
│   └── renderer/              # UI shell (React)
│       ├── ModuleManager/     # Écran de démarrage
│       └── ModuleFrame/       # Conteneur module actif
│
├── backend/                   # Python FastAPI
│   ├── main.py                # Serveur principal
│   ├── core/
│   │   ├── registry.py        # Chargement/déchargement modules
│   │   ├── ipc.py             # WebSocket vers Electron
│   │   └── db.py              # Abstraction base de données
│   └── requirements.txt
│
├── modules/                   # Modules installés
│   └── <nom-module>/
│       ├── manifest.json      # Métadonnées du module
│       ├── backend/
│       │   ├── __init__.py
│       │   └── main.py        # Logique Python du module
│       └── frontend/
│           ├── index.html     # UI du module
│           └── bundle.js      # (optionnel, compilé)
│
├── modules-available/         # Catalogue (local ou distant)
├── SPECIFICATION.md
├── CLAUDE.md
└── package.json
```

---

## Format d'un module — `manifest.json`

```json
{
  "id": "weather-monitor",
  "name": "Météo Monitor",
  "version": "1.0.0",
  "description": "Affiche les données météo en temps réel",
  "author": "...",
  "entry": {
    "backend": "backend/main.py",
    "frontend": "frontend/index.html"
  },
  "permissions": ["network", "database"],
  "database": {
    "type": "couchdb",
    "collections": ["weather_data"]
  },
  "dependencies": {
    "python": ["httpx>=0.27", "pydantic>=2.0"],
    "modules": []
  },
  "config_schema": {
    "api_key": { "type": "string", "required": true },
    "refresh_interval": { "type": "integer", "default": 60 }
  }
}
```

### Valeurs `database.type`

| Valeur | Base utilisée |
|--------|--------------|
| `"couchdb"` | CouchDB (documents, sync offline) |
| `"postgresql"` | PostgreSQL (relationnel, agrégations) |
| `"none"` | Aucune base de données |

---

## API Python Backend (FastAPI)

### Endpoints système

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/modules` | Liste tous les modules (état, version) |
| `POST` | `/modules/{id}/activate` | Active un module |
| `POST` | `/modules/{id}/deactivate` | Désactive un module |
| `POST` | `/modules/{id}/install` | Installe depuis le catalogue |
| `DELETE` | `/modules/{id}` | Désinstalle un module |
| `GET` | `/modules/{id}/config` | Lit la config d'un module |
| `PUT` | `/modules/{id}/config` | Sauvegarde la config |
| `WS` | `/ws` | WebSocket bidirectionnel Electron ↔ Python |

### Endpoints par module (montés dynamiquement)

Chaque module déclare ses propres routes FastAPI, montées sous `/modules/{id}/api/...` par le registry au moment de l'activation.

```python
# backend d'un module : backend/main.py
from ledroide_sdk import Module, router

module = Module()

@router.get("/data")
async def get_data():
    return {"value": 42}
```

---

## Communication Electron ↔ Python

**Protocole :** WebSocket (`ws://localhost:PORT/ws`) + REST HTTP

**Format des messages WebSocket :**

```json
{
  "type": "event | call | response",
  "module_id": "weather-monitor",
  "action": "update_data",
  "payload": {},
  "request_id": "uuid"
}
```

Le `preload.js` Electron expose une API sécurisée au renderer via `contextBridge` :

```js
// Dans le renderer d'un module
window.ledroide.call(moduleId, action, payload)   // RPC vers Python
window.ledroide.on(moduleId, event, callback)      // écoute événements
window.ledroide.off(moduleId, event, callback)     // désinscription
```

---

## Base de données

### Règle de choix

| Cas d'usage | Base recommandée |
|-------------|-----------------|
| Documents libres, sync offline, temps réel | **CouchDB** |
| Relations, agrégations, intégrité forte | **PostgreSQL** |
| Les deux dans la même appli | Les deux, sélectionnés par module dans le manifest |

### Abstraction dans le SDK module

```python
from ledroide_sdk import db

# Transparent : route vers CouchDB ou PostgreSQL selon le manifest
await db.save("weather_data", {"temp": 22.5, "city": "Paris"})
results = await db.query("weather_data", {"city": "Paris"})
await db.delete("weather_data", doc_id)
```

---

## SDK Python — `ledroide-sdk`

Paquet local (installé en editable dans le venv principal), exposé à chaque module.

```python
from ledroide_sdk import Module, router, db, events, config

module = Module()  # lit le manifest.json du dossier courant

# Config typée, validée par le schema manifest
api_key = config.get("api_key")

# Émettre un événement vers l'UI Electron
await events.emit("data_updated", {"temp": 22.5})

# Écouter un événement venant de l'UI
@module.on("refresh")
async def on_refresh(payload):
    ...

# Routes FastAPI du module
@router.get("/data")
async def get_data():
    return await db.query("weather_data", {})
```

---

## Écran de démarrage — Module Manager

Trois onglets :

| Onglet | Contenu | Actions |
|--------|---------|---------|
| **Actifs** | Modules en cours d'exécution | Désactiver, Configurer |
| **Installés** | Modules installés, inactifs | Activer, Désinstaller |
| **Disponibles** | Catalogue local/distant | Installer |

Chaque carte module affiche : nom, description, version, permissions requises, type de base de données.

---

## Choix techniques

| Composant | Choix | Raison |
|-----------|-------|--------|
| Shell | Electron 30+ | Natif multiplateforme, accès OS |
| UI Shell | React 18 | Composants, écosystème |
| Backend | FastAPI + Uvicorn | Async, montage dynamique de routes |
| DB document | CouchDB | Sync offline-first, JSON natif |
| DB relationnelle | PostgreSQL | Standard, robuste |
| IPC | WebSocket + REST | Temps réel + requêtes ponctuelles |
| Packaging module | Dossier zip | Simple, extensible |

---

## Phases de développement

| Phase | Périmètre |
|-------|-----------|
| **1 — Socle** | Electron shell + FastAPI + WebSocket IPC + Module Manager UI (liste statique) |
| **2 — Registry** | Chargement/déchargement dynamique, montage routes par module |
| **3 — SDK** | Paquet `ledroide-sdk`, abstraction DB, système d'événements |
| **4 — Premier module** | Module de démonstration complet (backend + UI) |
| **5 — Catalogue** | Installation depuis catalogue, gestion dépendances Python |
