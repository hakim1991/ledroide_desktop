import React from 'react'
import './Sidebar.css'

const NAV = [
  { id: 'modules', icon: '⊞', label: 'Modules' },
  { id: 'catalog', icon: '🗂', label: 'Catalogue' },
]


export default function Sidebar({ view, onNavigate }) {
  const activeNav = view === 'module' ? 'modules' : view

  return (
    <aside className="sidebar">
      <div className="sb-logo">
        <span className="sb-logo-dot" />
        <span className="sb-logo-text">Ledroide</span>
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

    </aside>
  )
}
