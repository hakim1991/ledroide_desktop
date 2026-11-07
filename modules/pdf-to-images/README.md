# 🖼️ PDF → Images

Module LedroideDesktop qui convertit chaque page d'un PDF en image PNG (150 DPI) et les regroupe dans un fichier ZIP.

## Prérequis

- Dépendances Python : `pdf2image`, `Pillow`
- **Poppler** (outils `pdftoppm` / `pdfinfo`), utilisé par `pdf2image` pour lire les PDF.

## Installer Poppler (Windows)

Le module cherche Poppler **dans le PATH de Windows**. Il ne suffit pas de copier un dossier quelque part.

### 1. Télécharger la bonne version

Sur <https://github.com/oschwartz10612/poppler-windows/releases>, dans la section **Assets**, téléchargez
**`Release-xx.xx.x-0.zip`**.

> ⚠️ Ne prenez **pas** « Source code (zip) » : c'est le code source, il ne contient aucun `.exe`.

### 2. Extraire

Extrayez le zip, par exemple dans `C:\poppler-26.09.0`. Vous devez obtenir :

```
C:\poppler-26.09.0\
└── Library\
    └── bin\
        ├── pdftoppm.exe
        ├── pdfinfo.exe
        └── ...
```

### 3. Ajouter le dossier `bin` au PATH

Le chemin à ajouter est celui du dossier **`Library\bin`**, par exemple `C:\poppler-26.09.0\Library\bin`.

**Méthode graphique**

1. Menu Démarrer → « Modifier les variables d'environnement pour votre compte »
2. Sélectionnez `Path` → **Modifier** → **Nouveau**
3. Collez `C:\poppler-26.09.0\Library\bin` → **OK**

**Ou en PowerShell** (adaptez le chemin) :

```powershell
$p = 'C:\poppler-26.09.0\Library\bin'
$c = [Environment]::GetEnvironmentVariable('Path','User')
if (($c -split ';') -notcontains $p) {
  [Environment]::SetEnvironmentVariable('Path', ($c.TrimEnd(';') + ';' + $p), 'User')
}
```

### 4. Vérifier

Ouvrez un **nouveau** terminal et lancez :

```
pdftoppm -v
```

La commande doit afficher un numéro de version.

### 5. Redémarrer LedroideDesktop

Fermez **complètement** LedroideDesktop puis relancez-le, sinon il ne verra pas le nouveau PATH.

## Dépannage

| Message | Cause | Solution |
|---|---|---|
| `Poppler non installé` | `pdftoppm` introuvable dans le PATH | Vérifier les étapes 2 à 5 |
| Toujours l'erreur après installation | LedroideDesktop pas redémarré, ou PATH pointant vers le mauvais dossier | Le PATH doit viser `...\Library\bin`, puis redémarrer l'application |
| Pas de `pdftoppm.exe` dans le dossier | Code source téléchargé au lieu de la release | Télécharger `Release-xx.xx.x-0.zip` |
| `pdf2image non installé` | Dépendance Python manquante | `pip install pdf2image Pillow` |
