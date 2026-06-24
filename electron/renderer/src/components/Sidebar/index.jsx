import React from 'react'
import './Sidebar.css'

const NAV = [
  { id: 'modules', icon: '⊞', label: 'Modules' },
  { id: 'catalog', icon: '🗂', label: 'Catalogue' },
]

const OP_TITLE = {
  reloading:  'Rechargement…',
  restarting: 'Redémarrage…',
}

export default function Sidebar({ view, op, onNavigate, onHotReload, onRestartBackend }) {
  const busy       = !!op
  const activeNav  = view === 'module' ? 'modules' : view

  return (
    <aside className="sidebar">
      <div className="sb-logo">
        <span className={`sb-logo-dot ${busy ? 'pulse' : ''}`} />
        <span className="sb-logo-text">Ledroide</span>
        {busy && <span className="sb-op-label">{OP_TITLE[op]}</span>}
      </div>

      <nav className="sb-nav">
        {NAV.map(({ id, icon, label }) => (
          <button
            key={id}
            className={`sb-item ${activeNav === id ? 'active' : ''}`}
            onClick={() => onNavigate(id)}
          >
            <span className="sb-item-icon">{icon}</span>
            <span className="sb-item-label">{label}</span>
          </button>
        ))}
      </nav>

      <div className="sb-bottom">
        <div className="sb-sys-row">
          <button
            className="sb-sys-btn"
            onClick={onHotReload}
            disabled={busy}
            title="Recharger les modules"
          >
            ↺
          </button>
          <button
            className="sb-sys-btn warn"
            onClick={onRestartBackend}
            disabled={busy}
            title="Redémarrer le backend Python"
          >
            ⟳
          </button>
        </div>
      </div>
    </aside>
  )
}
