const SIZES = [3, 4, 5, 6]
const UNDO_OPTS = [[0, 'Désactivé'], [1, '1 coup'], [3, '3 coups'], [5, '5 coups'], [10, '10 coups']]
const SLIDE = 110
const target = (n) => (n === 3 ? 512 : 2048)
const variantOf = (n) => `size-${n}`

const PALETTE = {
  2: ['#3a3a5c', '#2c2c46', '#c9cbff'],
  4: ['#41417a', '#33335e', '#d6d8ff'],
  8: ['#5150b8', '#3d3c94', '#fff'],
  16: ['#6366f1', '#4b4fd6', '#fff'],
  32: ['#7c5cf0', '#6243d6', '#fff'],
  64: ['#9a52e6', '#7a3bc9', '#fff'],
  128: ['#b84bd6', '#9634b8', '#fff'],
  256: ['#d0479f', '#ac2f82', '#fff'],
  512: ['#e65a82', '#c63f68', '#fff'],
  1024: ['#f2785e', '#d85a44', '#fff'],
  2048: ['#f8a94a', '#e2852a', '#2a1606'],
  4096: ['#3fd0b4', '#1fa88f', '#06231d'],
  8192: ['#38b6f0', '#1d8fcc', '#04192a'],
  big: ['#f4f4fa', '#c9cbe0', '#1b1b2e'],
}

const CSS = `
.g-fusion { flex: 1; display: flex; align-items: center; justify-content: center; padding: 16px; min-height: 0; }
.g-fusion .board { position: relative; border-radius: 14px; background: linear-gradient(160deg, #1d1d2b, #15151f);
  border: 1px solid #2c2c3e; box-shadow: 0 18px 50px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.03);
  touch-action: none; cursor: grab; flex-shrink: 0; }
.g-fusion .board:active { cursor: grabbing; }
.g-fusion .cell { position: absolute; border-radius: 9px; background: rgba(99,102,241,.06); border: 1px solid rgba(99,102,241,.08); }
.g-fusion .t { position: absolute; left: 0; top: 0; width: var(--cell); height: var(--cell);
  transform: translate(calc(var(--gap) + var(--c) * var(--step)), calc(var(--gap) + var(--r) * var(--step)));
  transition: transform ${SLIDE}ms ease-in-out; will-change: transform; }
.g-fusion .board.still .t { transition: none; }
.g-fusion .ti { width: 100%; height: 100%; border-radius: 9px; display: grid; place-items: center;
  font-weight: 800; letter-spacing: -.02em; font-variant-numeric: tabular-nums; font-size: calc(var(--cell) * .42);
  box-shadow: inset 0 -3px 0 rgba(0,0,0,.18), inset 0 1px 0 rgba(255,255,255,.12); }
.g-fusion .t[data-len="3"] .ti { font-size: calc(var(--cell) * .34); }
.g-fusion .t[data-len="4"] .ti { font-size: calc(var(--cell) * .27); }
.g-fusion .t[data-len="5"] .ti { font-size: calc(var(--cell) * .22); }
.g-fusion .t[data-len="6"] .ti, .g-fusion .t[data-len="7"] .ti { font-size: calc(var(--cell) * .18); }
.g-fusion .t.sp .ti { animation: gf-spawn .18s ease-out; }
.g-fusion .t.mg { z-index: 2; }
.g-fusion .t.mg .ti { animation: gf-merge .2s ease-out; }
.g-fusion .t.fade .ti { animation: gf-fade .18s ease-out; }
.g-fusion .board.bump { animation: gf-bump .16s; }
@keyframes gf-spawn { from { transform: scale(.1); opacity: 0 } 70% { transform: scale(1.05); opacity: 1 } to { transform: scale(1) } }
@keyframes gf-merge { 0% { transform: scale(.9) } 45% { transform: scale(1.16) } 100% { transform: scale(1) } }
@keyframes gf-fade { from { opacity: .2; transform: scale(.92) } }
@keyframes gf-bump { 30% { transform: translateX(-3px) } 70% { transform: translateX(3px) } }
.gf-score { position: relative; }
.gf-gain { position: absolute; right: 6px; top: -2px; color: var(--accent-h); font-weight: 700; pointer-events: none; animation: gf-gain .7s ease-out forwards; }
@keyframes gf-gain { from { opacity: 1; transform: translateY(0) } to { opacity: 0; transform: translateY(-22px) } }
.gf-help p { margin-bottom: 8px; }
` + Object.entries(PALETTE).map(([v, [a, b, fg]]) => {
  const glow = v === 'big' || +v >= 128 ? `, 0 0 calc(var(--cell) * ${v === 'big' || +v >= 2048 ? '.35' : '.18'}) ${a}66` : ''
  return `.g-fusion .t[data-c="${v}"] .ti { background: linear-gradient(145deg, ${a}, ${b}); color: ${fg};
  box-shadow: inset 0 -3px 0 rgba(0,0,0,.18), inset 0 1px 0 rgba(255,255,255,.14)${glow}; }`
}).join('\n')

export default {
  async mount(root, ctx) {
    const { h } = ctx
    let prefs = {}
    try { prefs = (await ctx.prefs.get()) || {} } catch { prefs = {} }
    prefs = { size: 4, undo: 1, best: {}, ...prefs }
    if (!SIZES.includes(prefs.size)) prefs.size = 4
    prefs.best ||= {}

    let st = null
    let nextId = 1
    const els = new Map()
    let pending = null
    let saveTimer = 0
    let closeOv = null
    let destroyed = false

    const board = h('div', { class: 'board' })
    const wrap = h('div', { class: 'g-fusion' }, board)
    root.append(h('style', null, CSS), wrap)

    const scorePill = h('span', { class: 'pill gf-score' })
    const bestPill = h('span', { class: 'pill' })
    const movesPill = h('span', { class: 'pill' })
    ctx.setStatus(scorePill, bestPill, movesPill)

    const undoBtn = h('button', { title: 'Annuler le dernier coup (Retour arrière / X)', onclick: () => undo() })
    const sizeSeg = h('div', { class: 'seg' }, SIZES.map((n) => h('button', { 'data-n': n, onclick: () => changeSize(n) }, `${n}×${n}`)))
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => newGame(st.n, true) }, 'Nouvelle partie'),
      sizeSeg, undoBtn,
      h('button', { class: 'icon', title: 'Options', onclick: options }, '⚙️'),
      h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => showScores() }, '🏆'),
      h('button', { class: 'icon', title: 'Aide', onclick: help }, '❔'),
    )

    const savePrefs = () => ctx.prefs.set(prefs)
    const bestFor = (n) => prefs.best[n] || 0
    const maxTile = () => Math.max(0, ...[...st.tiles.values()].map((t) => t.v))
    const values = () => st.grid.map((id) => (id ? st.tiles.get(id).v : 0))

    function fromValues(n, vals) {
      const tiles = new Map(), grid = new Array(n * n).fill(0)
      vals.forEach((v, i) => {
        if (!v) return
        const t = { id: nextId++, v, r: Math.floor(i / n), c: i % n }
        tiles.set(t.id, t)
        grid[i] = t.id
      })
      return { tiles, grid }
    }

    function spawn() {
      const empty = []
      st.grid.forEach((id, i) => { if (!id) empty.push(i) })
      if (!empty.length) return null
      const i = empty[Math.floor(Math.random() * empty.length)]
      const t = { id: nextId++, v: Math.random() < 0.9 ? 2 : 4, r: Math.floor(i / st.n), c: i % st.n }
      st.tiles.set(t.id, t)
      st.grid[i] = t.id
      return t
    }

    function canMove() {
      const n = st.n, v = values()
      for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) {
        const x = v[r * n + c]
        if (!x) return true
        if (c + 1 < n && v[r * n + c + 1] === x) return true
        if (r + 1 < n && v[(r + 1) * n + c] === x) return true
      }
      return false
    }

    function slide(dir) {
      const n = st.n
      let moved = false, gained = 0
      const gone = [], born = []
      for (let i = 0; i < n; i++) {
        const pos = []
        for (let k = 0; k < n; k++) {
          pos.push(dir === 'left' ? [i, k] : dir === 'right' ? [i, n - 1 - k] : dir === 'up' ? [k, i] : [n - 1 - k, i])
        }
        const ids = pos.map(([r, c]) => st.grid[r * n + c]).filter(Boolean)
        const out = []
        for (const id of ids) {
          const t = st.tiles.get(id), last = out[out.length - 1]
          if (last && !last.m && last.v === t.v) { last.m = true; last.v *= 2; last.ids.push(id) }
          else out.push({ v: t.v, m: false, ids: [id] })
        }
        for (const [r, c] of pos) st.grid[r * n + c] = 0
        out.forEach((o, k) => {
          const [r, c] = pos[k]
          if (!o.m) {
            const t = st.tiles.get(o.ids[0])
            if (t.r !== r || t.c !== c) moved = true
            t.r = r; t.c = c
            st.grid[r * n + c] = t.id
            return
          }
          moved = true
          for (const id of o.ids) {
            const t = st.tiles.get(id)
            t.r = r; t.c = c
            gone.push(t)
            st.tiles.delete(id)
          }
          const nt = { id: nextId++, v: o.v, r, c }
          st.tiles.set(nt.id, nt)
          st.grid[r * n + c] = nt.id
          born.push(nt)
          gained += o.v
        })
      }
      return { moved, gained, gone, born }
    }

    function place(el, t) {
      el.style.setProperty('--r', t.r)
      el.style.setProperty('--c', t.c)
    }

    function makeEl(t, cls) {
      const len = String(t.v).length
      const el = h('div', { class: `t${cls ? ' ' + cls : ''}`, 'data-v': t.v, 'data-c': PALETTE[t.v] ? t.v : 'big', 'data-len': len },
        h('div', { class: 'ti' }, String(t.v)))
      place(el, t)
      board.append(el)
      els.set(t.id, el)
      if (cls) el.addEventListener('animationend', () => el.classList.remove(cls), { once: true })
      return el
    }

    function flush() {
      if (!pending) return
      clearTimeout(pending.timer)
      const fn = pending.fn
      pending = null
      fn()
    }

    function buildBoard() {
      flush()
      board.replaceChildren()
      els.clear()
      for (let i = 0; i < st.n * st.n; i++) {
        board.append(h('div', { class: 'cell', 'data-i': i }))
      }
      layout()
      for (const t of st.tiles.values()) makeEl(t, 'fade')
    }

    function layout() {
      if (!st) return
      const n = st.n
      const avail = Math.max(160, Math.min(root.clientWidth - 32, root.clientHeight - 32, 620))
      const gap = Math.max(5, Math.round(avail * (n >= 6 ? 0.018 : 0.024)))
      const cell = Math.floor((avail - gap * (n + 1)) / n)
      const size = cell * n + gap * (n + 1)
      board.classList.add('still')
      Object.assign(board.style, { width: `${size}px`, height: `${size}px` })
      board.style.setProperty('--cell', `${cell}px`)
      board.style.setProperty('--gap', `${gap}px`)
      board.style.setProperty('--step', `${cell + gap}px`)
      for (const c of board.querySelectorAll('.cell')) {
        Object.assign(c.style, {
          width: `${cell}px`, height: `${cell}px`,
          left: `${gap + (c.dataset.i % n) * (cell + gap)}px`,
          top: `${gap + Math.floor(c.dataset.i / n) * (cell + gap)}px`,
        })
      }
      void board.offsetWidth
      board.classList.remove('still')
    }

    function renderStatus(gain = 0) {
      scorePill.replaceChildren(h('span', { class: 'muted' }, 'Score'), h('b', null, ctx.format.int(st.score)))
      if (gain) {
        const g = h('span', { class: 'gf-gain' }, `+${gain}`)
        scorePill.append(g)
        setTimeout(() => g.remove(), 750)
      }
      bestPill.replaceChildren(h('span', { class: 'muted' }, 'Meilleur'), h('b', null, ctx.format.int(Math.max(bestFor(st.n), st.score))))
      movesPill.replaceChildren(h('span', { class: 'muted' }, 'Coups'), String(st.moves))
      undoBtn.textContent = prefs.undo ? `↶ Annuler${st.hist.length ? ` (${st.hist.length})` : ''}` : '↶ Annuler'
      undoBtn.disabled = !prefs.undo || !st.hist.length || st.over
      for (const b of sizeSeg.children) b.classList.toggle('on', +b.dataset.n === st.n)
    }

    function serialize() {
      return { v: 1, n: st.n, g: values(), score: st.score, moves: st.moves, hist: st.hist, reached: st.reached,
        assisted: st.assisted, undos: st.undos }
    }

    function scheduleSave() {
      clearTimeout(saveTimer)
      saveTimer = setTimeout(saveNow, 300)
    }
    function saveNow() {
      clearTimeout(saveTimer)
      saveTimer = 0
      if (!st || st.over || destroyed) return
      if (!st.moves && !st.hist.length) return
      return ctx.save.set(serialize())
    }

    function newGame(n, ask = false) {
      const go = async () => {
        if (ask && st && !st.over && st.moves > 0) {
          const ok = await ctx.confirm('Abandonner la partie en cours ? Son score sera enregistré.', { ok: 'Nouvelle partie' })
          if (!ok) return
          await finish(true)
        }
        closeOv?.(); closeOv = null
        clearTimeout(saveTimer)
        st = { n, score: 0, moves: 0, hist: [], reached: false, assisted: false, undos: 0, over: false, tiles: new Map(), grid: new Array(n * n).fill(0) }
        spawn(); spawn()
        if (prefs.size !== n) { prefs.size = n; savePrefs() }
        ctx.save.clear()
        buildBoard()
        renderStatus()
        refreshBest(n)
      }
      return go()
    }

    async function refreshBest(n) {
      try {
        const rows = await ctx.scores.list(variantOf(n), { limit: 1 })
        const top = rows[0]?.score || 0
        if (top > bestFor(n)) { prefs.best[n] = top; savePrefs() }
        if (st?.n === n) renderStatus()
      } catch { /* offline: keep prefs value */ }
    }

    function changeSize(n) {
      if (st && n === st.n) return
      newGame(n, true)
    }

    function move(dir) {
      if (!st || st.over || closeOv || document.querySelector('.dlg-back')) return
      flush()
      const snap = { g: values(), s: st.score, m: st.moves }
      const { moved, gained, gone, born } = slide(dir)
      if (!moved) {
        board.classList.remove('bump'); void board.offsetWidth; board.classList.add('bump')
        return
      }
      if (prefs.undo) {
        st.hist.push(snap)
        if (st.hist.length > prefs.undo) st.hist.splice(0, st.hist.length - prefs.undo)
      }
      st.moves++
      st.score += gained
      for (const t of gone) { const el = els.get(t.id); if (el) place(el, t) }
      for (const t of st.tiles.values()) { const el = els.get(t.id); if (el) place(el, t) }
      const fresh = spawn()
      ctx.sfx(gained ? 'merge' : 'move')
      renderStatus(gained)
      pending = {
        fn: () => {
          for (const t of gone) { els.get(t.id)?.remove(); els.delete(t.id) }
          for (const t of born) makeEl(t, 'mg')
          if (fresh) makeEl(fresh, 'sp')
        },
      }
      pending.timer = setTimeout(flush, SLIDE)
      const reachedNow = !st.reached && born.some((t) => t.v >= target(st.n))
      if (reachedNow) st.reached = true
      const blocked = !canMove()
      if (blocked) st.over = true
      if (!blocked) scheduleSave()
      if (reachedNow) {
        ctx.stats.update({ inc: { won: 1 }, max: { max_tile: target(st.n) } })
        setTimeout(() => { if (!destroyed) reachedOverlay(blocked) }, SLIDE + 220)
      } else if (blocked) {
        setTimeout(() => { if (!destroyed) finish(false) }, SLIDE + 260)
      }
    }

    function reachedOverlay(blocked) {
      ctx.sfx('win')
      closeOv = ctx.overlay({
        title: `${target(st.n)} atteint !`,
        text: blocked ? 'Bravo ! Plus aucun mouvement possible, la partie s’arrête ici.' : 'Bravo ! Vous pouvez continuer pour viser une tuile encore plus grande.',
        buttons: blocked
          ? [{ label: 'Voir le résultat', primary: true, onClick: () => { closeOv = null; finish(false) } }]
          : [
            { label: 'Nouvelle partie', onClick: () => { closeOv = null; newGame(st.n, true) } },
            { label: 'Continuer', primary: true, onClick: () => { closeOv = null } },
          ],
      })
    }

    async function finish(abandon) {
      flush()
      st.over = true
      clearTimeout(saveTimer)
      await ctx.save.clear()
      const n = st.n, score = st.score, top = maxTile()
      const meta = { max: top, moves: st.moves }
      if (st.assisted) { meta.assisted = true; meta.undos = st.undos }
      if (abandon) meta.abandoned = true
      if (score > bestFor(n)) prefs.best[n] = score
      savePrefs()
      const hubBest = Math.max(...SIZES.map(bestFor))
      const hubN = SIZES.filter((s) => bestFor(s) === hubBest).pop()
      ctx.stats.update({ inc: { played: 1 }, max: { max_tile: top, [`best_${n}`]: score }, set: { hub_best: `${ctx.format.int(hubBest)} (${hubN}×${hubN})` } })
      let r = null
      if (score > 0) {
        try { r = await ctx.scores.submit(variantOf(n), score, { meta }) } catch (e) { ctx.toast(`Score : ${e.message}`, 'err') }
      }
      if (abandon || destroyed || st.n !== n) return
      renderStatus()
      ctx.sfx('lose')
      const rankTxt = r ? (r.record ? '🏅 Nouveau record !' : `Rang ${r.rank} sur ${r.total}`) : ''
      closeOv = ctx.overlay({
        title: 'Plus de mouvement',
        text: `Score : ${ctx.format.int(score)}\nTuile max : ${top} · ${st.moves} coups${st.assisted ? '\n(partie assistée : annulations utilisées)' : ''}${rankTxt ? '\n' + rankTxt : ''}`,
        buttons: [
          { label: '🏆 Scores', onClick: () => { closeOv = null; showScores(r?.id) } },
          { label: 'Rejouer', primary: true, onClick: () => { closeOv = null; newGame(n) } },
        ],
      })
    }

    function undo() {
      if (!st || st.over || !st.hist.length || closeOv) return
      flush()
      const snap = st.hist.pop()
      const { tiles, grid } = fromValues(st.n, snap.g)
      st.tiles = tiles; st.grid = grid
      st.score = snap.s; st.moves = snap.m
      st.assisted = true
      st.undos++
      ctx.sfx('rotate')
      buildBoard()
      renderStatus()
      scheduleSave()
    }

    function showScores(highlightId = null) {
      ctx.showScores(variantOf(st.n), {
        highlightId,
        fmt: (s, m) => `${ctx.format.int(s)}${m?.max ? ` · ${m.max}` : ''}${m?.assisted ? ' ↶' : ''}`,
        variants: SIZES.map((n) => [variantOf(n), `${n}×${n}`]),
      })
    }

    async function options() {
      const sel = h('select', null, UNDO_OPTS.map(([v, l]) => h('option', { value: v, selected: v === prefs.undo }, l)))
      const ok = await ctx.dialog({
        title: 'Options',
        body: h('div', { class: 'form' }, h('label', null, 'Annulations'), sel,
          h('span'), h('span', { class: 'muted' }, 'Nombre de coups que l’on peut annuler. Une partie où l’on annule est marquée « assistée ».')),
        buttons: [{ label: 'Annuler', value: false }, { label: 'Enregistrer', value: true, primary: true }],
      })
      if (!ok) return
      prefs.undo = +sel.value
      if (st.hist.length > prefs.undo) st.hist.splice(0, st.hist.length - prefs.undo)
      savePrefs()
      renderStatus()
      scheduleSave()
    }

    function help() {
      ctx.dialog({
        title: 'Fusion — règles',
        body: h('div', { class: 'gf-help' },
          h('p', null, 'Faites glisser toutes les tuiles dans une direction. Deux tuiles de même valeur qui se rencontrent fusionnent et leur somme s’ajoute au score.'),
          h('p', null, 'Après chaque coup, une tuile 2 (parfois 4) apparaît. Objectif : 2048 (512 en 3×3), puis aussi loin que possible. La partie s’arrête quand plus aucun mouvement n’est possible.'),
          h('p', null, h('span', { class: 'kbd' }, '←↑→↓'), ' / ', h('span', { class: 'kbd' }, 'WASD'), ' / croix de la manette, ou glisser à la souris / au doigt.'),
          h('p', null, h('span', { class: 'kbd' }, 'Retour arrière'), ' ou ', h('span', { class: 'kbd' }, 'X'), ' : annuler (réglable dans ⚙️). Chaque taille de grille a son propre classement.')),
      })
    }

    ctx.onAction((a) => {
      if (a === 'up' || a === 'down' || a === 'left' || a === 'right') move(a)
      else if (a === 'b') undo()
    })

    let ptr = null
    board.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return
      ptr = { x: e.clientX, y: e.clientY, id: e.pointerId, done: false }
      board.setPointerCapture?.(e.pointerId)
    })
    const onPtr = (e, end) => {
      if (!ptr || ptr.id !== e.pointerId || ptr.done) { if (end) ptr = null; return }
      const dx = e.clientX - ptr.x, dy = e.clientY - ptr.y
      const thr = Math.max(24, (parseFloat(board.style.width) || 300) * 0.05)
      if (Math.max(Math.abs(dx), Math.abs(dy)) >= thr) {
        ptr.done = true
        move(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'))
      }
      if (end) ptr = null
    }
    board.addEventListener('pointermove', (e) => onPtr(e, false))
    board.addEventListener('pointerup', (e) => onPtr(e, true))
    board.addEventListener('pointercancel', () => { ptr = null })

    const ro = new ResizeObserver(() => layout())
    ro.observe(root)

    let saved = null
    try { saved = await ctx.save.get() } catch { saved = null }
    if (saved && saved.v === 1 && SIZES.includes(saved.n) && Array.isArray(saved.g) && saved.g.length === saved.n * saved.n) {
      const { tiles, grid } = fromValues(saved.n, saved.g)
      st = { n: saved.n, score: saved.score || 0, moves: saved.moves || 0, hist: Array.isArray(saved.hist) ? saved.hist : [],
        reached: !!saved.reached, assisted: !!saved.assisted, undos: saved.undos || 0, over: false, tiles, grid }
      buildBoard()
      renderStatus()
      refreshBest(st.n)
      if (!canMove()) finish(false)
    } else {
      await newGame(prefs.size)
    }

    return {
      pause() { flush(); saveNow() },
      destroy() {
        flush()
        saveNow()
        destroyed = true
        clearTimeout(saveTimer)
        ro.disconnect()
      },
    }
  },
}
