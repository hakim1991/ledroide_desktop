import React, { useEffect, useMemo, useState } from 'react'
import './EnvPanel.css'

const api = (path, opts) => window.ledroide.api(path, opts)
const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
const RESERVED = new Set(['PATH', 'PYTHONPATH', 'PYTHONHOME', 'SYSTEMROOT', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP'])

let rowSeq = 0
const toRow = (v) => ({ rid: ++rowSeq, key: '', value: '', secret: false, description: '', ...v })
const strip = ({ rid, ...v }) => v

function keyError(key, rows) {
  if (!key) return 'Nom requis'
  if (!KEY_RE.test(key)) return 'Lettres, chiffres et _ uniquement (pas de chiffre en premier)'
  if (RESERVED.has(key.toUpperCase()) || key.toUpperCase().startsWith('LEDROIDE_')) return 'Variable réservée'
  if (rows.filter((r) => r.key === key).length > 1) return 'Nom en double'
  return null
}

export default function EnvPanel({ modules, onSaved, showToast }) {
  const [rows, setRows]       = useState(null)
  const [saved, setSaved]     = useState([])
  const [saving, setSaving]   = useState(false)
  const [error, setError]     = useState(null)
  const [revealed, setRevealed] = useState(() => new Set())
  const [query, setQuery]     = useState('')

  useEffect(() => {
    api('/system/env')
      .then(({ vars }) => { setSaved(vars); setRows(vars.map(toRow)) })
      .catch((e) => setError(e.message))
  }, [])

  // key -> [{ module, required, description, secret }]
  const declared = useMemo(() => {
    const map = new Map()
    modules.forEach((m) => (m.env ?? []).forEach((e) => {
      if (!map.has(e.key)) map.set(e.key, [])
      map.get(e.key).push({ module: m, ...e })
    }))
    return map
  }, [modules])

  if (rows === null) {
    return (
      <div className="env-layout">
        {error ? <p className="env-error">{error}</p> : <span className="cp-spinner" />}
      </div>
    )
  }

  const dirty = JSON.stringify(rows.map(strip)) !== JSON.stringify(saved)
  const errors = new Map(rows.map((r) => [r.rid, keyError(r.key, rows)]))
  const invalid = [...errors.values()].some(Boolean)
  const definedKeys = new Set(rows.map((r) => r.key))
  const requested = [...declared.entries()].filter(([key]) => !definedKeys.has(key))
  const q = query.trim().toLowerCase()
  const visibleRows = q
    ? rows.filter((r) => r.key.toLowerCase().includes(q) || r.description.toLowerCase().includes(q))
    : rows

  const update = (rid, patch) => setRows((rs) => rs.map((r) => (r.rid === rid ? { ...r, ...patch } : r)))
  const remove = (rid) => setRows((rs) => rs.filter((r) => r.rid !== rid))
  const add = (v = {}) => setRows((rs) => [...rs, toRow(v)])
  const toggleReveal = (rid) => setRevealed((s) => {
    const next = new Set(s)
    next.has(rid) ? next.delete(rid) : next.add(rid)
    return next
  })

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const { vars, changed } = await api('/system/env', { method: 'PUT', body: { vars: rows.map(strip) } })
      setSaved(vars)
      setRows(vars.map(toRow))
      setRevealed(new Set())
      showToast(changed.length ? `${changed.length} variable(s) mise(s) à jour` : 'Aucun changement')
      onSaved?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="env-layout">
      <header className="env-header">
        <div>
          <h2 className="env-title">Variables d'environnement</h2>
          <p className="env-sub">
            Disponibles pour tous les modules via <code>os.environ</code> ou <code>module.env("NOM")</code>,
            ainsi que pour les programmes qu'ils lancent. Appliquées immédiatement, sans redémarrage.
          </p>
        </div>
        <div className="env-actions">
          {dirty && (
            <button className="btn btn-ghost" onClick={() => setRows(saved.map(toRow))} disabled={saving}>
              Annuler
            </button>
          )}
          <button className="btn btn-primary" onClick={save} disabled={!dirty || invalid || saving}>
            {saving ? <span className="cp-spinner" /> : null} Enregistrer
          </button>
        </div>
      </header>

      <div className="env-content">
        {error && <p className="env-error">{error}</p>}

        {requested.length > 0 && (
          <section className="env-requested">
            <h3 className="env-section-title">Demandées par les modules</h3>
            {requested.map(([key, uses]) => (
              <div key={key} className="env-request">
                <code className="env-key-label">{key}</code>
                <span className="env-request-desc">
                  {uses.find((u) => u.description)?.description || 'Aucune description'}
                  <span className="env-request-mods"> · {uses.map((u) => u.module.name).join(', ')}</span>
                </span>
                {uses.some((u) => u.required) && <span className="env-badge-required">requise</span>}
                <button
                  className="btn btn-ghost"
                  onClick={() => add({
                    key,
                    secret: uses.some((u) => u.secret),
                    description: uses.find((u) => u.description)?.description ?? '',
                  })}
                >
                  + Ajouter
                </button>
              </div>
            ))}
          </section>
        )}

        <section>
          <div className="env-list-head">
            <h3 className="env-section-title">Variables définies ({rows.length})</h3>
            {rows.length > 5 && (
              <input
                className="env-filter"
                placeholder="Filtrer…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            )}
          </div>

          {rows.length === 0 ? (
            <p className="env-empty">Aucune variable. Ajoutez-en une pour la rendre disponible aux modules.</p>
          ) : (
            <div className="env-table">
              <div className="env-row env-row-head">
                <span>Nom</span><span>Valeur</span><span>Description</span><span title="Masquer la valeur">Secret</span><span />
              </div>
              {visibleRows.map((r) => {
                const err = errors.get(r.rid)
                const users = declared.get(r.key)
                const hidden = r.secret && !revealed.has(r.rid)
                return (
                  <div key={r.rid} className="env-row">
                    <div className="env-cell">
                      <input
                        className={`env-input env-key ${err ? 'invalid' : ''}`}
                        value={r.key}
                        placeholder="NOM_VARIABLE"
                        spellCheck={false}
                        onChange={(e) => update(r.rid, { key: e.target.value.trim() })}
                        autoFocus={!r.key}
                      />
                      {err && r.key !== '' && <span className="env-field-error">{err}</span>}
                      {users && <span className="env-used-by">Utilisée par {users.map((u) => u.module.name).join(', ')}</span>}
                    </div>
                    <div className="env-cell env-value-cell">
                      <input
                        className="env-input"
                        type={hidden ? 'password' : 'text'}
                        value={r.value}
                        placeholder="valeur"
                        spellCheck={false}
                        autoComplete="off"
                        onChange={(e) => update(r.rid, { value: e.target.value })}
                      />
                      {r.secret && (
                        <button className="env-icon-btn" onClick={() => toggleReveal(r.rid)} title={hidden ? 'Afficher' : 'Masquer'}>
                          {hidden ? '👁' : '🙈'}
                        </button>
                      )}
                    </div>
                    <input
                      className="env-input"
                      value={r.description}
                      placeholder="optionnel"
                      onChange={(e) => update(r.rid, { description: e.target.value })}
                    />
                    <input
                      type="checkbox"
                      className="env-check"
                      checked={r.secret}
                      onChange={(e) => update(r.rid, { secret: e.target.checked })}
                    />
                    <button className="env-icon-btn env-delete" onClick={() => remove(r.rid)} title="Supprimer">✕</button>
                  </div>
                )
              })}
            </div>
          )}

          <button className="btn btn-ghost env-add" onClick={() => add()}>+ Nouvelle variable</button>
          <p className="env-note">
            Les valeurs sont enregistrées en clair dans le dossier de données de l'application (<code>env.json</code>).
            « Secret » masque seulement l'affichage.
          </p>
        </section>
      </div>
    </div>
  )
}
