import React, { useState, useEffect, useCallback, useRef } from 'react'
import Sidebar from './components/Sidebar'
import ModulesGrid from './components/ModulesGrid'
import ModuleFrame from './components/ModuleFrame'
import CatalogPanel from './components/ModuleManager'
import ModuleIcon from './components/ModuleIcon'
import EnvPanel from './components/EnvPanel'
import PackagesPanel from './components/PackagesPanel'
import { EMPTY_FILTERS } from './components/ModuleFilters'

const api = (path, opts) => window.ledroide.api(path, opts)

export default function App() {
  const [modules, setModules]       = useState([])
  const [openIds, setOpenIds]       = useState([])
  const [currentId, setCurrentId]   = useState(null)
  const [reloadKeys, setReloadKeys] = useState({})
  const [view, setView]             = useState('modules') // 'modules' | 'module' | 'catalog' | 'env' | 'packages'
  const [homeFilters, setHomeFilters] = useState(EMPTY_FILTERS)
  const [toast, setToast]           = useState(null)
  const [op, setOp]                 = useState(null)      // 'reloading' | 'restarting'
  const [backend, setBackend]       = useState({ status: 'ready' })
  const toastTimer = useRef(null)

  const fetchModules = useCallback(async () => {
    try {
      setModules(await api('/modules'))
      setBackend((b) => (b.status === 'error' ? { status: 'ready' } : b))
      return true
    } catch (err) {
      console.error('Backend injoignable:', err.message)
      return false
    }
  }, [])

  useEffect(() => {
    fetchModules().then((ok) => {
      if (!ok) setBackend({ status: 'error', message: 'Le backend Python ne répond pas.' })
    })
    return window.ledroide.onBackendStatus((s) => {
      setBackend(s)
      if (s.status === 'ready') fetchModules()
    })
  }, [fetchModules])

  const showToast = useCallback((text, ok = true) => {
    clearTimeout(toastTimer.current)
    setToast({ text, ok })
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  function openModule(mod) {
    setOpenIds((ids) => (ids.includes(mod.id) ? ids : [...ids, mod.id]))
    setCurrentId(mod.id)
    setView('module')
  }

  function closeModule(modId) {
    const next = openIds.filter((id) => id !== modId)
    setOpenIds(next)
    if (currentId === modId) {
      const fallback = next[next.length - 1] ?? null
      setCurrentId(fallback)
      if (!fallback && view === 'module') setView('modules')
    }
  }

  function reloadModule(modId) {
    setReloadKeys((k) => ({ ...k, [modId]: (k[modId] ?? 0) + 1 }))
  }

  async function hotReload() {
    setOp('reloading')
    try {
      const d = await api('/system/reload', { method: 'POST' })
      const n = d.new_modules?.length ?? 0
      await fetchModules()
      showToast(n > 0 ? `${n} nouveau(x) module(s) détecté(s)` : 'Aucun nouveau module')
    } catch (err) {
      showToast(`Échec du rechargement : ${err.message}`, false)
    } finally {
      setOp(null)
    }
  }

  async function restartBackend() {
    setOp('restarting')
    setBackend({ status: 'restarting', message: 'Redémarrage du backend…' })
    try {
      const { ok } = await window.ledroide.restartBackend()
      if (!ok) throw new Error('le backend ne répond pas')
      setBackend({ status: 'ready' })
      await fetchModules()
      openIds.forEach(reloadModule)
      showToast('Backend redémarré')
    } catch (err) {
      setBackend({ status: 'error', message: `Échec du redémarrage : ${err.message}` })
    } finally {
      setOp(null)
    }
  }

  function handleModuleDeactivated(modId) {
    closeModule(modId)
  }

  const activeModules = modules.filter((m) => m.status === 'active')
  const openModules   = openIds.map((id) => modules.find((m) => m.id === id)).filter((m) => m?.status === 'active')
  const showFrames    = view === 'module' && openModules.length > 0
  const missingEnv    = new Set(activeModules.flatMap((m) => m.env_missing ?? [])).size

  return (
    <div className="app">
      <Sidebar view={view} onNavigate={setView} badges={{ env: missingEnv }} />

      <main className="app-main">
        {toast && (
          <div className={`app-toast ${toast.ok ? 'ok' : 'fail'}`}>
            {toast.ok ? '✓' : '✕'} {toast.text}
          </div>
        )}

        {backend.status !== 'ready' && (
          <BackendBanner backend={backend} onRestart={restartBackend} busy={!!op} />
        )}

        {view === 'module' && openModules.length > 0 && (
          <nav className="app-tabs">
            {openModules.map((mod) => (
              <div
                key={mod.id}
                className={`app-tab ${mod.id === currentId ? 'active' : ''}`}
                onClick={() => setCurrentId(mod.id)}
                onAuxClick={(e) => e.button === 1 && closeModule(mod.id)}
                title={mod.name}
              >
                <ModuleIcon id={mod.id} fallback={mod.icon} size={16} />
                <span className="app-tab-name">{mod.name}</span>
                <button
                  className="app-tab-btn"
                  title="Recharger"
                  onClick={(e) => { e.stopPropagation(); reloadModule(mod.id) }}
                >↻</button>
                <button
                  className="app-tab-btn"
                  title="Fermer"
                  onClick={(e) => { e.stopPropagation(); closeModule(mod.id) }}
                >✕</button>
              </div>
            ))}
          </nav>
        )}

        <div className="app-frames" style={showFrames ? undefined : { display: 'none' }}>
          {openModules.map((mod) => (
            <ModuleFrame
              key={mod.id}
              module={mod}
              hidden={mod.id !== currentId}
              reloadKey={reloadKeys[mod.id] ?? 0}
            />
          ))}
        </div>

        {(view === 'modules' || (view === 'module' && openModules.length === 0)) && (
          <ModulesGrid
            modules={activeModules}
            onOpen={openModule}
            filters={homeFilters}
            onFiltersChange={setHomeFilters}
          />
        )}

        {view === 'catalog' && (
          <CatalogPanel
            modules={modules}
            onRefresh={fetchModules}
            onClose={() => setView(openModules.length ? 'module' : 'modules')}
            onModuleDeactivated={handleModuleDeactivated}
            showToast={showToast}
            onHotReload={hotReload}
            onRestartBackend={restartBackend}
            onOpenEnv={() => setView('env')}
            op={op}
          />
        )}

        {view === 'env' && (
          <EnvPanel modules={modules} onSaved={fetchModules} showToast={showToast} />
        )}

        {view === 'packages' && (
          <PackagesPanel
            modules={modules}
            showToast={showToast}
            onRestartBackend={restartBackend}
            onChanged={fetchModules}
          />
        )}
      </main>
    </div>
  )
}

function BackendBanner({ backend, onRestart, busy }) {
  const error = backend.status === 'error'
  return (
    <div className={`app-banner ${error ? 'error' : 'info'}`}>
      {!error && <span className="cp-spinner" />}
      <span className="app-banner-text">{backend.message ?? 'Backend indisponible'}</span>
      {error && (
        <>
          <button className="btn btn-ghost" onClick={() => window.ledroide.openBackendLog()}>
            Voir le journal
          </button>
          <button className="btn btn-primary" onClick={onRestart} disabled={busy}>
            Redémarrer le backend
          </button>
        </>
      )}
    </div>
  )
}
