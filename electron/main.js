const { app, BrowserWindow, ipcMain, dialog } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const fs = require('fs')

const BACKEND_PORT = process.env.LEDROIDE_BACKEND_PORT || 8765
const RENDERER_DIST = path.join(__dirname, '../dist/renderer/index.html')

let mainWindow
let pythonProcess

function resolvePython() {
  const root = path.join(__dirname, '..')
  // Prefer venv Python so deps are guaranteed available
  const candidates = [
    path.join(root, 'backend', '.venv', 'Scripts', 'python.exe'), // Windows venv
    path.join(root, 'backend', '.venv', 'bin', 'python'),          // Unix venv
    path.join(root, '.venv', 'Scripts', 'python.exe'),             // root venv Windows
    path.join(root, '.venv', 'bin', 'python'),                     // root venv Unix
    'python',                                                        // system PATH
    'python3',
  ]
  for (const p of candidates) {
    if (p === 'python' || p === 'python3') return p
    if (fs.existsSync(p)) return p
  }
  return 'python'
}

function startPythonBackend() {
  const backendPath = path.join(__dirname, '../backend/main.py')
  const cwd = path.join(__dirname, '..')
  const pythonExe = resolvePython()

  console.log(`[Python] using: ${pythonExe}`)
  console.log(`[Python] script: ${backendPath}`)

  pythonProcess = spawn(pythonExe, [backendPath], {
    cwd,
    env: { ...process.env, LEDROIDE_BACKEND_PORT: String(BACKEND_PORT) },
  })

  pythonProcess.stdout.on('data', (d) => process.stdout.write(`[Python] ${d}`))
  pythonProcess.stderr.on('data', (d) => process.stderr.write(`[Python ERR] ${d}`))
  pythonProcess.on('exit', (code) => {
    console.error(`[Python] process exited with code ${code}`)
  })
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    backgroundColor: '#0f0f12',
    show: false,
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  if (!app.isPackaged && !fs.existsSync(RENDERER_DIST)) {
    mainWindow.loadURL('http://localhost:5173')
  } else {
    mainWindow.loadFile(RENDERER_DIST)
  }
}

// Wait for FastAPI to be ready before opening the window
function waitForBackend(retries = 20, delay = 300) {
  const http = require('http')
  return new Promise((resolve) => {
    const attempt = () => {
      const req = http.get(`http://127.0.0.1:${BACKEND_PORT}/modules`, (res) => {
        if (res.statusCode === 200) return resolve()
        retry()
      })
      req.on('error', retry)
      function retry() {
        if (--retries <= 0) return resolve() // open anyway after timeout
        setTimeout(attempt, delay)
      }
    }
    attempt()
  })
}

// ── IPC: restart backend from renderer ───────────────────────────────────────
ipcMain.handle('restart-backend', async () => {
  if (pythonProcess) {
    pythonProcess.kill()
    await new Promise((resolve) => {
      pythonProcess.once('exit', resolve)
      setTimeout(resolve, 4000) // force-continue after 4s if stuck
    })
    pythonProcess = null
  }
  startPythonBackend()
  await waitForBackend(40, 300)
  return { ok: true }
})

app.whenReady().then(async () => {
  const { session, dialog } = require('electron')
  await session.defaultSession.clearCache()

  session.defaultSession.on('will-download', (event, item) => {
    const defaultPath = require('path').join(app.getPath('downloads'), item.getFilename())
    const savePath = dialog.showSaveDialogSync(mainWindow, {
      defaultPath,
      filters: [
        { name: 'Fichiers Excel', extensions: ['xlsx'] },
        { name: 'Tous les fichiers', extensions: ['*'] },
      ],
    })
    if (savePath) {
      item.setSavePath(savePath)
    } else {
      item.cancel()
    }
  })

  startPythonBackend()
  await waitForBackend()
  createWindow()
})

app.on('window-all-closed', () => {
  if (pythonProcess) pythonProcess.kill()
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})
