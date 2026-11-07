# Guide d'intégration — Système de modules LedroideDesktop

Ce document est destiné à un agent Claude Code chargé d'intégrer le système de modules dans une application Electron + Python existante.

---

## Ce que tu vas intégrer

Un système de modules dynamiques où :
- chaque module est un dossier `modules/<id>/` avec un `manifest.json`, un backend Python (`backend/main.py`) et une UI (`frontend/index.html`)
- le backend FastAPI charge/décharge les modules dynamiquement (routes montées à l'activation)
- l'UI de chaque module tourne dans un `<iframe>` isolé et communique avec son backend via `window.ledroide.call()` et `window.ledroide.on()`
- un écran "Module Manager" liste les modules et permet de les activer/désactiver

---

## Étape 1 — Fichiers à copier tels quels

Depuis le projet source (`ledroide-desktop/`), copie ces fichiers dans le projet cible :

### Backend

```
backend/core/registry.py        → backend/core/registry.py
backend/sdk/                    → backend/sdk/          (dossier entier)
```

`registry.py` gère : scan du dossier `modules/`, activation/désactivation, montage dynamique des routes FastAPI, persistance de l'état (`state.json`).

`backend/sdk/` contient le paquet `ledroide-sdk` (classe `Module`, `router`, `events`, `config`, `db`). Installe-le avec `pip install -e backend/sdk`.

### Frontend (React)

Si le projet cible utilise React, copie :

```
electron/renderer/src/components/ModuleManager/   → (même chemin)
electron/renderer/src/components/ModuleFrame/     → (même chemin)
```

Sinon, adapte la logique (voir Étape 4).

---

## Étape 2 — Modifications du backend Python (`backend/main.py`)

### 2a. Ajouter le fix de sys.path en tête de fichier

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
```

Obligatoire pour que `from core.registry import ModuleRegistry` fonctionne avec Python embeddable en production.

### 2b. Importer et instancier le registry

```python
from core.registry import ModuleRegistry

registry = ModuleRegistry(app)

@app.on_event("startup")
async def startup():
    await registry.load_all()
```

### 2c. Ajouter les routes système

```python
@app.get("/modules")
async def list_modules():
    return registry.list_modules()

@app.post("/modules/{module_id}/activate")
async def activate_module(module_id: str):
    return await registry.activate(module_id)

@app.post("/modules/{module_id}/deactivate")
async def deactivate_module(module_id: str):
    return await registry.deactivate(module_id)

@app.delete("/modules/{module_id}")
async def uninstall_module(module_id: str):
    return await registry.uninstall(module_id)

@app.post("/modules/{module_id}/deps")
async def install_deps(module_id: str):
    return await registry.install_deps(module_id)

@app.get("/modules/{module_id}/icon")
async def module_icon(module_id: str):
    from fastapi.responses import FileResponse, Response
    from pathlib import Path
    icon_path = Path(__file__).parent.parent / "modules" / module_id / "icon.png"
    if not icon_path.exists():
        return Response(status_code=404)
    return FileResponse(str(icon_path), media_type="image/png")
```

### 2d. Ajouter le WebSocket IPC

```python
from fastapi import WebSocket, WebSocketDisconnect

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    registry.add_connection(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await registry.handle_message(websocket, data)
    except WebSocketDisconnect:
        registry.remove_connection(websocket)
```

### 2e. Ajouter la route du bridge JS

Ce script est injecté dans chaque iframe de module. Il installe `window.ledroide` côté UI.

```python
_BRIDGE_JS = r"""
(function () {
  var pending   = new Map();
  var listeners = new Map();

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (d.type === 'ledroide:response' && pending.has(d.requestId)) {
      var p = pending.get(d.requestId);
      clearTimeout(p.timer);
      pending.delete(d.requestId);
      if (d.payload && d.payload.error) p.reject(new Error(d.payload.error));
      else p.resolve(d.payload);
    }
    if (d.type === 'ledroide:event') {
      var key = d.moduleId + ':' + d.action;
      var cbs = listeners.get(key);
      if (cbs) cbs.forEach(function (cb) { cb(d.payload); });
    }
  });

  window.ledroide = {
    backendPort: parseInt(window.location.port) || 8765,
    call: function (moduleId, action, payload) {
      payload = payload || {};
      return new Promise(function (resolve, reject) {
        var rid   = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
        var timer = setTimeout(function () {
          pending.delete(rid);
          reject(new Error('RPC timeout'));
        }, 10000);
        pending.set(rid, { resolve: resolve, reject: reject, timer: timer });
        window.parent.postMessage(
          { type: 'ledroide:call', moduleId: moduleId, action: action, payload: payload, requestId: rid },
          '*'
        );
      });
    },
    on: function (moduleId, event, callback) {
      var key = moduleId + ':' + event;
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(callback);
      window.parent.postMessage({ type: 'ledroide:subscribe', moduleId: moduleId, event: event }, '*');
    },
    off: function (moduleId, event, callback) {
      var key = moduleId + ':' + event;
      var cbs = listeners.get(key);
      if (cbs) cbs.delete(callback);
    },
  };
})();
"""

from fastapi.responses import Response

@app.get("/ledroide-bridge.js")
async def ledroide_bridge():
    return Response(content=_BRIDGE_JS, media_type="application/javascript")
```

---

## Étape 3 — Modifications du `electron/main.js`

### 3a. Attendre que le backend soit prêt avant d'ouvrir la fenêtre

```javascript
const http = require('http')
const BACKEND_PORT = process.env.LEDROIDE_BACKEND_PORT || 8765

function waitForBackend(retries = 20, delay = 300) {
  return new Promise((resolve) => {
    const attempt = () => {
      const req = http.get(`http://127.0.0.1:${BACKEND_PORT}/modules`, (res) => {
        if (res.statusCode === 200) return resolve()
        retry()
      })
      req.on('error', retry)
      function retry() {
        if (--retries <= 0) return resolve()
        setTimeout(attempt, delay)
      }
    }
    attempt()
  })
}

// Dans app.whenReady() :
app.whenReady().then(async () => {
  startPythonBackend()        // ton spawn Python existant
  await waitForBackend()      // attendre FastAPI
  createWindow()
})
```

### 3b. Ajouter le handler IPC restart (optionnel mais utile)

```javascript
const { ipcMain } = require('electron')

ipcMain.handle('restart-backend', async () => {
  if (pythonProcess) {
    pythonProcess.kill()
    await new Promise((resolve) => {
      pythonProcess.once('exit', resolve)
      setTimeout(resolve, 4000)
    })
    pythonProcess = null
  }
  startPythonBackend()
  await waitForBackend(40, 300)
  return { ok: true }
})
```

---

## Étape 4 — Modifications du `electron/preload.js`

Ajoute ces handlers dans le `contextBridge`. Ils font le pont entre les iframes modules (postMessage) et le WebSocket Python.

```javascript
const { contextBridge, ipcRenderer } = require('electron')

// WebSocket partagé
let ws = null
const wsListeners = new Map()

function getWs() {
  if (ws && ws.readyState === WebSocket.OPEN) return ws
  const port = process.env.LEDROIDE_BACKEND_PORT || 8765
  ws = new WebSocket(`ws://127.0.0.1:${port}/ws`)
  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data)
    if (data.type === 'response') {
      const key = data.request_id
      const cb = wsListeners.get(key)
      if (cb) { wsListeners.delete(key); cb(data) }
    }
    if (data.type === 'event') {
      // Rediffuse l'événement vers toutes les iframes
      // (géré côté renderer, voir ModuleFrame)
    }
  }
  return ws
}

contextBridge.exposeInMainWorld('electronAPI', {
  // ... tes APIs existantes ...

  // Appel RPC vers un module Python
  moduleCall: (moduleId, action, payload, requestId) => {
    const socket = getWs()
    socket.send(JSON.stringify({ type: 'call', module_id: moduleId, action, payload, request_id: requestId }))
  },

  onModuleResponse: (requestId, callback) => {
    wsListeners.set(requestId, callback)
  },

  restartBackend: () => ipcRenderer.invoke('restart-backend'),
})
```

> **Note** : si ton `preload.js` existant utilise déjà un WebSocket, branche les messages `module_id` dessus plutôt que d'en créer un second.

---

## Étape 5 — Intégrer le Module Manager dans l'UI

### Option A — Remplacer l'écran de démarrage (recommandé)

Intègre le composant `ModuleManager` comme vue principale ou comme écran d'accueil de l'app.

### Option B — Ajouter un onglet / une page dans l'UI existante

```jsx
import ModuleManager from './components/ModuleManager'

// Dans ton router ou layout :
<Route path="/modules" element={<ModuleManager />} />
```

### Option C — UI vanilla (sans React)

Si le projet n'utilise pas React, reproduis la logique suivante en JS pur :

```javascript
// Lister les modules
const modules = await fetch('http://127.0.0.1:8765/modules').then(r => r.json())

// Activer un module
await fetch(`http://127.0.0.1:8765/modules/${id}/activate`, { method: 'POST' })

// Afficher l'UI d'un module dans une iframe
const iframe = document.createElement('iframe')
iframe.src = `http://127.0.0.1:8765/modules/${id}/frontend/index.html`
iframe.sandbox = 'allow-scripts allow-same-origin'
document.body.appendChild(iframe)

// Injecter le bridge JS dans l'iframe une fois chargée
iframe.onload = () => {
  const script = iframe.contentDocument.createElement('script')
  script.src = 'http://127.0.0.1:8765/ledroide-bridge.js'
  iframe.contentDocument.head.appendChild(script)
}
```

---

## Étape 6 — Créer le premier module

Structure minimale :

```
modules/
└── mon-module/
    ├── manifest.json
    ├── backend/
    │   └── main.py
    └── frontend/
        └── index.html
```

**`manifest.json`**
```json
{
  "id": "mon-module",
  "name": "Mon Module",
  "version": "1.0.0",
  "description": "Description",
  "entry": {
    "backend": "backend/main.py",
    "frontend": "frontend/index.html"
  },
  "dependencies": { "python": [] },
  "database": { "type": "none" }
}
```

**`backend/main.py`**
```python
from ledroide_sdk import Module

module = Module(__file__)
router = module.router

@router.get("/ping")
async def ping():
    return {"ok": True}

@module.on("hello")
async def on_hello(payload):
    return {"message": f"Bonjour {payload.get('name', '...')}"}
```

**`frontend/index.html`**
```html
<!DOCTYPE html>
<html>
<head>
  <script src="http://127.0.0.1:8765/ledroide-bridge.js"></script>
</head>
<body>
  <button onclick="test()">Test</button>
  <script>
    async function test() {
      const r = await window.ledroide.call('mon-module', 'hello', { name: 'Monde' })
      alert(r.message)
    }
  </script>
</body>
</html>
```

---

## Points d'attention

- Le dossier `modules/` doit être à la racine du projet (même niveau que `backend/` et `electron/`).
- `registry.py` calcule son chemin avec `Path(__file__).parent.parent.parent / "modules"` — adapter si la structure est différente.
- Le CORS doit être ouvert sur le backend FastAPI (`allow_origins=["*"]`) pour que les iframes puissent appeler l'API.
- En production avec Python embeddable, ajouter `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` en tête de `backend/main.py`.
- Les dépendances Python d'un module sont installées via `POST /modules/{id}/deps` qui appelle `sys.executable -m pip install`.
