import React, { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import './ModuleManager.css'
import ModuleIcon from '../ModuleIcon'
import ModuleFilters, { EMPTY_FILTERS, filterModules, toggleTag } from '../ModuleFilters'

const api = (path, opts) => window.ledroide.api(path, opts)
const SYSTEM = '__system__'

const TABS = [
  { id: 'installed', label: 'Installés' },
  { id: 'available', label: 'Disponibles' },
]

function timeAgo(ts) {
  if (!ts) return 'jamais'
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return "à l'instant"
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`
  return new Date(ts * 1000).toLocaleDateString('fr-FR')
}

const isInstalled = (m) => m.status === 'installed' || m.status === 'active' || m.status === 'error'

const STATUS_FILTERS = [
  { id: 'all',      label: 'Tous',        test: () => true },
  { id: 'active',   label: 'Activés',     test: (m) => m.status === 'active' },
  { id: 'inactive', label: 'Non activés', test: (m) => m.status === 'installed' },
  { id: 'error',    label: 'En erreur',   test: (m) => m.status === 'error' },
  { id: 'updates',  label: 'Mises à jour', test: (m) => !!m.update_available },
]
const CONDITIONAL_FILTERS = new Set(['error', 'updates'])

const depsOf = (m) => m.dependencies?.python ?? []
const sameDeps = (a, b) => [...a].sort().join('\n') === [...b].sort().join('\n')

export default function CatalogPanel({ modules, onRefresh, onModuleDeactivated, showToast, onHotReload, onRestartBackend, onOpenEnv, op }) {
  const busy = !!op
  const [tab, setTab]         = useState('installed')
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const [statusFilter, setStatusFilter] = useState('all')
  const [catalog, setCatalog]   = useState(null)
  const [syncing, setSyncing]   = useState(false)
  const [modal, setModal]     = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [batch, setBatch]       = useState(null)      // null | { index, total, kind: 'install'|'update' }
  const [repairing, setRepairing] = useState(false)
  // modal: null | { mod, type: 'activate'|'deactivate'|'uninstall'|'config'|'meta'|'update'|'deps', loading, loadingMsg, log: [] }

  const inTab      = modules.filter((m) => (tab === 'installed' ? isInstalled(m) : m.status === 'available'))
  const matching   = filterModules(inTab, filters)
  const statusCounts = Object.fromEntries(STATUS_FILTERS.map((s) => [s.id, matching.filter(s.test).length]))
  const statusTest = tab === 'installed' ? STATUS_FILTERS.find((s) => s.id === statusFilter).test : () => true
  const filtered   = matching.filter(statusTest).sort((a, b) => a.name.localeCompare(b.name, 'fr'))
  const categories = [...new Set(modules.map((m) => m.category))].sort((a, b) => a.localeCompare(b, 'fr'))

  const selectable  = tab === 'available'
  const filteredIds = filtered.map((m) => m.id)
  const nSelFilter  = filteredIds.filter((id) => selected.has(id)).length
  const allSelected = filteredIds.length > 0 && nSelFilter === filteredIds.length
  const updatable   = modules.filter((m) => isInstalled(m) && m.update_available)

  useEffect(() => {
    api('/modules/catalog').then(setCatalog).catch(() => {})
    return window.ledroide.on(SYSTEM, 'modules:changed', (d) => {
      if (d?.catalog) setCatalog(d.catalog)
      onRefresh()
    })
  }, [])

  async function syncCatalog() {
    setSyncing(true)
    try {
      const info = await api('/modules/catalog/refresh', { method: 'POST' })
      setCatalog(info)
      await onRefresh()
      if (info.error) showToast(info.error, false)
      else {
        const mods = await api('/modules').catch(() => [])
        const nUpd = mods.filter((m) => isInstalled(m) && m.update_available).length
        const parts = []
        if (info.new_modules?.length) parts.push(`${info.new_modules.length} nouveau(x) module(s) disponible(s)`)
        if (nUpd) parts.push(`${nUpd} mise${nUpd > 1 ? 's' : ''} à jour disponible${nUpd > 1 ? 's' : ''}`)
        showToast(parts.length ? parts.join(' · ') : 'Catalogue à jour')
      }
    } catch (err) {
      showToast(err.message, false)
    } finally {
      setSyncing(false)
    }
  }

  useEffect(() => {
    const available = new Set(modules.filter((m) => m.status === 'available').map((m) => m.id))
    setSelected((s) => {
      const next = new Set([...s].filter((id) => available.has(id)))
      return next.size === s.size ? s : next
    })
  }, [modules])

  function toggleSelected(id) {
    setSelected((s) => {
      const next = new Set(s)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  function toggleAll() {
    setSelected((s) => {
      const next = new Set(s)
      filteredIds.forEach((id) => (allSelected ? next.delete(id) : next.add(id)))
      return next
    })
  }

  const counts = {
    installed: modules.filter(isInstalled).length,
    available: modules.filter((m) => m.status === 'available').length,
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  async function activate(mod) {
    const pythonDeps = mod.dependencies?.python ?? []
    if (pythonDeps.length > 0) {
      const d = await api(`/modules/${mod.id}/deps`, { method: 'POST' })
      if (!d.ok) throw Object.assign(new Error('Échec de l\'installation des dépendances'), { output: d.output })
    }
    await api(`/modules/${mod.id}/activate`, { method: 'POST' })
    if (pythonDeps.length > 0) await onRestartBackend()
    else await onRefresh()
    showToast(`${mod.name} activé`)
  }

  async function deactivate(mod) {
    await api(`/modules/${mod.id}/deactivate`, { method: 'POST' })
    onModuleDeactivated(mod.id)
    await onRefresh()
    showToast(`${mod.name} désactivé`)
  }

  async function uninstall(mod) {
    onModuleDeactivated(mod.id)
    await api(`/modules/${mod.id}`, { method: 'DELETE' })
    await onRefresh()
    showToast(`${mod.name} désinstallé`)
  }

  async function install(mod) {
    try {
      await api(`/modules/${mod.id}/install`, { method: 'POST' })
      await onRefresh()
      showToast(`${mod.name} installé`)
    } catch (err) {
      showToast(err.message, false)
    }
  }

  // Returns true when the backend must restart (new dependencies loaded by an active module).
  async function update(mod) {
    if (mod.status === 'active') onModuleDeactivated(mod.id)
    const r = await api(`/modules/${mod.id}/update`, { method: 'POST' })
    if (r.status === 'error') throw new Error(`Mis à jour en v${r.version}, mais la réactivation a échoué : ${r.error}`)
    return r
  }

  async function updateOne(mod) {
    const r = await update(mod)
    if (r.restart) await onRestartBackend()
    else await onRefresh()
    showToast(`${mod.name} mis à jour : v${r.previous_version} → v${r.version}`)
  }

  async function updateAll() {
    const list = [...updatable]
    if (!list.length) return
    const failed = []
    let restart = false
    try {
      for (const [index, mod] of list.entries()) {
        setBatch({ index, total: list.length, kind: 'update' })
        try {
          restart = (await update(mod)).restart || restart
        } catch (err) {
          failed.push(`${mod.name} (${err.message})`)
        }
      }
    } finally {
      setBatch(null)
    }
    if (restart) await onRestartBackend()
    else await onRefresh()
    const ok = list.length - failed.length
    if (!failed.length) showToast(`${ok} module${ok > 1 ? 's' : ''} mis à jour`)
    else showToast(`${ok} mis à jour, ${failed.length} en échec : ${failed.join(', ')}`, false)
  }

  async function repairAll() {
    setRepairing(true)
    try {
      const r = await api('/modules/repair', { method: 'POST' })
      if (r.restart) await onRestartBackend()
      else await onRefresh()
      if (r.ok) showToast(r.activated.length ? `${r.activated.length} module(s) réparé(s) et réactivé(s)` : 'Modules vérifiés : tout est en ordre')
      else if (!r.pip_ok) showToast('Échec de l’installation des bibliothèques — voir le bandeau', false)
      else showToast(`${r.failed.length} module(s) toujours en erreur : ${r.failed.map((f) => f.name).join(', ')}`, false)
    } catch (err) {
      showToast(err.message, false)
    } finally {
      setRepairing(false)
    }
  }

  async function reinstallDeps(mod) {
    const d = await api(`/modules/${mod.id}/deps?reinstall=true`, { method: 'POST' })
    if (!d.ok) throw Object.assign(new Error('Échec de la réinstallation des dépendances'), { output: d.output })
    if (mod.status === 'active') await onRestartBackend()
    else await onRefresh()
    showToast(`Dépendances de ${mod.name} réinstallées`)
  }

  async function installSelected() {
    const ids = [...selected]
    const names = Object.fromEntries(modules.map((m) => [m.id, m.name]))
    setBatch({ index: 0, total: ids.length, kind: 'install' })
    const off = window.ledroide.on(SYSTEM, 'modules:install', (d) => setBatch({ index: d.index, total: d.total, kind: 'install' }))
    try {
      const d = await api('/modules/install', { method: 'POST', body: { ids } })
      const failed = d.results.filter((r) => r.status === 'error')
      await onRefresh()
      setSelected(new Set(failed.map((r) => r.id)))
      if (failed.length === 0) {
        showToast(`${d.installed} module${d.installed > 1 ? 's' : ''} installé${d.installed > 1 ? 's' : ''}`)
      } else {
        const detail = failed.map((r) => `${names[r.id] ?? r.id} (${r.error})`).join(', ')
        showToast(`${d.installed} installé(s), ${failed.length} en échec : ${detail}`, false)
      }
    } catch (err) {
      await onRefresh()
      showToast(err.message, false)
    } finally {
      off?.()
      setBatch(null)
    }
  }

  // ── Modal ─────────────────────────────────────────────────────────────────

  async function confirmModal(extra) {
    const { mod, type } = modal
    const hasDeps = depsOf(mod).length > 0
    const loadingMsg = {
      activate: hasDeps ? 'Installation des dépendances…' : 'Activation…',
      update: 'Mise à jour…',
      deps: 'Réinstallation des dépendances…',
      deactivate: 'Désactivation…',
      uninstall: 'Désinstallation…',
      config: 'Enregistrement…',
      meta: 'Enregistrement…',
    }[type]

    setModal((m) => ({ ...m, loading: true, loadingMsg, log: [] }))
    try {
      if (type === 'activate')   await activate(mod)
      if (type === 'deactivate') await deactivate(mod)
      if (type === 'uninstall')  await uninstall(mod)
      if (type === 'update')     await updateOne(mod)
      if (type === 'deps')       await reinstallDeps(mod)
      if (type === 'config') {
        await api(`/modules/${mod.id}/config`, { method: 'PUT', body: extra })
        showToast('Configuration enregistrée')
      }
      if (type === 'meta') {
        await api(`/modules/${mod.id}/meta`, { method: 'PUT', body: extra })
        await onRefresh()
        showToast('Catégorie et tags enregistrés')
      }
      setModal(null)
    } catch (err) {
      await onRefresh()
      setModal((m) => m && ({
        ...m,
        loading: false,
        error: err.message,
        log: err.output ? err.output.split('\n').slice(-30) : m.log,
      }))
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
            className="btn btn-ghost cp-sys-btn"
            onClick={repairAll}
            disabled={busy || repairing || !!batch}
            title="Installer les bibliothèques manquantes des modules activés et réactiver ceux en erreur"
          >
            {repairing ? <span className="cp-spinner" /> : '🛠'} Réparer
          </button>
          <button
            className="btn btn-ghost cp-sys-btn cp-sys-restart"
            onClick={onRestartBackend}
            disabled={busy}
            title="Redémarrer le process Python"
          >
            {op === 'restarting' ? <span className="cp-spinner" /> : '⟳'} Redémarrer
          </button>
        </div>
      </header>

      <div className="cp-filters">
        <ModuleFilters modules={inTab} filters={filters} onChange={setFilters} autoFocus />
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
        {tab === 'installed' && (
          <div className="cp-status" role="group" aria-label="Filtrer par état">
            {STATUS_FILTERS.filter((s) => !CONDITIONAL_FILTERS.has(s.id) || statusCounts[s.id] > 0 || statusFilter === s.id).map((s) => (
              <button
                key={s.id}
                className={`cp-status-btn st-${s.id}${statusFilter === s.id ? ' active' : ''}`}
                onClick={() => setStatusFilter(s.id)}
                aria-pressed={statusFilter === s.id}
              >
                {s.label}
                <span className="cp-status-n">{statusCounts[s.id]}</span>
              </button>
            ))}
          </div>
        )}
      </nav>

      <div className="cp-content">
        {tab === 'installed' && updatable.length > 0 && (
          <div className="cp-repo cp-updates">
            <span className="cp-repo-icon">↑</span>
            <span className="cp-repo-text">
              <span className="cp-repo-updates">
                {updatable.length} mise{updatable.length > 1 ? 's' : ''} à jour disponible{updatable.length > 1 ? 's' : ''}
              </span>
              {' · '}{updatable.map((m) => `${m.name} v${m.remote_version}`).join(', ')}
            </span>
            <button className="btn btn-primary" onClick={updateAll} disabled={!!batch || busy} title="Mettre à jour tous les modules qui ont une nouvelle version">
              {batch?.kind === 'update'
                ? <><span className="cp-spinner" /> Mise à jour {Math.min(batch.index + 1, batch.total)}/{batch.total}…</>
                : `Tout mettre à jour (${updatable.length})`}
            </button>
          </div>
        )}
        {tab === 'available' && catalog?.enabled && (
          <div className={`cp-repo ${catalog.error ? 'err' : ''}`}>
            <span className="cp-repo-icon">⎇</span>
            <span className="cp-repo-text">
              Dépôt <b>{catalog.repo}</b> <span className="cp-repo-branch">{catalog.branch}</span>
              {' · '}
              {catalog.error
                ? <span className="cp-repo-error">{catalog.error}</span>
                : <>{catalog.count} module{catalog.count > 1 ? 's' : ''} · mis à jour {timeAgo(catalog.updated_at)}</>}
            </span>
            {catalog.error && !catalog.has_token && onOpenEnv && (
              <button className="btn btn-ghost" onClick={onOpenEnv} title="Ajouter GITHUB_TOKEN">Variables…</button>
            )}
            <button className="btn btn-ghost" onClick={syncCatalog} disabled={syncing || busy} title="Télécharger la dernière version du dépôt">
              {syncing ? <span className="cp-spinner" /> : '↻'} Actualiser
            </button>
          </div>
        )}
        {selectable && filtered.length > 0 && (
          <div className="cp-bulk">
            <label className="cp-bulk-all">
              <input
                type="checkbox"
                checked={allSelected}
                ref={(el) => el && (el.indeterminate = nSelFilter > 0 && !allSelected)}
                onChange={toggleAll}
                disabled={!!batch}
              />
              Tout sélectionner
            </label>
            <span className="cp-bulk-count">
              {selected.size > 0 ? `${selected.size} sélectionné${selected.size > 1 ? 's' : ''}` : 'Cochez les modules à installer'}
            </span>
            {selected.size > 0 && !batch && (
              <button className="btn btn-ghost cp-bulk-clear" onClick={() => setSelected(new Set())}>Effacer</button>
            )}
            <button
              className="btn btn-primary cp-bulk-install"
              onClick={installSelected}
              disabled={selected.size === 0 || !!batch || busy}
            >
              {batch
                ? <><span className="cp-spinner" /> Installation {Math.min(batch.index + 1, batch.total)}/{batch.total}…</>
                : `Installer la sélection${selected.size > 0 ? ` (${selected.size})` : ''}`}
            </button>
          </div>
        )}
        {filtered.length === 0 && (
          <p className="cp-empty">
            {inTab.length > 0
              ? (matching.length > 0 && statusFilter !== 'all'
                  ? (statusFilter === 'updates' ? 'Tous les modules sont à jour.' : `Aucun module ${{ active: 'activé', inactive: 'non activé', error: 'en erreur' }[statusFilter]}.`)
                  : 'Aucun module ne correspond aux filtres.')
              : tab === 'installed' ? 'Aucun module installé.' : (catalog?.enabled && !catalog.error && catalog.count > 0 ? 'Tous les modules du dépôt sont déjà installés.' : 'Aucun module disponible dans le catalogue.')}
          </p>
        )}
        <div className="cp-grid">
          {filtered.map((mod) => (
            <CatalogCard
              key={mod.id}
              mod={mod}
              onAction={(type) => setModal({ mod, type, loading: false, loadingMsg: '', log: [] })}
              onInstall={install}
              installing={!!batch}
              busy={!!batch || busy}
              selected={selectable && selected.has(mod.id)}
              onToggle={selectable && !batch ? () => toggleSelected(mod.id) : null}
              activeTags={filters.tags}
              onTagClick={(tag) => setFilters((f) => toggleTag(f, tag))}
              onCategoryClick={(category) => setFilters((f) => ({ ...f, category }))}
              onOpenEnv={onOpenEnv}
            />
          ))}
        </div>
      </div>

      {modal && (
        <ConfirmModal
          modal={modal}
          setModal={setModal}
          categories={categories}
          onConfirm={confirmModal}
          onCancel={() => !modal.loading && setModal(null)}
        />
      )}
    </div>
  )
}

// ── Modal ─────────────────────────────────────────────────────────────────────

const TITLES = {
  activate: 'Activer', deactivate: 'Désactiver', uninstall: 'Désinstaller', config: 'Configurer', meta: 'Classer',
  update: 'Mettre à jour', deps: 'Dépendances de',
}

function ConfirmModal({ modal, setModal, onConfirm, onCancel, categories }) {
  const { mod, type, loading, loadingMsg, error, log } = modal
  const pythonDeps = depsOf(mod)
  const hasDeps    = pythonDeps.length > 0
  const logRef     = useRef(null)
  const [remoteDeps, setRemoteDeps] = useState(null)
  const [config, setConfig] = useState(null)
  const [meta, setMeta]     = useState(() => ({ category: mod.category ?? '', tags: (mod.tags ?? []).join(', ') }))

  useEffect(() => {
    if (type !== 'config') return
    api(`/modules/${mod.id}/config`).then(setConfig).catch((e) => setModal((m) => ({ ...m, error: e.message })))
  }, [type, mod.id, setModal])

  useEffect(() => {
    if (type !== 'update') return
    api('/modules').then((mods) => setRemoteDeps(mods.find((m) => m.id === mod.id)?.remote_dependencies ?? null)).catch(() => {})
  }, [type, mod.id])

  useEffect(() => {
    if (!loading || !(type === 'deps' || type === 'update' || (type === 'activate' && hasDeps))) return
    return window.ledroide.on(SYSTEM, 'deps:log', (d) => {
      if (d.module_id !== mod.id) return
      setModal((m) => m && ({ ...m, log: [...(m.log ?? []), d.line].slice(-30) }))
    })
  }, [loading, type, hasDeps, mod.id, setModal])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [log])

  const confirmLabel = {
    activate: hasDeps ? 'Installer et activer' : 'Activer',
    deactivate: 'Désactiver',
    uninstall: 'Désinstaller',
    config: 'Enregistrer',
    meta: 'Enregistrer',
    update: `Mettre à jour en v${mod.remote_version}`,
    deps: 'Réinstaller',
  }[type]

  const payload = type === 'meta'
    ? { category: meta.category, tags: meta.tags.split(',').map((t) => t.trim()).filter(Boolean) }
    : config

  return createPortal(
    <div className="cc-modal-backdrop" onClick={loading ? undefined : onCancel}>
      <div className="cc-modal" onClick={(e) => e.stopPropagation()}>

        <div className="cc-modal-head">
          <ModuleIcon id={mod.id} fallback={mod.icon} size={36} className="cc-modal-icon" />
          <div>
            <p className="cc-modal-title">{TITLES[type]} {mod.name}</p>
            <p className="cc-modal-sub">v{mod.version}</p>
          </div>
        </div>

        <div className="cc-modal-body">
          {loading ? (
            <div className="cc-modal-progress">
              <span className="cp-spinner cc-modal-spinner" />
              <span className="cc-modal-progress-msg">{loadingMsg}</span>
            </div>
          ) : type === 'update' ? (
            <>
              <p className="cc-modal-text">
                Mettre à jour <strong>{mod.name}</strong> de <strong>v{mod.version}</strong> vers <strong>v{mod.remote_version}</strong> ?
                Ses données et sa configuration sont conservées.
              </p>
              {mod.status === 'active' && <p className="cc-modal-text">Le module est actif : il sera désactivé le temps de la mise à jour puis réactivé.</p>}
              {remoteDeps && !sameDeps(remoteDeps, pythonDeps) && (
                <>
                  <p className="cc-modal-text">Ses dépendances changent et seront installées :</p>
                  <ul className="cc-modal-deps">{remoteDeps.map((d) => <li key={d}>{d}</li>)}</ul>
                  {mod.status === 'active' && <p className="cc-modal-warn">Le backend Python sera redémarré après l'installation.</p>}
                </>
              )}
            </>
          ) : type === 'deps' ? (
            <>
              <p className="cc-modal-text">Réinstaller les dépendances Python de <strong>{mod.name}</strong> (téléchargement d'une copie neuve) :</p>
              <ul className="cc-modal-deps">{pythonDeps.map((d) => <li key={d}>{d}</li>)}</ul>
              {mod.status === 'active' && <p className="cc-modal-warn">Le backend Python sera redémarré après la réinstallation.</p>}
            </>
          ) : type === 'deactivate' ? (
            <p className="cc-modal-text">Désactiver le module <strong>{mod.name}</strong> ?</p>
          ) : type === 'uninstall' ? (
            <p className="cc-modal-text">
              Supprimer définitivement les fichiers du module <strong>{mod.name}</strong> ?
              Ses données utilisateur sont conservées.
            </p>
          ) : type === 'meta' ? (
            <div className="cc-config">
              <label className="cc-config-row">
                <span className="cc-config-label">Catégorie</span>
                <input
                  type="text"
                  list="cc-categories"
                  value={meta.category}
                  onChange={(e) => setMeta((m) => ({ ...m, category: e.target.value }))}
                />
                <datalist id="cc-categories">
                  {categories.map((c) => <option key={c} value={c} />)}
                </datalist>
              </label>
              <label className="cc-config-row">
                <span className="cc-config-label">Tags</span>
                <input
                  type="text"
                  placeholder="séparés par des virgules"
                  value={meta.tags}
                  onChange={(e) => setMeta((m) => ({ ...m, tags: e.target.value }))}
                />
              </label>
              {mod.meta_overridden && (
                <button className="btn btn-ghost cc-meta-reset" onClick={() => onConfirm({})}>
                  Revenir aux valeurs du module
                </button>
              )}
            </div>
          ) : type === 'config' ? (
            <ConfigForm schema={mod.config_schema ?? {}} values={config} onChange={setConfig} />
          ) : hasDeps ? (
            <>
              <p className="cc-modal-text">Les dépendances suivantes seront installées :</p>
              <ul className="cc-modal-deps">
                {pythonDeps.map((d) => <li key={d}>{d}</li>)}
              </ul>
              <p className="cc-modal-warn">Le backend Python sera redémarré après l'installation.</p>
            </>
          ) : (
            <p className="cc-modal-text">Activer le module <strong>{mod.name}</strong> ?</p>
          )}

          {error && <p className="cc-modal-error">{error}</p>}
          {log?.length > 0 && <pre className="cc-modal-log" ref={logRef}>{log.join('\n')}</pre>}
        </div>

        {!loading && (
          <div className="cc-modal-footer">
            <button className="btn btn-ghost" onClick={onCancel}>Annuler</button>
            <button
              className={`btn ${type === 'uninstall' ? 'btn-danger' : 'btn-primary'}`}
              onClick={() => onConfirm(payload)}
              disabled={type === 'config' && !config}
              autoFocus
            >
              {error ? 'Réessayer' : confirmLabel}
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}

function ConfigForm({ schema, values, onChange }) {
  if (!values) return <span className="cp-spinner" />
  const entries = Object.entries(schema)
  if (entries.length === 0) return <p className="cc-modal-text">Ce module n'a aucun paramètre.</p>

  const set = (key, value) => onChange({ ...values, [key]: value })

  return (
    <div className="cc-config">
      {entries.map(([key, def]) => {
        const value = values[key] ?? def.default ?? ''
        const label = def.title ?? def.description ?? key
        let input
        if (def.type === 'boolean') {
          input = <input type="checkbox" checked={!!value} onChange={(e) => set(key, e.target.checked)} />
        } else if (Array.isArray(def.enum)) {
          input = (
            <select value={value} onChange={(e) => set(key, e.target.value)}>
              {def.enum.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
            </select>
          )
        } else if (def.type === 'integer' || def.type === 'number') {
          input = (
            <input
              type="number"
              step={def.type === 'integer' ? 1 : 'any'}
              min={def.minimum}
              max={def.maximum}
              value={value}
              onChange={(e) => {
                const n = def.type === 'integer' ? parseInt(e.target.value, 10) : parseFloat(e.target.value)
                set(key, Number.isNaN(n) ? '' : n)
              }}
            />
          )
        } else {
          input = <input type="text" value={value} onChange={(e) => set(key, e.target.value)} />
        }
        return (
          <label key={key} className="cc-config-row">
            <span className="cc-config-label">{label}</span>
            {input}
          </label>
        )
      })}
    </div>
  )
}

// ── Card ──────────────────────────────────────────────────────────────────────

function CatalogCard({ mod, onAction, onInstall, installing, busy, selected, onToggle, activeTags, onTagClick, onCategoryClick, onOpenEnv }) {
  const pythonDeps = depsOf(mod)
  const hasConfig  = Object.keys(mod.config_schema ?? {}).length > 0
  const canSelect  = mod.status === 'available' && !!onToggle

  return (
    <div
      className={`cc ${mod.status} ${canSelect ? 'selectable' : ''} ${selected ? 'selected' : ''}`}
      onClick={canSelect ? (e) => { if (!e.target.closest('button, input, a')) onToggle() } : undefined}
    >
      <div className="cc-top">
        {mod.status === 'available' && (
          <input
            type="checkbox"
            className="cc-select"
            checked={!!selected}
            onChange={() => onToggle?.()}
            disabled={!onToggle}
            aria-label={`Sélectionner ${mod.name}`}
          />
        )}
        <ModuleIcon id={mod.id} fallback={mod.icon} size={40} className="cc-icon" />
        <div className="cc-info">
          <span className="cc-name">{mod.name}</span>
          <span className="cc-version">
            v{mod.version}
            {mod.update_available && <span className="cc-version-new" title="Nouvelle version dans le dépôt"> → v{mod.remote_version}</span>}
          </span>
        </div>
        {mod.update_available && <span className="cc-badge-update">Mise à jour</span>}
        {mod.status === 'active' && <span className="cc-badge-active">Actif</span>}
        {mod.status === 'available' && mod.source === 'remote' && <span className="cc-badge-remote" title="Module du dépôt GitHub">Dépôt</span>}
        {mod.status === 'error' && <span className="cc-badge-error">Erreur</span>}
      </div>

      <button className="cc-category" onClick={() => onCategoryClick(mod.category)} title="Filtrer sur cette catégorie">
        {mod.category}
      </button>

      <p className="cc-desc">{mod.description}</p>

      {mod.tags?.length > 0 && (
        <div className="cc-tags">
          {mod.tags.map((tag) => (
            <button
              key={tag}
              className={`cc-tag-chip ${activeTags.includes(tag) ? 'active' : ''}`}
              onClick={() => onTagClick(tag)}
            >
              #{tag}
            </button>
          ))}
        </div>
      )}

      {mod.env_missing?.length > 0 && (
        <button className="cc-env-missing" onClick={onOpenEnv} title="Définir dans Variables">
          🔑 {mod.env_missing.length > 1 ? 'Variables manquantes' : 'Variable manquante'} : {mod.env_missing.join(', ')}
        </button>
      )}
      {mod.status === 'error' && mod.last_error && (
        <p className="cc-error" title={mod.last_error}>{mod.last_error}</p>
      )}

      <div className="cc-meta">
        {mod.database?.type && mod.database.type !== 'none' && (
          <span className="cc-tag db">{mod.database.type}</span>
        )}
        {mod.permissions?.map((p) => (
          <span key={p} className="cc-tag">{p}</span>
        ))}
        {pythonDeps.length > 0 && (
          <span className="cc-tag" title={pythonDeps.join(', ')}>📦 {pythonDeps.length} dép.</span>
        )}
      </div>

      <div className="cc-actions">
        {mod.status === 'available' && (
          <button className="btn btn-secondary" onClick={() => onInstall(mod)} disabled={installing}>Installer</button>
        )}

        {isInstalled(mod) && (
          <>
            {mod.update_available && (
              <button className="btn btn-primary" onClick={() => onAction('update')} disabled={busy} title={`Installer la v${mod.remote_version}`}>
                Mettre à jour
              </button>
            )}
            {(mod.status === 'installed' || mod.status === 'error') && (
              <button className="btn btn-primary" onClick={() => onAction('activate')}>
                {mod.status === 'error' ? 'Réessayer' : 'Activer'}
              </button>
            )}
            {mod.status === 'active' && (
              <button className="btn btn-ghost" onClick={() => onAction('deactivate')}>Désactiver</button>
            )}
            <button className="btn btn-ghost" onClick={() => onAction('meta')} title="Catégorie et tags">Tags</button>
            {hasConfig && (
              <button className="btn btn-ghost" onClick={() => onAction('config')} title="Configurer">⚙</button>
            )}
            {pythonDeps.length > 0 && (
              <button className="btn btn-ghost" onClick={() => onAction('deps')} disabled={busy} title={`Réinstaller les dépendances : ${pythonDeps.join(', ')}`}>📦</button>
            )}
            <button className="btn btn-ghost cc-uninstall" onClick={() => onAction('uninstall')}>
              Désinstaller
            </button>
          </>
        )}
      </div>
    </div>
  )
}
