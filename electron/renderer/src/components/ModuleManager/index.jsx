import React, { useState, useEffect, useCallback } from 'react'
import './ModuleManager.css'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765
const API = `http://127.0.0.1:${BACKEND_PORT}`

const TABS = [
  { id: 'active',    label: 'Actifs' },
  { id: 'installed', label: 'Installés' },
  { id: 'available', label: 'Disponibles' },
]

export default function ModuleManager({ onOpen }) {
  const [tab, setTab] = useState('active')
  const [modules, setModules] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetchModules = useCallback(() => {
    setLoading(true)
    fetch(`${API}/modules`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data) => { setModules(data); setError(null) })
      .catch((err) => setError(`Backend injoignable sur le port ${BACKEND_PORT} — ${err.message}`))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    fetchModules()
  }, [fetchModules])

  const filtered = modules.filter((m) => m.status === tab)

  const counts = modules.reduce((acc, m) => {
    acc[m.status] = (acc[m.status] || 0) + 1
    return acc
  }, {})

  async function handleActivate(mod) {
    await fetch(`${API}/modules/${mod.id}/activate`, { method: 'POST' })
    fetchModules()
    onOpen(mod)
  }

  async function handleDeactivate(mod) {
    await fetch(`${API}/modules/${mod.id}/deactivate`, { method: 'POST' })
    fetchModules()
  }

  async function handleInstall(mod) {
    setModules((prev) => prev.map((m) => m.id === mod.id ? { ...m, installing: true } : m))
    await fetch(`${API}/modules/${mod.id}/install`, { method: 'POST' })
    fetchModules()
  }

  return (
    <div className="mm-layout">
      <header className="mm-header">
        <div className="mm-logo">
          <span className="mm-logo-dot" />
          <span className="mm-logo-text">LedroideDesktop</span>
        </div>
        <button className="btn btn-ghost mm-refresh" onClick={fetchModules} title="Actualiser">
          ↺
        </button>
      </header>

      <nav className="mm-tabs">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            className={`mm-tab ${tab === id ? 'active' : ''}`}
            onClick={() => setTab(id)}
          >
            {label}
            {counts[id] > 0 && <span className="mm-badge-count">{counts[id]}</span>}
          </button>
        ))}
      </nav>

      <main className="mm-content">
        {error && (
          <div className="mm-error">
            <div>
              <strong>Backend Python injoignable</strong>
              <br />
              <small>{error}</small>
              <br />
              <small style={{ color: '#6b7280', marginTop: '.4rem', display: 'block' }}>
                Vérifiez que le backend tourne : <code>python backend/main.py</code>
              </small>
            </div>
            <button className="btn btn-ghost" onClick={fetchModules} style={{ flexShrink: 0 }}>
              Réessayer
            </button>
          </div>
        )}

        {!error && loading && (
          <p className="mm-empty">Connexion au backend...</p>
        )}

        {!error && !loading && filtered.length === 0 && (
          <p className="mm-empty">
            {tab === 'active'    && 'Aucun module actif.'}
            {tab === 'installed' && 'Aucun module installé.'}
            {tab === 'available' && 'Aucun module disponible dans le catalogue.'}
          </p>
        )}

        <div className="mm-grid">
          {filtered.map((mod) => (
            <ModuleCard
              key={mod.id}
              mod={mod}
              onActivate={handleActivate}
              onDeactivate={handleDeactivate}
              onInstall={handleInstall}
              onOpen={onOpen}
            />
          ))}
        </div>
      </main>
    </div>
  )
}

function ModuleCard({ mod, onActivate, onDeactivate, onInstall, onOpen }) {
  return (
    <div className={`mc ${mod.status}`}>
      <div className="mc-top">
        <span className="mc-name">{mod.name}</span>
        <span className="mc-version">v{mod.version}</span>
      </div>

      <p className="mc-desc">{mod.description}</p>

      <div className="mc-meta">
        {mod.database?.type && mod.database.type !== 'none' && (
          <span className="mc-tag db">{mod.database.type}</span>
        )}
        {mod.permissions?.map((p) => (
          <span key={p} className="mc-tag">{p}</span>
        ))}
      </div>

      <div className="mc-actions">
        {mod.status === 'active' && (
          <>
            <button className="btn btn-primary" onClick={() => onOpen(mod)}>Ouvrir</button>
            <button className="btn btn-ghost" onClick={() => onDeactivate(mod)}>Désactiver</button>
          </>
        )}
        {mod.status === 'installed' && (
          <button className="btn btn-primary" onClick={() => onActivate(mod)}>Activer</button>
        )}
        {mod.status === 'available' && (
          <button className="btn btn-secondary" onClick={() => onInstall(mod)} disabled={mod.installing}>
            {mod.installing ? 'Installation...' : 'Installer'}
          </button>
        )}
      </div>
    </div>
  )
}
