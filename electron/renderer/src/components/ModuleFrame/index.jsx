import React from 'react'
import './ModuleFrame.css'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765

export default function ModuleFrame({ module, onBack }) {
  const src = `http://127.0.0.1:${BACKEND_PORT}/modules/${module.id}/frontend/`

  return (
    <div className="mf-layout">
      <div className="mf-toolbar">
        <button className="btn btn-ghost mf-back" onClick={onBack}>
          ← Retour
        </button>
        <div className="mf-title-group">
          <span className="mf-name">{module.name}</span>
          <span className="mf-version">v{module.version}</span>
        </div>
        <div className="mf-status active" title="Module actif" />
      </div>

      <iframe
        className="mf-iframe"
        src={src}
        sandbox="allow-scripts allow-same-origin allow-forms"
        title={module.name}
      />
    </div>
  )
}
