import React, { useState } from 'react'
import { createPortal } from 'react-dom'
import './ModuleManager.css'
import ModuleIcon from '../ModuleIcon'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765
const API = `http://127.0.0.1:${BACKEND_PORT}`

const TABS = [
  { id: 'installed', label: 'Installés' },
  { id: 'available', label: 'Disponibles' },
]

export default function CatalogPanel({ modules, onRefresh, onClose, onModuleDeactivated, showToast, onHotReload, onRestartBackend, op }) {
  const busy = !!op
  const [tab, setTab]     = useState('installed')
  const [search, setSearch] = useState('')
  const [modal, setModal]   = useState(null)
  // modal: null | { mod, type: 'activate'|'uninstall', loading: false, loadingMsg: '' }

  const query    = search.trim().toLowerCase()
  const filtered = modules.filter((m) => {
    const matchTab = tab === 'installed'
      ? m.status === 'installed' || m.status === 'active'
      : m.status === 'available'
    const matchSearch = !query
      || m.name.toLowerCase().includes(query)
      || m.description?.toLowerCase().includes(query)
    return matchTab && matchSearch
  })

  const counts = {
    installed: modules.filter((m) => m.status === 'installed' || m.status === 'active').length,
    available: modules.filter((m) => m.status === 'available').length,
  }

  // ── Action handlers ───────────────────────────────────────────────────────

  async function handleActivate(mod) {
    const pythonDeps = mod.dependencies?.python ?? []

    if (pythonDeps.length > 0) {
      try {
        const r = await fetch(`${API}/modules/${mod.id}/deps`, { method: 'POST' })
        const d = await r.json()
        if (!d.ok) {
          showToast('Échec de l\'installation des dépendances', false)
          return
        }
      } catch {
        showToast('Erreur lors de l\'installation des dépendances', false)
        return
      }
    }

    await fetch(`${API}/modules/${mod.id}/activate`, { method: 'POST' })

    if (pythonDeps.length > 0) {
      await onRestartBackend()
    } else {
      await onRefresh()
      showToast(`${mod.name} activé`)
    }
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

  // ── Modal ─────────────────────────────────────────────────────────────────

  function openModal(mod, type) {
    setModal({ mod, type, loading: false, loadingMsg: '' })
  }

  async function confirmModal() {
    const { mod, type } = modal
    const pythonDeps = mod.dependencies?.python ?? []

    const loadingMsg = type === 'activate'
      ? (pythonDeps.length > 0 ? 'Installation des dépendances…' : 'Activation…')
      : 'Désactivation…'

    setModal(m => ({ ...m, loading: true, loadingMsg }))

    try {
      if (type === 'activate')    await handleActivate(mod)
      if (type === 'deactivate')  await handleDeactivate(mod)
    } finally {
      setModal(null)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="cp-layout">

      <header className="cp-header">
        <h2 className="cp-title">Catalogue</h2>
        <div className="cp-header-actions">
          <button
            className="btn btn-ghost cp-sys-btn"
            onClick={onHotReload}
            disabled={busy}
            title="Recharger les modules sans redémarrer le backend"
          >
            {op === 'reloading' ? <span className="cp-spinner" /> : '↺'} Recharger
          </button>
          <button
            className="btn btn-ghost cp-sys-btn cp-sys-restart"
            onClick={onRestartBackend}
            disabled={busy}
            title="Redémarrer le process Python"
          >
            {op === 'restarting' ? <span className="cp-spinner" /> : '⟳'} Redémarrer
          </button>
          <button className="btn btn-ghost cp-close" onClick={onClose}>✕</button>
        </div>
      </header>

      <div className="cp-search-bar">
        <span className="cp-search-icon">⌕</span>
        <input
          className="cp-search-input"
          type="text"
          placeholder="Rechercher un module…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          autoFocus
        />
        {search && (
          <button className="cp-search-clear" onClick={() => setSearch('')}>✕</button>
        )}
      </div>

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
              onActivate={(m) => openModal(m, 'activate')}
              onInstall={handleInstall}
              onDeactivate={(m) => openModal(m, 'deactivate')}
              onUninstall={handleUninstall}
            />
          ))}
        </div>
      </div>

      {modal && (
        <ConfirmModal
          modal={modal}
          onConfirm={confirmModal}
          onCancel={() => !modal.loading && setModal(null)}
        />
      )}
    </div>
  )
}

// ── Confirmation modal ────────────────────────────────────────────────────────

function ConfirmModal({ modal, onConfirm, onCancel }) {
  const { mod, type, loading, loadingMsg } = modal
  const pythonDeps   = mod.dependencies?.python ?? []
  const hasDeps      = pythonDeps.length > 0
  const isDeactivate = type === 'deactivate'

  const title = isDeactivate ? 'Désactiver' : 'Activer'
  const confirmLabel = isDeactivate
    ? 'Désactiver'
    : hasDeps ? 'Installer et activer' : 'Activer'

  return createPortal(
    <div
      className="cc-modal-backdrop"
      onClick={loading ? undefined : onCancel}
    >
      <div className="cc-modal" onClick={(e) => e.stopPropagation()}>

        <div className="cc-modal-head">
          <ModuleIcon id={mod.id} fallback={mod.icon} size={36} className="cc-modal-icon" />
          <div>
            <p className="cc-modal-title">{title} {mod.name}</p>
            <p className="cc-modal-sub">v{mod.version}</p>
          </div>
        </div>

        <div className="cc-modal-body">
          {loading ? (
            <div className="cc-modal-progress">
              <span className="cp-spinner cc-modal-spinner" />
              <span className="cc-modal-progress-msg">{loadingMsg}</span>
            </div>
          ) : isDeactivate ? (
            <p className="cc-modal-text">
              Désactiver le module <strong>{mod.name}</strong> ?
            </p>
          ) : hasDeps ? (
            <>
              <p className="cc-modal-text">
                Les dépendances suivantes seront installées :
              </p>
              <ul className="cc-modal-deps">
                {pythonDeps.map((d) => <li key={d}>{d}</li>)}
              </ul>
              <p className="cc-modal-warn">
                Le backend Python sera redémarré après l'installation.
              </p>
            </>
          ) : (
            <p className="cc-modal-text">
              Activer le module <strong>{mod.name}</strong> ?
            </p>
          )}
        </div>

        {!loading && (
          <div className="cc-modal-footer">
            <button className="btn btn-ghost" onClick={onCancel}>Annuler</button>
            <button className="btn btn-primary" onClick={onConfirm} autoFocus>
              {confirmLabel}
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}

// ── Catalog card ──────────────────────────────────────────────────────────────

function CatalogCard({ mod, onActivate, onInstall, onDeactivate, onUninstall }) {
  const pythonDeps = mod.dependencies?.python ?? []

  return (
    <div className={`cc ${mod.status}`}>
      <div className="cc-top">
        <ModuleIcon id={mod.id} fallback={mod.icon} size={40} className="cc-icon" />
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
            {mod.status === 'installed' && (
              <button className="btn btn-primary" onClick={() => onActivate(mod)}>
                Activer
              </button>
            )}
            {mod.status === 'active' && (
              <button className="btn btn-ghost" onClick={() => onDeactivate(mod)}>
                Désactiver
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
