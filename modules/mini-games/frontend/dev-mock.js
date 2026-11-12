// In-browser stand-in for the backend, used only when the page is opened outside LedroideDesktop
// (e.g. `python -m http.server` in frontend/). Mirrors backend/main.py actions.

const KEY = 'mini-games-mock'
const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {} } catch { return {} } }
const db = Object.assign({ scores: [], saves: {}, stats: {}, prefs: {}, seq: 1 }, load())
const persist = () => { try { localStorage.setItem(KEY, JSON.stringify(db)) } catch { /* quota */ } }
const sorted = (game, variant, lb) => db.scores.filter((s) => s.game === game && s.variant === variant)
  .sort((a, b) => (lb ? a.score - b.score : b.score - a.score) || a.id - b.id)

const actions = {
  'hub.summary'() {
    const games = {}
    const g = (n) => (games[n] ??= { best: {}, has_save: false, stats: {} })
    for (const s of db.scores) {
      const b = g(s.game).best[s.variant]
      const better = !b || (s.lower_better ? s.score < b.score : s.score > b.score)
      g(s.game).best[s.variant] = { score: better ? s.score : b.score, count: (b?.count || 0) + 1, lower_better: !!s.lower_better }
    }
    for (const k of Object.keys(db.saves)) { g(k).has_save = true; g(k).saved_at = db.saves[k].updated_at }
    for (const [k, v] of Object.entries(db.stats)) g(k).stats = v
    return { games, prefs: db.prefs._hub || {} }
  },
  'scores.list'({ game, variant = '', limit = 10, lower_better }) {
    return { scores: sorted(game, variant, lower_better).slice(0, limit) }
  },
  'score.submit'({ game, variant = '', score, lower_better = false, player = '', meta = {} }) {
    const row = { id: db.seq++, game, variant, score, lower_better, player, meta, created_at: new Date().toISOString() }
    db.scores.push(row)
    const ids = sorted(game, variant, lower_better).map((s) => s.id)
    const stale = new Set(ids.slice(50))
    db.scores = db.scores.filter((s) => !stale.has(s.id))
    const rank = ids.indexOf(row.id) < 50 ? ids.indexOf(row.id) + 1 : null
    return { id: row.id, rank, record: rank === 1, total: Math.min(ids.length, 50) }
  },
  'scores.clear'({ game, variant }) {
    db.scores = db.scores.filter((s) => !(s.game === game && (variant == null || s.variant === variant)))
    return { deleted: 0 }
  },
  'save.get'({ game }) { return db.saves[game] ? { ...db.saves[game] } : { state: null } },
  'save.set'({ game, state }) {
    if (state == null) delete db.saves[game]
    else db.saves[game] = { state: JSON.parse(JSON.stringify(state)), updated_at: new Date().toISOString() }
    return { ok: true }
  },
  'save.clear'({ game }) { delete db.saves[game]; return { ok: true } },
  'stats.get'({ game }) { return { stats: db.stats[game] || {} } },
  'stats.update'({ game, inc = {}, max = {}, min = {}, set = {} }) {
    const d = (db.stats[game] ??= {})
    for (const [k, v] of Object.entries(inc)) d[k] = (typeof d[k] === 'number' ? d[k] : 0) + v
    for (const [k, v] of Object.entries(max)) d[k] = typeof d[k] === 'number' ? Math.max(d[k], v) : v
    for (const [k, v] of Object.entries(min)) d[k] = typeof d[k] === 'number' ? Math.min(d[k], v) : v
    Object.assign(d, set)
    return { stats: { ...d } }
  },
  'prefs.get'({ game }) { return { prefs: db.prefs[game] || {} } },
  'prefs.set'({ game, data }) { db.prefs[game] = data; return { ok: true } },
  'game.reset'({ game, what = 'all' }) {
    if (what === 'all' || what === 'scores') db.scores = db.scores.filter((s) => s.game !== game)
    if (what === 'all' || what === 'stats') delete db.stats[game]
    if (what === 'all' || what === 'save') delete db.saves[game]
    return { ok: true }
  },
}

export const mockBridge = {
  async call(_module, action, payload) {
    const fn = actions[action]
    if (!fn) throw new Error(`Action inconnue : ${action}`)
    const res = JSON.parse(JSON.stringify(fn(payload || {})))
    persist()
    return res
  },
}
console.info('[mini-games] bridge absent : backend simulé (localStorage)')
