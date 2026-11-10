import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './PackagesPanel.css'

const api = (path, opts) => window.ledroide.api(path, opts)
const SYSTEM = '__system__'
const SPEC_RE = /^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(\[[A-Za-z0-9._,\s-]+\])?\s*((===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*$/

const FILTERS = [
  { id: 'all',      label: 'Toutes' },
  { id: 'modules',  label: 'Utilisées par un module' },
  { id: 'outdated', label: 'Mises à jour' },
  { id: 'user',     label: 'Hors système' },
]

export default function PackagesPanel({ modules, showToast, onRestartBackend, onChanged }) {
  const [data, setData]           = useState(null)
  const [error, setError]         = useState(null)
  const [outdated, setOutdated]   = useState(null)
  const [checking, setChecking]   = useState(false)
  const [busy, setBusy]           = useState(null)      // { action, target }
  const [log, setLog]             = useState([])
  const [showLog, setShowLog]     = useState(false)
  const [spec, setSpec]           = useState('')
  const [query, setQuery]         = useState('')
  const [filter, setFilter]       = useState('all')
  const [confirm, setConfirm]     = useState(null)      // package key awaiting uninstall confirmation
  const [needRestart, setNeedRestart] = useState(false)
  const logRef = useRef(null)

  const load = useCallback(async () => {
    try {
      setData(await api('/system/packages'))
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => { load() }, [load])

  useEffect(() => window.ledroide.on(SYSTEM, 'packages:log', (d) => {
    setLog((l) => [...l, d.line].slice(-400))
  }), [])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [log, showLog])

  const moduleNames = useMemo(() => new Map(modules.map((m) => [m.id, m.name])), [modules])

  async function checkUpdates() {
    setChecking(true)
    try {
      const { outdated } = await api('/system/packages/outdated')
      setOutdated(outdated)
      const n = Object.keys(outdated).length
      showToast(n ? `${n} mise(s) à jour disponible(s)` : 'Tout est à jour')
      if (n) setFilter('outdated')
    } catch (e) {
      showToast(e.message, false)
    } finally {
      setChecking(false)
    }
  }

  async function runOp(action, target, request, success) {
    setBusy({ action, target })
    setConfirm(null)
    setLog([])
    setShowLog(true)
    try {
      const r = await request()
      if (r.ok) {
        showToast(success)
        if (r.restart_recommended) setNeedRestart(true)
        if (action !== 'install') setOutdated((o) => {
          if (!o) return o
          const { [target]: _, ...rest } = o
          return rest
        })
      } else {
        showToast(`Échec : voir le journal pip`, false)
      }
      await load()
      onChanged?.()
      return r.ok
    } catch (e) {
      setLog((l) => [...l, `✕ ${e.message}`])
      showToast(e.message, false)
      return false
    } finally {
      setBusy(null)
    }
  }

  function install(e) {
    e.preventDefault()
    const s = spec.trim()
    runOp('install', s, () => api('/system/packages', { method: 'POST', body: { spec: s } }), `${s} installée`)
      .then((ok) => ok && setSpec(''))
  }

  const upgrade = (p) => runOp(
    'upgrade', p.key,
    () => api(`/system/packages/${encodeURIComponent(p.name)}/upgrade`, { method: 'POST' }),
    `${p.name} mise à jour`,
  )

  const uninstall = (p) => runOp(
    'uninstall', p.key,
    () => api(`/system/packages/${encodeURIComponent(p.name)}`, { method: 'DELETE' }),
    `${p.name} désinstallée`,
  )

  if (data === null) {
    return (
      <div className="pk-layout">
        <div className="pk-content">
          {error ? <p className="pk-error">{error}</p> : <span className="cp-spinner" />}
        </div>
      </div>
    )
  }

  const specValid = SPEC_RE.test(spec.trim())
  const q = query.trim().toLowerCase()
  const counts = {
    all: data.packages.length,
    modules: data.packages.filter((p) => p.modules.length).length,
    outdated: outdated ? data.packages.filter((p) => outdated[p.key]).length : null,
    user: data.packages.filter((p) => !p.protected).length,
  }
  const visible = data.packages.filter((p) => {
    if (filter === 'modules' && !p.modules.length) return false
    if (filter === 'outdated' && !outdated?.[p.key]) return false
    if (filter === 'user' && p.protected) return false
    return !q || p.key.includes(q) || p.summary.toLowerCase().includes(q)
  })

  return (
    <div className="pk-layout">
      <header className="pk-header">
        <div>
          <h2 className="pk-title">Bibliothèques Python</h2>
          <p className="pk-sub">
            Environnement partagé par le backend et tous les modules · Python {data.python}
          </p>
        </div>
        <div className="pk-actions">
          <button className="btn btn-ghost" onClick={() => setShowLog((s) => !s)} disabled={!log.length}>
            Journal
          </button>
          <button className="btn btn-ghost" onClick={checkUpdates} disabled={checking || !!busy}>
            {checking ? <span className="cp-spinner" /> : null} Vérifier les mises à jour
          </button>
        </div>
      </header>

      <div className="pk-content">
        {error && <p className="pk-error">{error}</p>}

        {needRestart && (
          <div className="pk-restart">
            <span>Une bibliothèque déjà chargée a changé : redémarrez le backend pour utiliser la nouvelle version.</span>
            <button
              className="btn btn-primary"
              onClick={async () => { await onRestartBackend(); setNeedRestart(false); load() }}
            >
              Redémarrer le backend
            </button>
          </div>
        )}

        <form className="pk-install" onSubmit={install}>
          <input
            className={`pk-input ${spec && !specValid ? 'invalid' : ''}`}
            placeholder="Nom de la bibliothèque, ex. requests ou requests>=2,<3"
            spellCheck={false}
            value={spec}
            onChange={(e) => setSpec(e.target.value)}
            disabled={!!busy}
          />
          <button className="btn btn-primary" type="submit" disabled={!specValid || !!busy}>
            {busy?.action === 'install' ? <span className="cp-spinner" /> : null} Installer
          </button>
        </form>

        {showLog && (
          <pre className="pk-log" ref={logRef}>
            {log.length ? log.join('\n') : '…'}
          </pre>
        )}

        <div className="pk-toolbar">
          <div className="pk-filters">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                className={`pk-chip ${filter === f.id ? 'active' : ''}`}
                onClick={() => setFilter(f.id)}
                disabled={f.id === 'outdated' && outdated === null}
                title={f.id === 'outdated' && outdated === null ? 'Cliquez sur « Vérifier les mises à jour »' : undefined}
              >
                {f.label}{counts[f.id] !== null && <span className="pk-chip-count">{counts[f.id]}</span>}
              </button>
            ))}
          </div>
          <input
            className="pk-input pk-filter"
            placeholder="Filtrer…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <div className="pk-table">
          <div className="pk-row pk-row-head">
            <span>Bibliothèque</span><span>Version</span><span>Utilisée par</span><span />
          </div>
          {visible.length === 0 && <p className="pk-empty">Aucune bibliothèque.</p>}
          {visible.map((p) => {
            const latest = outdated?.[p.key]
            const running = busy?.target === p.key
            const dependents = [...p.modules.map((id) => moduleNames.get(id) ?? id), ...p.required_by]
            return (
              <div key={p.key} className="pk-row">
                <div className="pk-cell">
                  <span className="pk-name">
                    {p.name}
                    {p.protected && <span className="pk-badge" title="Nécessaire au fonctionnement de Ledroide">système</span>}
                  </span>
                  {p.summary && <span className="pk-summary">{p.summary}</span>}
                </div>
                <div className="pk-cell">
                  <code className="pk-version">{p.version}</code>
                  {latest && <span className="pk-latest">→ {latest}</span>}
                </div>
                <div className="pk-cell pk-users">
                  {p.modules.length > 0 && (
                    <span className="pk-mods">{p.modules.map((id) => moduleNames.get(id) ?? id).join(', ')}</span>
                  )}
                  {p.required_by.length > 0 && (
                    <span className="pk-deps" title={p.required_by.join(', ')}>
                      dépendance de {p.required_by.slice(0, 3).join(', ')}
                      {p.required_by.length > 3 && ` +${p.required_by.length - 3}`}
                    </span>
                  )}
                </div>
                <div className="pk-row-actions">
                  {running ? (
                    <span className="cp-spinner" />
                  ) : p.protected ? null : confirm === p.key ? (
                    <>
                      {dependents.length > 0 && (
                        <span className="pk-warn" title={dependents.join(', ')}>
                          ⚠ utilisée par {dependents.length}
                        </span>
                      )}
                      <button className="btn btn-danger" onClick={() => uninstall(p)}>Confirmer</button>
                      <button className="btn btn-ghost" onClick={() => setConfirm(null)}>Annuler</button>
                    </>
                  ) : (
                    <>
                      {latest && (
                        <button className="btn btn-secondary" onClick={() => upgrade(p)} disabled={!!busy}>
                          Mettre à jour
                        </button>
                      )}
                      <button
                        className="pk-icon-btn"
                        title="Désinstaller"
                        onClick={() => setConfirm(p.key)}
                        disabled={!!busy}
                      >✕</button>
                    </>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        <p className="pk-note">
          Les bibliothèques marquées « système » font tourner Ledroide : elles sont verrouillées et leurs versions
          restent figées lors des installations. Pour qu'un module déclare ses dépendances, utilisez
          <code> dependencies.python</code> dans son <code>manifest.json</code>.
        </p>
      </div>
    </div>
  )
}
