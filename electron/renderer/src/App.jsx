import React, { useState, useEffect, useCallback, useRef } from 'react'
import Sidebar, { NAV } from './components/Sidebar'
import ModulesGrid from './components/ModulesGrid'
import ModuleFrame from './components/ModuleFrame'
import CatalogPanel from './components/ModuleManager'
import ModuleIcon from './components/ModuleIcon'
import EnvPanel from './components/EnvPanel'
import PackagesPanel from './components/PackagesPanel'
import { EMPTY_FILTERS } from './components/ModuleFilters'

const api = (path, opts) => window.ledroide.api(path, opts)

// Onglets : id de module, ou « sys:<page> » pour les pages système (Catalogue, Variables, Bibliothèques).
const SYS = 'sys:'
const PAGES = Object.fromEntries(NAV.filter((n) => n.id !== 'modules').map((n) => [n.id, n]))
const isPage = (key) => key.startsWith(SYS)
const pageOf = (key) => key.slice(SYS.length)

export default function App() {
  const [modules, setModules]       = useState([])
  const [tabs, setTabs]             = useState([])
  const [current, setCurrent]       = useState(null)      // clé d'onglet, null = accueil
  const [reloadKeys, setReloadKeys] = useState({})
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

  function openTab(key) {
    setTabs((t) => (t.includes(key) ? t : [...t, key]))
    setCurrent(key)
  }

  const openModule = (mod) => openTab(mod.id)

  function navigate(id) {
    if (id === 'modules') setCurrent(null)
    else openTab(SYS + id)
  }

  function closeTab(key) {
    const next = tabs.filter((k) => k !== key)
    setTabs(next)
    if (current === key) setCurrent(next[next.length - 1] ?? null)
  }

  function reloadTab(key) {
    setReloadKeys((k) => ({ ...k, [key]: (k[key] ?? 0) + 1 }))
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
      tabs.filter((k) => !isPage(k)).forEach(reloadTab)
      showToast('Backend redémarré')
    } catch (err) {
      setBackend({ status: 'error', message: `Échec du redémarrage : ${err.message}` })
    } finally {
      setOp(null)
    }
  }


  const activeModules = modules.filter((m) => m.status === 'active')
  const openModules   = tabs.filter((k) => !isPage(k)).map((id) => modules.find((m) => m.id === id)).filter((m) => m?.status === 'active')
  const visibleTabs   = tabs.filter((k) => isPage(k) || openModules.some((m) => m.id === k))
  const shown         = visibleTabs.includes(current) ? current : null
  const sidebarView   = shown && isPage(shown) ? pageOf(shown) : 'modules'
  const missingEnv    = new Set(activeModules.flatMap((m) => m.env_missing ?? [])).size

  return (
    <div className="app">
      <Sidebar view={sidebarView} onNavigate={navigate} badges={{ env: missingEnv }} />

      <main className="app-main">
        {toast && (
          <div className={`app-toast ${toast.ok ? 'ok' : 'fail'}`}>
            {toast.ok ? '✓' : '✕'} {toast.text}
          </div>
        )}

        {backend.status !== 'ready' && (
          <BackendBanner backend={backend} onRestart={restartBackend} busy={!!op} />
        )}
        {backend.status === 'ready' && (
          <RepairBanner onChanged={fetchModules} onOpenCatalog={() => navigate('catalog')} showToast={showToast} />
        )}

        {visibleTabs.length > 0 && (
          <nav className="app-tabs">
            {visibleTabs.map((key) => {
              const page = isPage(key) ? PAGES[pageOf(key)] : null
              const mod  = page ? null : openModules.find((m) => m.id === key)
              const name = page ? page.label : mod.name
              return (
                <div
                  key={key}
                  className={`app-tab ${key === shown ? 'active' : ''} ${page ? 'sys' : ''}`}
                  onClick={() => setCurrent(key)}
                  onAuxClick={(e) => e.button === 1 && closeTab(key)}
                  title={name}
                >
                  {page
                    ? <span className="app-tab-icon">{page.icon}</span>
                    : <ModuleIcon id={mod.id} fallback={mod.icon} size={16} />}
                  <span className="app-tab-name">{name}</span>
                  <button
                    className="app-tab-btn"
                    title="Recharger"
                    onClick={(e) => { e.stopPropagation(); reloadTab(key) }}
                  >↻</button>
                  <button
                    className="app-tab-btn"
                    title="Fermer"
                    onClick={(e) => { e.stopPropagation(); closeTab(key) }}
                  >✕</button>
                </div>
              )
            })}
          </nav>
        )}

        <div className="app-frames" style={shown && !isPage(shown) ? undefined : { display: 'none' }}>
          {openModules.map((mod) => (
            <ModuleFrame
              key={mod.id}
              module={mod}
              hidden={mod.id !== shown}
              reloadKey={reloadKeys[mod.id] ?? 0}
            />
          ))}
        </div>

        {!shown && (
          <ModulesGrid
            modules={activeModules}
            onOpen={openModule}
            filters={homeFilters}
            onFiltersChange={setHomeFilters}
          />
        )}

        {visibleTabs.filter(isPage).map((key) => (
          <div key={`${key}:${reloadKeys[key] ?? 0}`} className="app-page" style={key === shown ? undefined : { display: 'none' }}>
            {pageOf(key) === 'catalog' && (
              <CatalogPanel
                modules={modules}
                onRefresh={fetchModules}
                onModuleDeactivated={closeTab}
                showToast={showToast}
                onHotReload={hotReload}
                onRestartBackend={restartBackend}
                onOpenEnv={() => navigate('env')}
                op={op}
              />
            )}
            {pageOf(key) === 'env' && (
              <EnvPanel modules={modules} onSaved={fetchModules} showToast={showToast} />
            )}
            {pageOf(key) === 'packages' && (
              <PackagesPanel
                modules={modules}
                showToast={showToast}
                onRestartBackend={restartBackend}
                onChanged={fetchModules}
              />
            )}
          </div>
        ))}
      </main>
    </div>
  )
}

const REPAIR_REASON = {
  update: 'Mise à jour de l’application détectée',
  startup: 'Bibliothèques manquantes',
  manual: 'Réparation des modules',
}

// Startup self-repair: modules whose Python libraries vanished are restored in the background
// by the backend; this banner follows its `modules:repair` events.
function RepairBanner({ onChanged, onOpenCatalog, showToast }) {
  const [st, setSt] = useState(null)
  const [retrying, setRetrying] = useState(false)
  const hideTimer = useRef(null)

  useEffect(() => {
    let alive = true
    api('/modules/repair').then((d) => { if (alive && d.running) setSt(d) }).catch(() => {})
    const off = window.ledroide.on('__system__', 'modules:repair', (d) => {
      clearTimeout(hideTimer.current)
      setSt((prev) => ({ ...prev, ...d, log: d.log ?? prev?.log }))
      if (d.stage === 'end') {
        onChanged()
        if (!d.failed?.length) {
          if (d.reason !== 'manual' && d.activated?.length) {
            showToast(`${d.activated.length} module${d.activated.length > 1 ? 's' : ''} restauré${d.activated.length > 1 ? 's' : ''}`)
          }
          hideTimer.current = setTimeout(() => setSt(null), 400)
        }
      }
    })
    return () => { alive = false; off?.(); clearTimeout(hideTimer.current) }
  }, [onChanged, showToast])

  if (!st) return null

  async function retry() {
    setRetrying(true)
    try {
      await api('/modules/repair', { method: 'POST' })
    } catch (err) {
      showToast(err.message, false)
    } finally {
      setRetrying(false)
    }
  }

  if (st.running) {
    const n = st.modules?.length ?? 0
    const pk = st.packages ?? []
    const what = st.stage === 'deps'
      ? `installation de ${pk.length} bibliothèque${pk.length > 1 ? 's' : ''} pour ${n} module${n > 1 ? 's' : ''} (${pk.slice(0, 4).join(', ')}${pk.length > 4 ? '…' : ''})`
      : `réactivation des modules (${Math.min((st.done ?? 0) + 1, n)}/${n})`
    return (
      <div className="app-banner info">
        <span className="cp-spinner" />
        <span className="app-banner-text">
          {REPAIR_REASON[st.reason] ?? REPAIR_REASON.manual} : {what}…
          {st.log && <span className="app-banner-log">{st.log}</span>}
        </span>
      </div>
    )
  }

  const failed = st.failed ?? []
  if (!failed.length) return null
  return (
    <div className="app-banner error">
      <span className="app-banner-text">
        {failed.length} module{failed.length > 1 ? 's' : ''} n’{failed.length > 1 ? 'ont' : 'a'} pas pu être restauré{failed.length > 1 ? 's' : ''}
        {st.pip_ok === false ? ' (installation des bibliothèques impossible — connexion Internet ?)' : ''} :{' '}
        <span title={failed.map((f) => `${f.name} : ${f.error}`).join('\n')}>{failed.map((f) => f.name).join(', ')}</span>
      </span>
      <button className="btn btn-ghost" onClick={onOpenCatalog}>Catalogue</button>
      <button className="btn btn-primary" onClick={retry} disabled={retrying}>
        {retrying ? <span className="cp-spinner" /> : 'Réessayer'}
      </button>
      <button className="btn btn-ghost" onClick={() => setSt(null)} title="Masquer">✕</button>
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
