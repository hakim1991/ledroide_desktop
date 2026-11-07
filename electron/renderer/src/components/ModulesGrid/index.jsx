import React, { useMemo } from 'react'
import './ModulesGrid.css'
import ModuleIcon from '../ModuleIcon'
import ModuleFilters, { EMPTY_FILTERS, filterModules } from '../ModuleFilters'

export default function ModulesGrid({ modules, onOpen, filters, onFiltersChange }) {
  const filtered = useMemo(() => filterModules(modules, filters), [modules, filters])

  const groups = useMemo(() => {
    const byCat = new Map()
    filtered.forEach((m) => {
      if (!byCat.has(m.category)) byCat.set(m.category, [])
      byCat.get(m.category).push(m)
    })
    return [...byCat.entries()]
      .sort((a, b) => a[0].localeCompare(b[0], 'fr'))
      .map(([cat, mods]) => [cat, mods.sort((a, b) => a.name.localeCompare(b.name, 'fr'))])
  }, [filtered])

  if (modules.length === 0) {
    return (
      <div className="mg-empty">
        <div className="mg-empty-icon">⊞</div>
        <p className="mg-empty-title">Aucun module actif</p>
        <p className="mg-empty-sub">Ouvrez le Catalogue pour en activer.</p>
      </div>
    )
  }

  function openFirstMatch(e) {
    if (e.key === 'Enter' && filtered.length > 0 && e.target.tagName === 'INPUT') onOpen(filtered[0])
  }

  return (
    <div className="mg-layout">
      <div className="mg-toolbar" onKeyDown={openFirstMatch}>
        <ModuleFilters
          modules={modules}
          filters={filters}
          onChange={onFiltersChange}
          placeholder={`Rechercher parmi ${modules.length} modules… (Entrée pour ouvrir)`}
          autoFocus
        />
      </div>

      {filtered.length === 0 ? (
        <div className="mg-no-match">
          <p>Aucun module ne correspond aux filtres.</p>
          <button className="btn btn-ghost" onClick={() => onFiltersChange(EMPTY_FILTERS)}>Effacer les filtres</button>
        </div>
      ) : (
        <div className="mg-groups">
          {groups.map(([category, mods]) => (
            <section key={category} className="mg-group">
              {groups.length > 1 && <h3 className="mg-group-title">{category}</h3>}
              <div className="mg-grid">
                {mods.map((mod) => (
                  <button key={mod.id} className="mg-tile" onClick={() => onOpen(mod)} title={mod.description}>
                    <div className="mg-tile-icon">
                      <ModuleIcon id={mod.id} fallback={mod.icon} size={56} />
                    </div>
                    <span className="mg-tile-name">{mod.name}</span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}
