import React, { useMemo, useState } from 'react'
import './ModuleFilters.css'

export const EMPTY_FILTERS = { query: '', category: null, tags: [] }
const VISIBLE_TAGS = 12

const norm = (s) => (s ?? '').toString().normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()

function matchesQuery(mod, query) {
  if (!query) return true
  const haystack = norm([mod.name, mod.id, mod.description, mod.category, ...(mod.tags ?? [])].join(' '))
  return norm(query).split(/\s+/).filter(Boolean).every((word) => haystack.includes(word))
}

export function filterModules(modules, { query, category, tags }) {
  return modules.filter((m) =>
    matchesQuery(m, query.trim())
    && (!category || m.category === category)
    && tags.every((t) => m.tags?.includes(t)))
}

export const hasActiveFilters = (f) => !!(f.query.trim() || f.category || f.tags.length)

function countBy(items) {
  const counts = new Map()
  items.forEach((k) => counts.set(k, (counts.get(k) ?? 0) + 1))
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'fr'))
}

export function toggleTag(filters, tag) {
  const tags = filters.tags.includes(tag) ? filters.tags.filter((t) => t !== tag) : [...filters.tags, tag]
  return { ...filters, tags }
}

export default function ModuleFilters({ modules, filters, onChange, placeholder = 'Rechercher un module…', autoFocus = false }) {
  const [showAllTags, setShowAllTags] = useState(false)
  const set = (patch) => onChange({ ...filters, ...patch })

  const categories = useMemo(
    () => countBy(modules.filter((m) => matchesQuery(m, filters.query.trim())).map((m) => m.category))
      .sort((a, b) => a[0].localeCompare(b[0], 'fr')),
    [modules, filters.query],
  )

  // Tag counts reflect the modules still reachable with the current search + category.
  const tags = useMemo(() => {
    const scope = filterModules(modules, { ...filters, tags: [] })
    const counted = countBy(scope.flatMap((m) => m.tags ?? []))
    const selectedMissing = filters.tags.filter((t) => !counted.some(([k]) => k === t)).map((t) => [t, 0])
    return [...selectedMissing, ...counted]
  }, [modules, filters])

  const visibleTags = showAllTags
    ? tags
    : tags.filter(([t], i) => i < VISIBLE_TAGS || filters.tags.includes(t))
  const hiddenCount = tags.length - visibleTags.length

  return (
    <div className="flt">
      <div className="flt-search">
        <span className="flt-search-icon">⌕</span>
        <input
          className="flt-search-input"
          type="text"
          placeholder={placeholder}
          value={filters.query}
          onChange={(e) => set({ query: e.target.value })}
          onKeyDown={(e) => e.key === 'Escape' && set({ query: '' })}
          autoFocus={autoFocus}
        />
        {hasActiveFilters(filters) && (
          <button className="flt-clear" onClick={() => onChange(EMPTY_FILTERS)} title="Effacer les filtres">
            Effacer
          </button>
        )}
      </div>

      {categories.length > 1 && (
        <div className="flt-row" role="group" aria-label="Catégories">
          <button
            className={`flt-chip ${!filters.category ? 'active' : ''}`}
            onClick={() => set({ category: null })}
          >
            Toutes
          </button>
          {categories.map(([cat, n]) => (
            <button
              key={cat}
              className={`flt-chip ${filters.category === cat ? 'active' : ''}`}
              onClick={() => set({ category: filters.category === cat ? null : cat })}
            >
              {cat}<span className="flt-count">{n}</span>
            </button>
          ))}
        </div>
      )}

      {tags.length > 0 && (
        <div className="flt-row flt-tags" role="group" aria-label="Tags">
          {visibleTags.map(([tag, n]) => (
            <button
              key={tag}
              className={`flt-tag ${filters.tags.includes(tag) ? 'active' : ''}`}
              onClick={() => onChange(toggleTag(filters, tag))}
            >
              #{tag}<span className="flt-count">{n}</span>
            </button>
          ))}
          {(hiddenCount > 0 || showAllTags) && tags.length > VISIBLE_TAGS && (
            <button className="flt-more" onClick={() => setShowAllTags((v) => !v)}>
              {showAllTags ? 'Moins' : `+${hiddenCount}`}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
