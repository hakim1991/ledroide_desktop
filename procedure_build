electron-builder installé. Tout est en place. Voici la procédure complète :

---
Procédure pour créer le build prod

Étape 1 — Préparer l'environnement Python (une seule fois)

npm run setup-python
Ce script :
- Télécharge Python 3.12.9 embeddable (~10 Mo) dans backend/python-dist/
- Active les site-packages
- Installe pip
- Installe FastAPI, Uvicorn, Pillow, numpy et le SDK Ledroide
- Les dépendances lourdes (moviepy, yt-dlp, sympy…) s'installent à la demande quand l'utilisateur active un module

Étape 2 — Builder l'app

npm run dist
Ça fait vite build + electron-builder → produit dist/release/LedroideDesktop Setup 0.1.0.exe

---
Ce que l'installeur embarque

resources/
  app/           ← code Electron + frontend buildé + backend Python + modules
  python/        ← Python 3.12.9 embeddable autonome (~60 Mo)

L'utilisateur final n'a pas besoin de Python. L'app s'installe dans %AppData%\Local\Programs\LedroideDesktop (pas de droits admin).

---
Pour ajouter un raccourci ou une icône

Crée assets/icon.ico (256×256 minimum), puis ajoute dans package.json :
"win": { "icon": "assets/icon.ico" }