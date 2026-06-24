import React from 'react'
import './ModulesGrid.css'
import ModuleIcon from '../ModuleIcon'

export default function ModulesGrid({ modules, onOpen }) {
  if (modules.length === 0) {
    return (
      <div className="mg-empty">
        <div className="mg-empty-icon">⊞</div>
        <p className="mg-empty-title">Aucun module installé</p>
        <p className="mg-empty-sub">Ouvrez le Catalogue pour en installer.</p>
      </div>
    )
  }

  return (
    <div className="mg-layout">
      <div className="mg-grid">
        {modules.map((mod) => (
          <button key={mod.id} className="mg-tile" onClick={() => onOpen(mod)}>
            <div className="mg-tile-icon">
              <ModuleIcon id={mod.id} fallback={mod.icon} size={56} />
            </div>
            <span className="mg-tile-name">{mod.name}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
