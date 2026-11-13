import { app, BrowserWindow, ipcMain, dialog, session, shell } from 'electron'
import path from 'node:path'
import fs from 'node:fs'
import net from 'node:net'
import http from 'node:http'
import crypto from 'node:crypto'
import { spawn, spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.join(__dirname, '..')
const RENDERER_DIST = path.join(ROOT, 'dist/renderer/index.html')
const PREFERRED_PORT = Number(process.env.LEDROIDE_BACKEND_PORT) || 8765
const TOKEN = crypto.randomBytes(32).toString('hex')
const MAX_AUTO_RESTARTS = 3
const AUTO_RESTART_WINDOW_MS = 60_000

if (process.env.LEDROIDE_USER_DATA) app.setPath('userData', path.resolve(process.env.LEDROIDE_USER_DATA))

let backendPort = PREFERRED_PORT
let mainWindow = null
let pythonProcess = null
let expectedExit = false
let recentCrashes = []
let backendLogFd = null
let backendLogSize = 0
let quitting = false

// ── Logging ──────────────────────────────────────────────────────────────────

function backendLogPath() {
  return path.join(app.getPath('userData'), 'backend.log')
}

const LOG_MAX_BYTES = 5 * 1024 * 1024
const LOG_ARCHIVES = 2  // backend.1.log, backend.2.log

// Synchronous writes on a raw fd: the file can be closed and renamed immediately on rotation
// (Windows refuses to rename a file a pending write stream still holds open).
function openBackendLog() {
  const file = backendLogPath()
  try {
    backendLogSize = fs.statSync(file).size
  } catch {
    backendLogSize = 0
  }
  if (backendLogSize >= LOG_MAX_BYTES) {
    rotateBackendLog(file)
    backendLogSize = 0
  }
  backendLogFd = fs.openSync(file, 'a')
}

function rotateBackendLog(file) {
  const archive = (i) => file.replace(/\.log$/, `.${i}.log`)
  try {
    fs.rmSync(archive(LOG_ARCHIVES), { force: true })
    for (let i = LOG_ARCHIVES - 1; i >= 1; i--) {
      if (fs.existsSync(archive(i))) fs.renameSync(archive(i), archive(i + 1))
    }
    fs.renameSync(file, archive(1))
  } catch (err) {
    // Rotation is best effort: if the file is locked, truncate rather than grow without bound.
    console.error('log rotation failed:', err.message)
    try { fs.truncateSync(file, 0) } catch { /* ignore */ }
  }
}

function logBackend(line) {
  const text = `[${new Date().toISOString()}] ${line}\n`
  console.log(line)
  try {
    if (backendLogFd === null) openBackendLog()
    if (backendLogSize >= LOG_MAX_BYTES) {
      fs.closeSync(backendLogFd)
      backendLogFd = null
      openBackendLog()
    }
    backendLogSize += fs.writeSync(backendLogFd, text)
  } catch (err) {
    console.error('backend log write failed:', err.message)
  }
}

// ── Python process ───────────────────────────────────────────────────────────

function resolvePython() {
  const candidates = [
    app.isPackaged ? path.join(process.resourcesPath, 'python', 'python.exe') : null,
    path.join(ROOT, 'backend', 'python-dist', 'python.exe'),
    path.join(ROOT, 'backend', '.venv', 'Scripts', 'python.exe'),
    path.join(ROOT, 'backend', '.venv', 'bin', 'python'),
    path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(ROOT, '.venv', 'bin', 'python'),
  ].filter(Boolean)
  return candidates.find((p) => fs.existsSync(p)) ?? (process.platform === 'win32' ? 'python' : 'python3')
}

function isPortFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer()
    srv.once('error', () => resolve(false))
    srv.listen(port, '127.0.0.1', () => srv.close(() => resolve(true)))
  })
}

function randomFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.once('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
  })
}

// The preferred port is kept when possible: module iframes' localStorage is keyed by origin (port included).
async function choosePort() {
  if (await isPortFree(PREFERRED_PORT)) return PREFERRED_PORT
  const port = await randomFreePort()
  logBackend(`[Python] port ${PREFERRED_PORT} busy, using ${port}`)
  return port
}

function sendStatus(status, detail = {}) {
  mainWindow?.webContents.send('backend-status', { status, ...detail })
}

function startPythonBackend() {
  const backendPath = path.join(ROOT, 'backend', 'main.py')
  const pythonExe = resolvePython()
  const dataDir = path.join(app.getPath('userData'), 'data')
  fs.mkdirSync(dataDir, { recursive: true })

  logBackend(`[Python] using: ${pythonExe} (port ${backendPort}, data ${dataDir})`)
  expectedExit = false

  const proc = spawn(pythonExe, [backendPath], {
    cwd: ROOT,
    windowsHide: true,
    env: {
      ...process.env,
      LEDROIDE_BACKEND_PORT: String(backendPort),
      LEDROIDE_TOKEN: TOKEN,
      LEDROIDE_DATA_DIR: dataDir,
      LEDROIDE_APP_VERSION: app.getVersion(),
      PYTHONUSERBASE: path.join(dataDir, 'python'),
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
    },
  })
  pythonProcess = proc

  proc.stdout.on('data', (d) => logBackend(`[Python] ${d.toString().trimEnd()}`))
  proc.stderr.on('data', (d) => logBackend(`[Python ERR] ${d.toString().trimEnd()}`))
  proc.on('error', (err) => {
    logBackend(`[Python] failed to spawn: ${err.message}`)
    sendStatus('error', { message: `Impossible de lancer Python : ${err.message}` })
  })
  proc.on('exit', (code, signal) => {
    logBackend(`[Python] exited with code ${code} (signal: ${signal})`)
    if (pythonProcess === proc) pythonProcess = null
    if (expectedExit || quitting) return
    handleCrash(code)
  })
}

async function handleCrash(code) {
  const now = Date.now()
  recentCrashes = recentCrashes.filter((t) => now - t < AUTO_RESTART_WINDOW_MS)
  recentCrashes.push(now)
  if (recentCrashes.length > MAX_AUTO_RESTARTS) {
    sendStatus('error', { message: `Le backend s'est arrêté (code ${code}) et ne redémarre plus.` })
    return
  }
  sendStatus('restarting', { message: `Le backend s'est arrêté (code ${code}), redémarrage…` })
  startPythonBackend()
  sendStatus((await waitForBackend()) ? 'ready' : 'error', { message: 'Le backend ne répond pas.' })
}

function request(method, urlPath, timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.request(
      { host: '127.0.0.1', port: backendPort, path: urlPath, method, timeout,
        headers: { 'X-Ledroide-Token': TOKEN } },
      (res) => { res.resume(); resolve(res.statusCode) },
    )
    req.on('timeout', () => req.destroy())
    req.on('error', () => resolve(null))
    req.end()
  })
}

async function waitForBackend(retries = 60, delay = 300) {
  for (let i = 0; i < retries; i++) {
    if (!pythonProcess) return false
    if ((await request('GET', '/system/health')) === 200) return true
    await new Promise((r) => setTimeout(r, delay))
  }
  return false
}

function killTree(proc) {
  if (!proc || proc.exitCode !== null) return
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(proc.pid), '/T', '/F'], { windowsHide: true })
  } else {
    proc.kill('SIGKILL')
  }
}

// Graceful first (module on_shutdown hooks run), then kill the whole tree (ffmpeg, yt-dlp…).
async function stopPythonBackend(graceMs = 3000) {
  const proc = pythonProcess
  if (!proc) return
  expectedExit = true
  const exited = new Promise((resolve) => proc.once('exit', resolve))
  await request('POST', '/system/shutdown')
  const timedOut = await Promise.race([
    exited.then(() => false),
    new Promise((r) => setTimeout(() => r(true), graceMs)),
  ])
  if (timedOut) killTree(proc)
  pythonProcess = null
}

// ── Window ───────────────────────────────────────────────────────────────────

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
      sandbox: true,
    },
    backgroundColor: '#0f0f12',
    show: false,
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())
  mainWindow.on('closed', () => { mainWindow = null })

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('file://') && !url.startsWith('http://localhost:5173')) event.preventDefault()
  })

  if (!app.isPackaged && !fs.existsSync(RENDERER_DIST)) {
    mainWindow.loadURL('http://localhost:5173')
  } else {
    mainWindow.loadFile(RENDERER_DIST)
  }
}

function saveDialogFilters(filename) {
  const ext = path.extname(filename).slice(1).toLowerCase()
  const all = { name: 'Tous les fichiers', extensions: ['*'] }
  return ext ? [{ name: `Fichier ${ext.toUpperCase()}`, extensions: [ext] }, all] : [all]
}

// ── IPC ──────────────────────────────────────────────────────────────────────

ipcMain.on('ledroide:config', (event) => {
  event.returnValue = { port: backendPort, token: TOKEN }
})

ipcMain.handle('restart-backend', async () => {
  await stopPythonBackend()
  recentCrashes = []
  startPythonBackend()
  const ok = await waitForBackend()
  sendStatus(ok ? 'ready' : 'error', { message: ok ? undefined : 'Le backend ne répond pas.' })
  return { ok }
})

ipcMain.handle('open-backend-log', () => shell.openPath(backendLogPath()))

ipcMain.handle('backend-ready', () => waitForBackend(1, 0))

// ── Lifecycle ────────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  session.defaultSession.on('will-download', (_event, item) => {
    const filename = item.getFilename()
    const savePath = dialog.showSaveDialogSync(mainWindow, {
      defaultPath: path.join(app.getPath('downloads'), filename),
      filters: saveDialogFilters(filename),
    })
    if (savePath) item.setSavePath(savePath)
    else item.cancel()
  })

  backendPort = await choosePort()
  startPythonBackend()
  const ready = await waitForBackend()
  createWindow()
  if (!ready) {
    mainWindow.webContents.once('did-finish-load', () =>
      sendStatus('error', { message: 'Le backend Python ne répond pas.' }))
  }
})

app.on('before-quit', async (event) => {
  if (quitting || !pythonProcess) return
  event.preventDefault()
  quitting = true
  await stopPythonBackend()
  app.quit()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})
