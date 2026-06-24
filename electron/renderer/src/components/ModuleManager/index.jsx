import React, { useState } from 'react'
import './ModuleManager.css'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765
const API = `http://127.0.0.1:${BACKEND_PORT}`

const TABS = [
  { id: 'installed', label: 'Installés' },
  { id: 'available', label: 'Disponibles' },
]

export default function CatalogPanel({ modules, onRefresh, onClose, onModuleDeactivated, showToast }) {
  const [tab, setTab] = useState('installed')

  const filtered = modules.filter((m) =>
    tab === 'installed'
      ? m.status === 'installed' || m.status === 'active'
      : m.status === 'available',
  )

  const counts = {
    installed: modules.filter((m) => m.status === 'installed' || m.status === 'active').length,
    available: modules.filter((m) => m.status === 'available').length,
  }

  async function handleInstall(mod) {
    await fetch(`${API}/modules/${mod.id}/install`, { method: 'POST' })
    await onRefresh()
    showToast(`${mod.name} installé`)
  }

  async function handleDeactivate(mod) {
    await fetch(`${API}/modules/${mod.id}/deactivate`, { method: 'POST' })
    onModuleDeactivated(mod.id)
    await onRefresh()
    showToast(`${mod.name} désactivé`)
  }

  async function handleUninstall(mod) {
    if (mod.status === 'active') {
      await fetch(`${API}/modules/${mod.id}/deactivate`, { method: 'POST' })
      onModuleDeactivated(mod.id)
    }
    await fetch(`${API}/modules/${mod.id}`, { method: 'DELETE' })
    await onRefresh()
    showToast(`${mod.name} désinstallé`)
  }

  return (
    <div className="cp-layout">
      <header className="cp-header">
        <h2 className="cp-title">Catalogue</h2>
        <button className="btn btn-ghost cp-close" onClick={onClose}>✕ Fermer</button>
      </header>

      <nav className="cp-tabs">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            className={`cp-tab ${tab === id ? 'active' : ''}`}
            onClick={() => setTab(id)}
          >
            {label}
            {counts[id] > 0 && <span className="cp-badge">{counts[id]}</span>}
          </button>
        ))}
      </nav>

      <div className="cp-content">
        {filtered.length === 0 && (
          <p className="cp-empty">
            {tab === 'installed'
              ? 'Aucun module installé.'
              : 'Aucun module disponible dans le catalogue.'}
          </p>
        )}

        <div className="cp-grid">
          {filtered.map((mod) => (
            <CatalogCard
              key={mod.id}
              mod={mod}
              onInstall={handleInstall}
              onDeactivate={handleDeactivate}
              onUninstall={handleUninstall}
              showToast={showToast}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function CatalogCard({ mod, onInstall, onDeactivate, onUninstall, showToast }) {
  const [depsState, setDepsState] = useState(null) // null | 'installing' | 'ok' | 'error'
  const pythonDeps = mod.dependencies?.python ?? []

  async function handleInstallDeps() {
    setDepsState('installing')
    try {
      const r = await fetch(`${API}/modules/${mod.id}/deps`, { method: 'POST' })
      const d = await r.json()
      setDepsState(d.ok ? 'ok' : 'error')
      setTimeout(() => setDepsState(null), 4000)
    } catch {
      setDepsState('error')
      setTimeout(() => setDepsState(null), 4000)
    }
  }

  return (
    <div className={`cc ${mod.status}`}>
      <div className="cc-top">
        <span className="cc-icon">{mod.icon ?? '⬡'}</span>
        <div className="cc-info">
          <span className="cc-name">{mod.name}</span>
          <span className="cc-version">v{mod.version}</span>
        </div>
        {mod.status === 'active' && <span className="cc-badge-active">Actif</span>}
      </div>

      <p className="cc-desc">{mod.description}</p>

      <div className="cc-meta">
        {mod.database?.type && mod.database.type !== 'none' && (
          <span className="cc-tag db">{mod.database.type}</span>
        )}
        {mod.permissions?.map((p) => (
          <span key={p} className="cc-tag">{p}</span>
        ))}
        {pythonDeps.length > 0 && (
          <span className="cc-tag" title={pythonDeps.join(', ')}>
            📦 {pythonDeps.length} dép.
          </span>
        )}
      </div>

      <div className="cc-actions">
        {mod.status === 'available' && (
          <button className="btn btn-secondary" onClick={() => onInstall(mod)}>
            Installer
          </button>
        )}

        {(mod.status === 'installed' || mod.status === 'active') && (
          <>
            {mod.status === 'active' && (
              <button className="btn btn-ghost" onClick={() => onDeactivate(mod)}>
                Désactiver
              </button>
            )}
            {pythonDeps.length > 0 && (
              <button
                className="btn btn-ghost"
                onClick={handleInstallDeps}
                disabled={depsState === 'installing'}
                title={`Installer : ${pythonDeps.join(', ')}`}
              >
                {depsState === 'installing' && '⏳ Dépendances…'}
                {depsState === 'ok'         && '✓ Installées'}
                {depsState === 'error'      && '✕ Erreur'}
                {depsState === null         && '📦 Dépendances'}
              </button>
            )}
            <button className="btn btn-ghost cc-uninstall" onClick={() => onUninstall(mod)}>
              Désinstaller
            </button>
          </>
        )}
      </div>
    </div>
  )
}
