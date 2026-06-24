import React, { useState, useEffect, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import ModulesGrid from './components/ModulesGrid'
import ModuleFrame from './components/ModuleFrame'
import CatalogPanel from './components/ModuleManager'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765
const API = `http://127.0.0.1:${BACKEND_PORT}`

export default function App() {
  const [modules, setModules]               = useState([])
  const [activeModuleId, setActiveModuleId] = useState(null)
  const [view, setView]                     = useState('modules') // 'modules' | 'module' | 'catalog'
  const [toast, setToast]                   = useState(null)
  const [op, setOp]                         = useState(null)      // 'reloading' | 'restarting'

  const fetchModules = useCallback(async () => {
    try {
      const r = await fetch(`${API}/modules`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setModules(await r.json())
    } catch (err) {
      console.error('Backend injoignable:', err.message)
    }
  }, [])

  useEffect(() => { fetchModules() }, [fetchModules])

  function showToast(text, ok = true) {
    setToast({ text, ok })
    setTimeout(() => setToast(null), 3000)
  }

  async function handleOpenModule(mod) {
    if (mod.status === 'installed') {
      await fetch(`${API}/modules/${mod.id}/activate`, { method: 'POST' })
      await fetchModules()
    }
    setActiveModuleId(mod.id)
    setView('module')
  }

  async function hotReload() {
    setOp('reloading')
    try {
      const r = await fetch(`${API}/system/reload`, { method: 'POST' })
      const d = await r.json()
      const n = d.new_modules?.length ?? 0
      await fetchModules()
      showToast(n > 0 ? `${n} nouveau(x) module(s) détecté(s)` : 'Aucun nouveau module')
    } catch {
      showToast('Échec du rechargement', false)
    } finally {
      setOp(null)
    }
  }

  async function restartBackend() {
    if (!window.ledroide?.restartBackend) {
      showToast('Redémarrage non disponible hors Electron', false)
      return
    }
    setOp('restarting')
    try {
      await window.ledroide.restartBackend()
      await fetchModules()
      showToast('Backend redémarré')
    } catch (err) {
      showToast(`Erreur : ${err.message}`, false)
    } finally {
      setOp(null)
    }
  }

  function handleModuleDeactivated(modId) {
    if (activeModuleId === modId) {
      setActiveModuleId(null)
      setView('modules')
    }
  }

  const installedModules = modules.filter((m) => m.status === 'installed')
  const activeModule     = modules.find((m) => m.id === activeModuleId)

  return (
    <div className="app">
      <Sidebar view={view} onNavigate={setView} />

      <main className="app-main">
        {toast && (
          <div className={`app-toast ${toast.ok ? 'ok' : 'fail'}`}>
            {toast.ok ? '✓' : '✕'} {toast.text}
          </div>
        )}

        {view === 'modules' && (
          <ModulesGrid
            modules={installedModules}
            onOpen={handleOpenModule}
          />
        )}

        {view === 'module' && activeModule && (
          <ModuleFrame module={activeModule} />
        )}

        {view === 'module' && !activeModule && (
          <ModulesGrid modules={installedModules} onOpen={handleOpenModule} />
        )}

        {view === 'catalog' && (
          <CatalogPanel
            modules={modules}
            onRefresh={fetchModules}
            onClose={() => setView('modules')}
            onModuleDeactivated={handleModuleDeactivated}
            showToast={showToast}
            onHotReload={hotReload}
            onRestartBackend={restartBackend}
            op={op}
          />
        )}
      </main>
    </div>
  )
}
