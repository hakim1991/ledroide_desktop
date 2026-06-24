import React from 'react'
import './Sidebar.css'

const OP_TITLE = {
  reloading:  'Rechargement…',
  restarting: 'Redémarrage…',
}

export default function Sidebar({
  modules,
  activeModuleId,
  catalogActive,
  op,
  onSelectModule,
  onOpenCatalog,
  onHotReload,
  onRestartBackend,
}) {
  const busy = !!op

  return (
    <aside className="sidebar">
      <div className="sb-logo">
        <span className={`sb-logo-dot ${busy ? 'pulse' : ''}`} />
        <span className="sb-logo-text">Ledroide</span>
        {busy && <span className="sb-op-label">{OP_TITLE[op]}</span>}
      </div>

      <nav className="sb-nav">
        <span className="sb-section-label">Modules</span>

        {modules.length === 0 && (
          <p className="sb-empty">Aucun module installé</p>
        )}

        {modules.map((mod) => (
          <button
            key={mod.id}
            className={`sb-item ${activeModuleId === mod.id ? 'active' : ''}`}
            onClick={() => onSelectModule(mod)}
            title={mod.name}
          >
            <span className="sb-item-icon">{mod.icon ?? '⬡'}</span>
            <span className="sb-item-name">{mod.name}</span>
          </button>
        ))}
      </nav>

      <div className="sb-bottom">
        <button
          className={`sb-catalog-btn ${catalogActive ? 'active' : ''}`}
          onClick={onOpenCatalog}
        >
          <span>🗂</span>
          <span>Catalogue</span>
        </button>

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
