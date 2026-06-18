# LedroideDesktop

Application desktop modulaire construite avec **Electron** (shell + UI React) et **Python / FastAPI** (backend).  
Chaque module embarque sa propre logique Python et sa propre interface graphique. Un écran de démarrage permet d'activer, désactiver et installer des modules sans redémarrer l'application.

---

## Prérequis

| Outil | Version minimale |
|-------|-----------------|
| Node.js | 20+ |
| Python | 3.11+ |
| npm | 9+ |

---

## Installation

### 1 — Dépendances Node.js

```bash
npm install
```

### 2 — Environnement Python

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install -e sdk/        # installe ledroide-sdk en mode editable
```

---

## Lancer en développement

Ouvrir **deux terminaux** :

```bash
# Terminal 1 — Backend Python
cd backend
.venv\Scripts\activate
python main.py

# Terminal 2 — Electron + Vite (depuis la racine du projet)
npm run dev
```

Ou tout-en-un (nécessite que Python soit dans le PATH et le venv activé) :

```bash
concurrently "python backend/main.py" "npm run dev"
```

---

## Structure du projet

```
ledroide-desktop/
├── electron/
│   ├── main.js              # Point d'entrée Electron
│   ├── preload.js           # Bridge IPC sécurisé (contextBridge)
│   └── renderer/            # Application React 18
│       ├── index.html
│       └── src/
│           ├── App.jsx
│           ├── main.jsx
│           ├── styles/global.css
│           └── components/
│               ├── ModuleManager/   # Écran de démarrage
│               └── ModuleFrame/     # Conteneur iframe module actif
│
├── backend/
│   ├── main.py              # Serveur FastAPI + Uvicorn
│   ├── requirements.txt
│   ├── core/
│   │   ├── registry.py      # Chargement/déchargement des modules
│   │   ├── ipc.py           # Gestionnaire WebSocket
│   │   └── db.py            # Abstraction base de données (Phase 3)
│   └── sdk/                 # Paquet ledroide-sdk (installé en editable)
│       └── ledroide_sdk/
│
├── modules/                 # Modules installés
│   └── demo/                # Module de démonstration inclus
│
├── modules-available/       # Catalogue local de modules disponibles
│   └── catalog.json
│
├── SPECIFICATION.md         # Spécification complète de l'architecture
└── CLAUDE.md                # Guide Claude Code
```

---

## Créer un module

1. Créer un dossier `modules/<id>/`
2. Ajouter `manifest.json` :

```json
{
  "id": "mon-module",
  "name": "Mon Module",
  "version": "1.0.0",
  "description": "Description courte",
  "author": "Auteur",
  "entry": {
    "backend": "backend/main.py",
    "frontend": "frontend/index.html"
  },
  "permissions": [],
  "database": { "type": "none" },
  "dependencies": { "python": [], "modules": [] },
  "config_schema": {}
}
```

3. Écrire `backend/main.py` :

```python
from ledroide_sdk import Module, router

module = Module()

@router.get("/hello")
async def hello():
    return {"message": "Bonjour depuis mon module !"}

@module.on("ping")
async def on_ping(payload):
    await module.dispatch("pong", {"echo": payload})
```

4. Écrire `frontend/index.html` (HTML/CSS/JS libre)

5. Redémarrer le backend → le module apparaît dans l'onglet **Installés**

---

## Variables d'environnement

| Variable | Défaut | Description |
|----------|--------|-------------|
| `LEDROIDE_BACKEND_PORT` | `8765` | Port du serveur FastAPI |

---

## Phases de développement

| Phase | État | Périmètre |
|-------|------|-----------|
| 1 — Socle | ✅ En cours | Electron + FastAPI + IPC WebSocket + Module Manager |
| 2 — Registry | ⏳ À venir | Chargement dynamique des routes par module |
| 3 — SDK complet | ⏳ À venir | Abstraction DB, events, config |
| 4 — Module démo | ⏳ À venir | Module de démonstration complet |
| 5 — Catalogue | ⏳ À venir | Installation depuis catalogue, dépendances Python |

---

## Spécification complète

Voir [SPECIFICATION.md](./SPECIFICATION.md) pour l'architecture détaillée, le format du manifest, les endpoints API et les choix techniques.
