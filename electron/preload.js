const { contextBridge, ipcRenderer } = require('electron')

const { port: BACKEND_PORT, token: TOKEN } = ipcRenderer.sendSync('ledroide:config')
const API_URL = `http://127.0.0.1:${BACKEND_PORT}`
const WS_URL = `ws://127.0.0.1:${BACKEND_PORT}/ws?token=${TOKEN}`
const DEFAULT_TIMEOUT = 10000

let ws = null
const listeners = new Map() // `${moduleId}:${event}` -> Set<callback>
const pending = new Map()   // request_id -> { resolve, reject, timer }

function makeError(payload) {
  const err = new Error(payload?.error ?? 'Erreur inconnue')
  err.code = payload?.code
  return err
}

function rejectAllPending(message, code) {
  for (const [id, p] of pending) {
    clearTimeout(p.timer)
    p.reject(Object.assign(new Error(message), { code }))
    pending.delete(id)
  }
}

function connect() {
  ws = new WebSocket(WS_URL)

  ws.onmessage = (event) => {
    let msg
    try { msg = JSON.parse(event.data) } catch { return }

    if (msg.type === 'response' && pending.has(msg.request_id)) {
      const { resolve, reject, timer } = pending.get(msg.request_id)
      clearTimeout(timer)
      pending.delete(msg.request_id)
      if (msg.payload && msg.payload.error) reject(makeError(msg.payload))
      else resolve(msg.payload)
    }

    if (msg.type === 'event') {
      listeners.get(`${msg.module_id}:${msg.action}`)?.forEach((cb) => {
        try { cb(msg.payload) } catch (e) { console.error(e) }
      })
    }
  }

  ws.onclose = () => {
    ws = null
    rejectAllPending('Connexion au backend perdue', 'WS_CLOSED')
    setTimeout(connect, 1000)
  }

  ws.onerror = () => ws?.close()
}

function getWs() {
  if (!ws || ws.readyState === WebSocket.CLOSED) connect()
  return ws
}

function call(moduleId, action, payload = {}, { timeout = DEFAULT_TIMEOUT } = {}) {
  return new Promise((resolve, reject) => {
    const requestId = crypto.randomUUID()
    const timer = timeout
      ? setTimeout(() => {
          pending.delete(requestId)
          reject(Object.assign(new Error('RPC timeout'), { code: 'RPC_TIMEOUT' }))
        }, timeout)
      : null
    pending.set(requestId, { resolve, reject, timer })

    const msg = JSON.stringify({ type: 'call', module_id: moduleId, action, payload, request_id: requestId })
    const socket = getWs()
    if (socket.readyState === WebSocket.OPEN) socket.send(msg)
    else socket.addEventListener('open', () => socket.send(msg), { once: true })
  })
}

function on(moduleId, event, callback) {
  const key = `${moduleId}:${event}`
  if (!listeners.has(key)) listeners.set(key, new Set())
  listeners.get(key).add(callback)
  return () => off(moduleId, event, callback)
}

function off(moduleId, event, callback) {
  listeners.get(`${moduleId}:${event}`)?.delete(callback)
}

function run(moduleId, action, payload = {}, { onProgress, onStart, timeout } = {}) {
  return new Promise((resolve, reject) => {
    let jobId = null
    let buffered = []
    const handle = (kind, d) => {
      if (jobId === null) { buffered.push([kind, d]); return }
      if (d?.job_id !== jobId) return
      if (kind === 'progress') { onProgress?.(d); return }
      unsubscribe.forEach((u) => u())
      if (kind === 'done') resolve(d.result)
      else reject(makeError(d))
    }
    const unsubscribe = [
      on(moduleId, 'job:progress', (d) => handle('progress', d)),
      on(moduleId, 'job:done', (d) => handle('done', d)),
      on(moduleId, 'job:error', (d) => handle('error', d)),
    ]
    call(moduleId, action, payload, { timeout }).then((r) => {
      jobId = r?.job_id ?? null
      if (!jobId) { unsubscribe.forEach((u) => u()); resolve(r); return }
      onStart?.(jobId)
      const queue = buffered
      buffered = []
      queue.forEach(([k, d]) => handle(k, d))
    }, (err) => { unsubscribe.forEach((u) => u()); reject(err) })
  })
}

// REST call to the backend management API; throws Error{code} on {"error","code"} bodies.
async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'X-Ledroide-Token': TOKEN }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const r = await fetch(`${API_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok || (data && data.error)) {
    throw makeError(data?.error ? data : { error: `HTTP ${r.status}`, code: `HTTP_${r.status}` })
  }
  return data
}

contextBridge.exposeInMainWorld('ledroide', {
  backendPort: BACKEND_PORT,
  backendOrigin: API_URL,
  call,
  run,
  cancel: (moduleId, jobId) => call(moduleId, 'job:cancel', { job_id: jobId }),
  on,
  off,
  api,
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  openBackendLog: () => ipcRenderer.invoke('open-backend-log'),
  isBackendReady: () => ipcRenderer.invoke('backend-ready'),
  onBackendStatus(callback) {
    const listener = (_event, status) => callback(status)
    ipcRenderer.on('backend-status', listener)
    return () => ipcRenderer.removeListener('backend-status', listener)
  },
})

connect()
