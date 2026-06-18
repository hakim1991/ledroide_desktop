const { contextBridge, ipcRenderer } = require('electron')

const BACKEND_PORT = process.env.LEDROIDE_BACKEND_PORT || 8765
const WS_URL = `ws://127.0.0.1:${BACKEND_PORT}/ws`

let ws = null
// key: `${moduleId}:${event}` -> Set of callbacks
const listeners = new Map()
// key: request_id -> { resolve, reject, timer }
const pending = new Map()

function connect() {
  ws = new WebSocket(WS_URL)

  ws.onmessage = (event) => {
    let msg
    try { msg = JSON.parse(event.data) } catch { return }

    if (msg.type === 'response' && pending.has(msg.request_id)) {
      const { resolve, reject, timer } = pending.get(msg.request_id)
      clearTimeout(timer)
      pending.delete(msg.request_id)
      if (msg.error) reject(new Error(msg.error))
      else resolve(msg.payload)
    }

    if (msg.type === 'event') {
      const key = `${msg.module_id}:${msg.action}`
      listeners.get(key)?.forEach((cb) => cb(msg.payload))
    }
  }

  ws.onclose = () => {
    ws = null
    setTimeout(connect, 2000)
  }

  ws.onerror = () => ws.close()
}

function getWs() {
  if (!ws || ws.readyState === WebSocket.CLOSED) connect()
  return ws
}

contextBridge.exposeInMainWorld('ledroide', {
  backendPort: BACKEND_PORT,

  call(moduleId, action, payload = {}) {
    return new Promise((resolve, reject) => {
      const requestId = crypto.randomUUID()
      const timer = setTimeout(() => {
        pending.delete(requestId)
        reject(new Error('RPC timeout'))
      }, 10000)

      pending.set(requestId, { resolve, reject, timer })

      const msg = JSON.stringify({
        type: 'call',
        module_id: moduleId,
        action,
        payload,
        request_id: requestId,
      })

      const socket = getWs()
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(msg)
      } else if (socket) {
        socket.addEventListener('open', () => socket.send(msg), { once: true })
      } else {
        clearTimeout(timer)
        pending.delete(requestId)
        reject(new Error('WebSocket not available'))
      }
    })
  },

  on(moduleId, event, callback) {
    const key = `${moduleId}:${event}`
    if (!listeners.has(key)) listeners.set(key, new Set())
    listeners.get(key).add(callback)
  },

  off(moduleId, event, callback) {
    const key = `${moduleId}:${event}`
    listeners.get(key)?.delete(callback)
  },

  // Ask Electron main to kill + respawn the Python process
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
})

// Initiate connection on preload load
connect()
