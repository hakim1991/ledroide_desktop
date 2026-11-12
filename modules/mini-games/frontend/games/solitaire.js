import * as K from './solitaire-core.js'
import { SYMBOL_DEFS, faceSVG } from './solitaire-cards.js'

const CSS = `
.g-solitaire { flex: 1; min-height: 0; display: flex; justify-content: center; padding: 6px 8px; position: relative; overflow: hidden; isolation: isolate;
  background: radial-gradient(ellipse at 50% 15%, #1e4a39 0%, #153429 55%, #0d221a 100%); }
.g-solitaire .board { position: relative; touch-action: none; user-select: none; flex-shrink: 0; }
.g-solitaire .slot { position: absolute; left: 0; top: 0; width: var(--cw); height: var(--ch); border-radius: calc(var(--cw) * .08);
  border: 2px dashed rgba(255,255,255,.15); display: grid; place-items: center; color: rgba(255,255,255,.28);
  font-size: calc(var(--cw) * .36); font-weight: 700; transition: border-color .15s, box-shadow .15s; }
.g-solitaire .slot svg { width: 46%; height: 46%; fill: rgba(255,255,255,.13); }
.g-solitaire .slot.stock { border-style: solid; border-color: rgba(255,255,255,.12); cursor: pointer; flex-direction: column; gap: 2px; }
.g-solitaire .slot.stock small { font-size: calc(var(--cw) * .15); font-weight: 500; }
.g-solitaire .slot.ok { border-color: rgba(250,204,21,.7); }
.g-solitaire .cd { position: absolute; left: 0; top: 0; width: var(--cw); height: var(--ch); perspective: 900px;
  transition: transform .26s cubic-bezier(.25,.75,.35,1) var(--d, 0ms); cursor: pointer; }
.g-solitaire .cd.down { cursor: default; }
.g-solitaire .cd.buried { visibility: hidden; }
.g-solitaire .notrans .cd, .g-solitaire .notrans .cdi, .g-solitaire .cd.drag, .g-solitaire .cd.casc { transition: none !important; }
.g-solitaire .cdi { position: absolute; inset: 0; transform-style: preserve-3d; transition: transform .3s ease var(--fd, 0ms); }
.g-solitaire .cd.down .cdi { transform: rotateY(180deg); }
.g-solitaire .fc, .g-solitaire .bk { position: absolute; inset: 0; border-radius: calc(var(--cw) * .08); backface-visibility: hidden;
  -webkit-backface-visibility: hidden; box-shadow: 0 1px 2px rgba(0,0,0,.45); }
.g-solitaire .fc { background: linear-gradient(170deg, #fffefa, #f1eee4); border: 1px solid #b9b4a5; overflow: hidden; }
.g-solitaire .fc svg { width: 100%; height: 100%; display: block; }
.g-solitaire .bk { transform: rotateY(180deg); border: 1px solid #cfcabb;
  background:
    radial-gradient(circle at 50% 50%, rgba(255,255,255,.22) 0 9%, transparent 10%),
    repeating-linear-gradient(45deg, rgba(255,255,255,.1) 0 1.5px, transparent 1.5px 7px),
    repeating-linear-gradient(-45deg, rgba(255,255,255,.1) 0 1.5px, transparent 1.5px 7px),
    radial-gradient(ellipse at 50% 40%, #5560c9, #27318a 75%);
  box-shadow: inset 0 0 0 calc(var(--cw) * .045) #f4f1e8, inset 0 0 0 calc(var(--cw) * .06) #8790d8, 0 1px 2px rgba(0,0,0,.45); }
.g-solitaire .cd.drag .fc { box-shadow: 0 10px 24px rgba(0,0,0,.55); }
.g-solitaire .cd.sel .fc { box-shadow: 0 0 0 2px #facc15, 0 0 14px rgba(250,204,21,.65); }
.g-solitaire .cd.hint .fc, .g-solitaire .cd.hint .bk, .g-solitaire .slot.hint { animation: sol-hint .45s ease-in-out 4 alternate; }
.g-solitaire .cd.shake { animation: sol-shake .3s; }
.g-solitaire .kcur { position: absolute; left: 0; top: 0; border: 2px solid #a5b4fc; border-radius: calc(var(--cw) * .1); pointer-events: none;
  z-index: 5000; box-shadow: 0 0 0 3px rgba(129,140,248,.35); transition: transform .12s, width .12s, height .12s; }
.g-solitaire .finish { position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); z-index: 4500; font-size: 15px;
  padding: 8px 18px; box-shadow: 0 6px 20px rgba(0,0,0,.45); animation: sol-pop .25s; }
.g-solitaire .ghost { position: absolute; left: 0; top: 0; width: var(--cw); height: var(--ch); pointer-events: none;
  animation: sol-fade .55s linear forwards; }
.g-solitaire .ghost .fc { box-shadow: none; }
.g-solitaire .deal { cursor: pointer; }
@keyframes sol-hint { from { box-shadow: 0 0 0 2px #38bdf8 } to { box-shadow: 0 0 0 4px #38bdf8, 0 0 18px rgba(56,189,248,.8) } }
@keyframes sol-shake { 25% { translate: -4px 0 } 75% { translate: 4px 0 } }
@keyframes sol-fade { from { opacity: .55 } to { opacity: 0 } }
@keyframes sol-pop { from { transform: translateX(-50%) scale(.8); opacity: 0 } }
.sol-stats td { padding: 3px 10px 3px 0; } .sol-stats td:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
.sol-stats th { text-align: right; color: var(--muted); font-weight: 500; padding: 0 0 4px 10px; }
`

const VARIANTS = [['draw1', 'Tirage 1 carte'], ['draw3', 'Tirage 3 cartes']]
const TOPS = ['stock', 'waste', null, 'f0', 'f1', 'f2', 'f3']
const PILES = ['stock', 'waste', 'f0', 'f1', 'f2', 'f3', 't0', 't1', 't2', 't3', 't4', 't5', 't6']
const MAX_HIST = 4000
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export default {
  async mount(root, ctx) {
    const { h, format } = ctx
    root.append(h('style', null, CSS))
    const board = h('div', { class: 'board' })
    const wrap = h('div', { class: 'g-solitaire' }, board)
    root.append(wrap)
    board.insertAdjacentHTML('beforeend', SYMBOL_DEFS)

    let prefs = { draw: 1, limited: false, autoFinish: true, winnable: false }
    try { prefs = { ...prefs, ...(await ctx.prefs.get()) } } catch { /* defaults */ }

    let g = null
    let running = false, tStart = 0
    let busy = false, destroyed = false
    let sel = null, drag = null
    let cursor = { pile: 't0', idx: 0 }, kb = false
    let hintIdx = 0, hintTimer = 0, hintEls = []
    let L = null, where = []
    let saveTimer = 0, stuckKey = '', cascade = null, finishTimer = 0, dealDelays = null, lastSize = ''

    const slots = {}
    for (const p of PILES) {
      const el = h('div', { class: `slot ${p === 'stock' ? 'stock' : ''}`, dataset: { pile: p } })
      if (p[0] === 'f') el.innerHTML = `<svg viewBox="0 0 100 100"><use href="#sol-s${p[1]}"/></svg>`
      if (p[0] === 't') el.textContent = 'R'
      slots[p] = el
      board.append(el)
    }
    const nodes = []
    for (let c = 0; c < 52; c++) {
      const el = h('div', { class: 'cd down', dataset: { c } },
        h('div', { class: 'cdi' }, h('div', { class: 'fc', html: faceSVG(c) }), h('div', { class: 'bk' })))
      nodes.push(el)
      board.append(el)
    }
    const kcur = h('div', { class: 'kcur hidden' })
    const finishBtn = h('button', { class: 'primary finish hidden', onclick: () => autoFinish() }, '✨ Terminer')
    board.append(kcur, finishBtn)

    const dealPill = h('span', { class: 'pill deal', title: 'Numéro de donne — cliquer pour en choisir une', onclick: () => dealDialog() })
    const scorePill = h('span', { class: 'pill', title: 'Score' })
    const movesPill = h('span', { class: 'pill', title: 'Coups' })
    const timePill = h('span', { class: 'pill', title: 'Temps' })
    const drawBtns = [1, 3].map((d) => h('button', { onclick: () => setDraw(d), title: `Retourner ${d} carte${d > 1 ? 's' : ''} à la fois` }, `Tirage ${d}`))
    const undoBtn = h('button', { class: 'icon', title: 'Annuler (Ctrl+Z)', onclick: () => undo() }, '↶')
    const redoBtn = h('button', { class: 'icon', title: 'Refaire (Ctrl+Y)', onclick: () => redo() }, '↷')
    ctx.setToolbar(
      h('button', { class: 'primary', title: 'Nouvelle donne (F2)', onclick: () => newGame() }, '↻ Nouvelle'),
      h('button', { class: 'icon', title: 'Rejouer la même donne', onclick: () => replay() }, '🔁'),
      h('div', { class: 'seg' }, drawBtns),
      undoBtn, redoBtn,
      h('button', { class: 'icon', title: 'Indice (H)', onclick: () => hint() }, '💡'),
      h('button', { class: 'icon', title: 'Options', onclick: () => optionsDialog() }, '⚙'),
      h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => showScores() }, '🏆'),
      h('button', { class: 'icon', title: 'Statistiques', onclick: () => showStats() }, '📊'),
      h('button', { class: 'icon', title: 'Aide', onclick: () => help() }, '❔'),
    )
    ctx.setStatus(dealPill, scorePill, movesPill, timePill)

    const core = () => g.core
    const opts = () => ({ draw: g.draw, limited: g.limited })
    const elapsed = () => g.elapsed + (running ? performance.now() - tStart : 0)
    const fmtTime = (ms) => format.time(ms).replace(/\.\d$/, '')

    // ── Layout & rendering ──────────────────────────────────────────────────

    function layout() {
      const W = Math.max(280, wrap.clientWidth - 16), H = Math.max(220, wrap.clientHeight - 12)
      const cw = Math.floor(Math.min(W / 7.84, H / (1.4 * 4.1), 130))
      const ch = Math.round(cw * 1.4), gap = Math.floor(cw * 0.14)
      L = { cw, ch, gap, bw: 7 * cw + 6 * gap, bh: H, topY: 4, fan: Math.round(cw * 0.24) }
      L.tabY = L.topY + ch + Math.max(8, Math.round(gap * 1.3))
      L.colX = (i) => i * (cw + gap)
      board.style.width = `${L.bw}px`
      board.style.height = `${H}px`
      board.style.setProperty('--cw', `${cw}px`)
      board.style.setProperty('--ch', `${ch}px`)
      const place = (el, x, y) => { el.style.transform = `translate(${x}px, ${y}px)` }
      place(slots.stock, L.colX(0), L.topY)
      place(slots.waste, L.colX(1), L.topY)
      for (let s = 0; s < 4; s++) place(slots[`f${s}`], L.colX(3 + s), L.topY)
      for (let i = 0; i < 7; i++) place(slots[`t${i}`], L.colX(i), L.tabY)
    }

    function colSteps(i) {
      const t = core().tab[i], d = core().down[i], n = t.length
      let dd = L.ch * 0.1, du = L.ch * 0.27
      const avail = L.bh - L.tabY - L.ch - 4
      const need = () => d * dd + Math.max(0, n - d - 1) * du
      if (need() > avail) dd = L.ch * 0.05
      if (need() > avail && n - d > 1) du = Math.max(L.ch * 0.09, (avail - d * dd) / (n - d - 1))
      return { dd, du }
    }

    function positions() {
      const c = core(), P = new Array(52)
      const ns = c.stock.length
      c.stock.forEach((id, i) => { P[id] = { pile: 'stock', idx: i, x: L.colX(0) + Math.floor(i / 6), y: L.topY - Math.floor(i / 6), up: false, z: 1 + i, b: i < ns - 2 && i % 6 !== 5 } })
      const n = c.waste.length, fanN = g.draw === 3 ? Math.min(3, n) : 1
      c.waste.forEach((id, i) => {
        const j = Math.max(0, i - (n - fanN))
        P[id] = { pile: 'waste', idx: i, x: L.colX(1) + j * L.fan, y: L.topY, up: true, z: 100 + i, b: i < n - fanN - 1 }
      })
      for (let s = 0; s < 4; s++) for (let r = 0; r < c.found[s]; r++) {
        P[s * 13 + r] = { pile: `f${s}`, idx: r, x: L.colX(3 + s), y: L.topY, up: true, z: 100 + r, b: r < c.found[s] - 2 }
      }
      for (let i = 0; i < 7; i++) {
        const { dd, du } = colSteps(i), d = c.down[i]
        c.tab[i].forEach((id, k) => {
          const y = L.tabY + (k <= d ? k * dd : d * dd + (k - d) * du)
          P[id] = { pile: `t${i}`, idx: k, x: L.colX(i), y, up: k >= d, z: 100 + k }
        })
      }
      return P
    }

    function render({ instant = false, delays = null, at = null } = {}) {
      if (!g || !L) return
      const P = positions()
      where = P
      if (instant) board.classList.add('notrans')
      const selSet = new Set(sel ? K.movable(core(), sel.pile, sel.idx) || [] : [])
      for (let id = 0; id < 52; id++) {
        const p = at ? { ...P[id], ...at(id) } : P[id], el = nodes[id]
        const tf = `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px)`
        el.classList.toggle('down', !p.up)
        el.title = p.up ? K.cardName(id) : ''
        el.classList.toggle('sel', selSet.has(id))
        el.classList.toggle('buried', !!p.b && !at)
        if (delays) { el.style.setProperty('--d', `${delays[id] || 0}ms`); el.style.setProperty('--fd', `${(delays[id] || 0) + 180}ms`) }
        else { el.style.removeProperty('--d'); el.style.removeProperty('--fd') }
        if (el._tf !== tf) {
          const moving = el._tf != null && !instant
          el._tf = tf
          el.style.transform = tf
          if (moving) {
            el.style.zIndex = 1000 + p.z
            clearTimeout(el._zt)
            el._zt = setTimeout(() => { el._zt = 0; el.style.zIndex = el._z }, 300 + (delays?.[id] || 0))
          }
        }
        el._z = p.z
        if (!el._zt) el.style.zIndex = p.z
      }
      if (instant) { void board.offsetWidth; board.classList.remove('notrans') }
      const c = core()
      const stockLbl = c.stock.length ? '' : K.canRecycle(c, opts()) ? '↻' : c.waste.length ? '✕' : ''
      const passes = K.maxPasses(opts())
      slots.stock.replaceChildren(h('span', null, stockLbl))
      if (Number.isFinite(passes)) slots.stock.append(h('small', null, `passe ${Math.min(passes, c.rec + 1)}/${passes}`))
      finishBtn.classList.toggle('hidden', !(K.canAutoFinish(c) && !busy && !g.over))
      updateStatus()
      renderCursor()
    }

    function updateStatus() {
      if (!g) return
      dealPill.textContent = `Donne n° ${g.seed}${g.winnable ? ' ✓' : ''}`
      dealPill.title = `${g.winnable ? 'Donne gagnable (vérifiée). ' : ''}Cliquer pour choisir une donne`
      scorePill.textContent = `★ ${format.int(core().score)}`
      movesPill.textContent = `${core().moves} coup${core().moves > 1 ? 's' : ''}`
      timePill.textContent = `⏱ ${fmtTime(elapsed())}`
      undoBtn.disabled = !g.hist.length || g.over || busy
      redoBtn.disabled = !g.redo.length || g.over || busy
      drawBtns.forEach((b, i) => b.classList.toggle('on', [1, 3][i] === prefs.draw))
    }

    function pileRect(pile) {
      const c = core()
      if (pile[0] === 't') {
        const i = +pile[1], t = c.tab[i]
        const last = t.length ? where[t[t.length - 1]].y : L.tabY
        return { x: L.colX(i), y: L.tabY - L.gap, w: L.cw, h: Math.max(last + L.ch, L.bh) - L.tabY + L.gap }
      }
      const col = pile === 'stock' ? 0 : pile === 'waste' ? 1 : 3 + +pile[1]
      return { x: L.colX(col), y: L.topY, w: L.cw, h: L.ch }
    }

    function cursorRect() {
      const c = core(), { pile } = cursor
      let x, y
      if (pile[0] === 't' && c.tab[+pile[1]].length) {
        const t = c.tab[+pile[1]], id = t[Math.min(cursor.idx, t.length - 1)]
        ;({ x, y } = where[id])
      } else if (pile === 'waste' && c.waste.length) ({ x, y } = where[c.waste[c.waste.length - 1]])
      else ({ x, y } = pileRect(pile))
      return { x, y }
    }

    function renderCursor() {
      kcur.classList.toggle('hidden', !kb || !g || g.over)
      if (!kb || !g) return
      const r = cursorRect()
      kcur.style.width = `${L.cw + 6}px`
      kcur.style.height = `${L.ch + 6}px`
      kcur.style.transform = `translate(${r.x - 3}px, ${r.y - 3}px)`
    }

    // ── Game flow ───────────────────────────────────────────────────────────

    function touch() {
      if (!running && !g.over) { running = true; tStart = performance.now() }
    }

    function snapshotState() {
      return { v: 1, seed: g.seed, draw: g.draw, limited: g.limited, winnable: g.winnable, core: K.encode(core()),
        hist: g.hist, redo: g.redo, elapsed: Math.round(elapsed()) }
    }
    function persist(now = false) {
      clearTimeout(saveTimer)
      if (!g || g.over || !(g.core.moves || g.redo.length)) return
      if (now) ctx.save.set(snapshotState())
      else saveTimer = setTimeout(() => ctx.save.set(snapshotState()), 400)
    }

    function pushHistory() {
      g.hist.push(K.encode(core()))
      if (g.hist.length > MAX_HIST) g.hist.shift()
      g.redo = []
    }

    function clearHint() {
      clearTimeout(hintTimer)
      for (const el of hintEls) el.classList.remove('hint')
      hintEls = []
    }

    function afterAction() {
      sel = null
      clearHint()
      render()
      persist()
      if (K.isWon(core())) { win(); return }
      clearTimeout(finishTimer)
      if (K.canAutoFinish(core())) {
        if (prefs.autoFinish) finishTimer = setTimeout(() => autoFinish(), 350)
        return
      }
      checkStuck()
    }

    function checkStuck() {
      const key = K.encode({ ...core(), score: 0, moves: 0 })
      if (key === stuckKey || K.findMoves(core(), opts()).length) return
      stuckKey = key
      setTimeout(() => { if (!destroyed && g && !g.over && K.encode({ ...core(), score: 0, moves: 0 }) === key) noMoves() }, 450)
    }

    function noMoves() {
      ctx.overlay({
        title: 'Aucun coup utile',
        text: 'Plus aucun coup ne fait progresser cette donne.\nAnnulez quelques coups ou lancez une nouvelle donne.',
        buttons: [
          ...(g.hist.length ? [{ label: '↶ Annuler', onClick: () => undo() }] : []),
          { label: 'Continuer' },
          { label: 'Nouvelle donne', primary: true, onClick: () => newGame(null, { ask: false }) },
        ],
      })
    }

    function doMove(from, idx, to) {
      if (busy || g.over || !K.legal(core(), from, idx, to)) return false
      pushHistory()
      touch()
      const r = K.applyMove(core(), from, idx, to)
      ctx.sfx(to[0] === 'f' ? 'pop' : 'move')
      if (r.revealed >= 0) setTimeout(() => ctx.sfx('flag'), 120)
      afterAction()
      return true
    }

    function doDraw() {
      if (busy || g.over) return
      if (!K.canDraw(core(), opts())) {
        ctx.sfx('error')
        if (core().waste.length) ctx.toast('Nombre de passages épuisé.')
        return
      }
      pushHistory()
      touch()
      const r = K.drawStock(core(), opts())
      ctx.sfx(r === 'recycle' ? 'rotate' : 'move')
      afterAction()
    }

    function toFoundation(pile, idx) {
      const cards = K.movable(core(), pile, idx)
      if (!cards || cards.length !== 1 || pile[0] === 'f') return false
      const to = `f${K.suitOf(cards[0])}`
      if (!K.legal(core(), pile, idx, to)) { shake(cards); ctx.sfx('error'); return false }
      return doMove(pile, idx, to)
    }

    function shake(cards) {
      for (const c of cards) { const el = nodes[c].querySelector('.cdi'); el.parentElement.classList.remove('shake'); void el.offsetWidth; el.parentElement.classList.add('shake') }
      setTimeout(() => cards.forEach((c) => nodes[c].classList.remove('shake')), 320)
    }

    function undo() {
      if (busy || !g || g.over || !g.hist.length) return
      let prev
      try { prev = K.decode(g.hist[g.hist.length - 1]) } catch { g.hist = []; updateStatus(); return }
      g.hist.pop()
      g.redo.push(K.encode(core()))
      g.core = prev
      ctx.sfx('click')
      sel = null
      clearHint()
      clearTimeout(finishTimer)
      root.querySelector(':scope > .overlay')?.remove()
      render()
      persist()
    }

    function redo() {
      if (busy || !g || g.over || !g.redo.length) return
      let next
      try { next = K.decode(g.redo[g.redo.length - 1]) } catch { g.redo = []; updateStatus(); return }
      g.redo.pop()
      g.hist.push(K.encode(core()))
      g.core = next
      ctx.sfx('click')
      touch()
      const keep = g.redo
      afterAction()
      g.redo = keep
      updateStatus()
    }

    function hint() {
      if (busy || !g || g.over) return
      clearHint()
      const list = K.findMoves(core(), opts())
      if (!list.length) {
        if (K.canAutoFinish(core())) { autoFinish(); return }
        noMoves()
        return
      }
      const m = list[hintIdx++ % list.length]
      if (m.draw) hintEls = [core().stock.length ? nodes[core().stock[core().stock.length - 1]] : slots.stock]
      else {
        hintEls = (K.movable(core(), m.from, m.idx) || []).map((c) => nodes[c])
        const t = K.pileTop(core(), m.to)
        hintEls.push(t >= 0 ? nodes[t] : slots[m.to])
      }
      for (const el of hintEls) { el.classList.remove('hint'); void el.offsetWidth; el.classList.add('hint') }
      ctx.sfx('tick')
      hintTimer = setTimeout(clearHint, 1900)
    }

    async function autoFinish() {
      if (busy || !g || g.over || !K.canAutoFinish(core())) return
      clearTimeout(finishTimer)
      busy = true
      sel = null
      touch()
      render()
      let m
      while (!destroyed && (m = K.nextAutoMove(core()))) {
        K.applyMove(core(), m.from, m.idx, m.to)
        render()
        ctx.sfx('pop')
        await sleep(85)
      }
      busy = false
      if (!destroyed && K.isWon(core())) win()
    }

    async function win() {
      if (g.over) return
      const mine = g
      g.elapsed = elapsed()
      running = false
      g.over = true
      clearTimeout(saveTimer)
      ctx.save.clear()
      render()
      const time = Math.round(g.elapsed), bonus = K.timeBonus(time), total = core().score + bonus
      const variant = `draw${g.draw}`
      const d = g.draw
      ctx.sfx('win')
      const results = (async () => {
        let r = null
        try {
          r = await ctx.scores.submit(variant, total, { meta: { time, moves: core().moves, seed: g.seed, bonus, limited: g.limited } })
        } catch (e) { ctx.toast(e.message, 'err') }
        const prev = await ctx.stats.get().catch(() => ({}))
        const st = await ctx.stats.update({
          inc: { played: 1, won: 1, [`played_draw${d}`]: 1, [`won_draw${d}`]: 1 },
          max: { streak_max: (prev.streak || 0) + 1, [`best_score_draw${d}`]: total },
          min: { [`best_time_draw${d}`]: time },
          set: { streak: (prev.streak || 0) + 1 },
        })
        const best = Math.max(st.best_score_draw1 || 0, st.best_score_draw3 || 0)
        if (best) await ctx.stats.update({ set: { hub_best: `${format.int(best)} pts` } })
        return r
      })()
      await Promise.all([runCascade(), results])
      const r = await results
      if (destroyed || mine.dead || g !== mine) return
      ctx.overlay({
        title: '🎉 Gagné !',
        text: `Donne n° ${g.seed} · tirage ${g.draw}\n`
          + `Score ${format.int(core().score)} + bonus temps ${format.int(bonus)} = ${format.int(total)} pts\n`
          + `${fmtTime(time)} · ${core().moves} coups`
          + (r?.rank === 1 ? '\nNouveau record !' : r?.rank ? `\nRang ${r.rank} au classement` : ''),
        buttons: [
          { label: '🏆 Classement', onClick: () => showScores(r?.id) },
          { label: '🔁 Même donne', onClick: () => newGame(g.seed, { ask: false }) },
          { label: 'Nouvelle donne', primary: true, onClick: () => newGame(null, { ask: false }) },
        ],
      })
    }

    function runCascade() {
      return new Promise((resolve) => {
        const order = []
        for (let r = 12; r >= 0; r--) for (let s = 0; s < 4; s++) order.push(s * 13 + r)
        const flying = []
        let launched = 0, frame = 0, raf = 0
        const g0 = Math.max(0.35, L.ch / 220)
        const done = () => {
          cancelAnimationFrame(raf)
          wrap.removeEventListener('pointerdown', done)
          board.querySelectorAll('.ghost').forEach((e) => e.remove())
          cascade = null
          resolve()
        }
        cascade = { stop: done }
        wrap.addEventListener('pointerdown', done)
        const step = () => {
          frame++
          if (frame % 7 === 1 && launched < order.length) {
            const id = order[launched++], el = nodes[id], p = where[id]
            el.classList.add('casc')
            el.classList.remove('buried')
            el.style.zIndex = 3000 + launched
            el._tf = 'casc'
            const dir = Math.random() < 0.5 ? -1 : 1
            flying.push({ el, x: p.x, y: p.y, vx: dir * (2 + Math.random() * 3.5) * (L.cw / 70), vy: -Math.random() * 4 * (L.ch / 100), n: 0 })
          }
          for (let i = flying.length - 1; i >= 0; i--) {
            const f = flying[i]
            f.vy += g0
            f.x += f.vx
            f.y += f.vy
            if (f.y + L.ch > L.bh) { f.y = L.bh - L.ch; f.vy = -f.vy * 0.72 }
            f.el.style.transform = `translate(${f.x}px, ${f.y}px)`
            if (++f.n % 4 === 0) {
              const gh = h('div', { class: 'ghost', style: { transform: f.el.style.transform, zIndex: 2000 } })
              gh.append(f.el.querySelector('.fc').cloneNode(true))
              gh.addEventListener('animationend', () => gh.remove())
              board.append(gh)
            }
            if (f.x < -L.cw * 1.2 || f.x > L.bw + L.cw * 0.2) { f.el.classList.remove('casc'); f.el.style.transform = 'translate(-9999px, 0)'; flying.splice(i, 1) }
          }
          if (launched >= order.length && !flying.length) { done(); return }
          raf = requestAnimationFrame(step)
        }
        raf = requestAnimationFrame(step)
      })
    }

    function stopCascade() {
      cascade?.stop()
      for (const el of nodes) el.classList.remove('casc')
    }

    async function pickSeed() {
      if (!prefs.winnable) return { seed: 1 + Math.floor(Math.random() * 999999), winnable: false }
      const o = { draw: prefs.draw, limited: prefs.limited }
      const start = Math.floor(Math.random() * 999999)
      const t0 = performance.now()
      let told = false
      for (let k = 0; k < 6000; k++) {
        const seed = ((start + k * 7919) % 999999) + 1
        if (K.greedyWins(seed, o)) return { seed, winnable: true }
        if (k % 25 === 24) {
          if (!told && performance.now() - t0 > 300) { told = true; ctx.toast('Recherche d’une donne gagnable…') }
          await sleep(0)
          if (destroyed) break
        }
      }
      return { seed: 1 + Math.floor(Math.random() * 999999), winnable: false }
    }

    async function abandonStats() {
      await ctx.stats.update({ inc: { played: 1, [`played_draw${g.draw}`]: 1 }, set: { streak: 0 } })
    }

    async function newGame(seed = null, { ask = true } = {}) {
      if (busy && !cascade) return
      if (g && !g.over && g.core.moves > 0) {
        if (ask && !(await ctx.confirm('Abandonner la donne en cours ?', { ok: 'Abandonner', danger: true }))) return
        abandonStats()
      }
      if (g) g.dead = true
      stopCascade()
      clearTimeout(finishTimer)
      root.querySelector(':scope > .overlay')?.remove()
      busy = true
      let winnable = false
      if (seed == null) ({ seed, winnable } = await pickSeed())
      else winnable = g?.seed === seed && g.winnable && g.draw === prefs.draw && g.limited === prefs.limited
      if (destroyed) return
      ctx.save.clear()
      g = { seed, draw: prefs.draw, limited: prefs.limited, winnable, core: K.deal(seed), hist: [], redo: [], elapsed: 0, over: false }
      running = false
      sel = null
      stuckKey = ''
      hintIdx = 0
      clearHint()
      await dealAnimation()
    }

    async function dealAnimation() {
      busy = true
      updateStatus()
      layout()
      lastSize = `${wrap.clientWidth}x${wrap.clientHeight}`
      const stockPos = { x: L.colX(0), y: L.topY, up: false }
      render({ instant: true, at: () => stockPos })
      const delays = {}
      K.DEAL_ORDER.forEach(([col, k], n) => { delays[core().tab[col][k]] = 60 + n * 38 })
      dealDelays = delays
      render({ delays })
      ctx.sfx('rotate')
      const total = 60 + K.DEAL_ORDER.length * 38 + 450
      for (let t = 0; t < total; t += 190) { await sleep(190); if (destroyed) return; if (t < total - 500) ctx.sfx('tick') }
      dealDelays = null
      busy = false
      render()
    }

    function replay() {
      if (!g) return
      newGame(g.seed)
    }

    async function setDraw(d) {
      if (d === prefs.draw && (!g || g.draw === d)) return
      if (g && !g.over && g.core.moves > 0) {
        if (!(await ctx.confirm(`Passer au tirage ${d} lance une nouvelle donne. Abandonner la donne en cours ?`, { ok: 'Abandonner', danger: true }))) return
        prefs.draw = d
        ctx.prefs.set(prefs)
        await newGame(null, { ask: false })
        return
      }
      prefs.draw = d
      ctx.prefs.set(prefs)
      if (g && !g.over) {
        g.draw = d
        g.winnable = false
        render({ instant: true })
      } else updateStatus()
    }

    async function dealDialog() {
      const input = h('input', { type: 'number', min: 1, max: 999999, value: g?.seed || 1, style: { width: '130px' } })
      const v = await ctx.dialog({
        title: 'Choisir une donne',
        body: h('div', null,
          h('div', { class: 'form' }, h('label', null, 'Donne n°'), input),
          h('p', { class: 'muted', style: { marginTop: '10px' } }, 'Une même donne distribue toujours les mêmes cartes : pratique pour rejouer ou défier quelqu’un.')),
        buttons: [{ label: 'Annuler', value: null }, { label: 'Jouer cette donne', value: () => input.value, primary: true }],
      })
      if (v == null) return
      const seed = Math.round(+v)
      if (!(seed >= 1 && seed <= 999999)) { ctx.toast('Numéro de donne entre 1 et 999 999.', 'err'); return }
      newGame(seed)
    }

    async function optionsDialog() {
      const box = (k, label, help) => {
        const cb = h('input', { type: 'checkbox', checked: !!prefs[k] })
        return [cb, h('label', { style: { display: 'flex', gap: '8px', alignItems: 'flex-start', marginBottom: '10px', cursor: 'pointer' } },
          cb, h('span', null, label, help ? h('div', { class: 'muted', style: { fontSize: '12px' } }, help) : null))]
      }
      const [lim, limEl] = box('limited', 'Passages limités', '1 seul passage dans la pioche en tirage 1, 3 passages en tirage 3. Appliqué à la prochaine donne.')
      const [af, afEl] = box('autoFinish', 'Fin automatique', 'Quand toutes les cartes sont visibles et la pioche vide, les cartes montent seules aux fondations.')
      const [wn, wnEl] = box('winnable', 'Donnes gagnables uniquement', 'Chaque nouvelle donne est vérifiée par un solveur avant d’être distribuée.')
      const ok = await ctx.dialog({ title: 'Options', body: h('div', null, limEl, afEl, wnEl),
        buttons: [{ label: 'Annuler', value: false }, { label: 'Enregistrer', value: true, primary: true }] })
      if (!ok) return
      prefs = { ...prefs, limited: lim.checked, autoFinish: af.checked, winnable: wn.checked }
      ctx.prefs.set(prefs)
      if (g && !g.over && g.core.moves === 0 && g.limited !== prefs.limited) { g.limited = prefs.limited; g.winnable = false; render() }
      if (g && !g.over && prefs.autoFinish && K.canAutoFinish(core())) autoFinish()
    }

    function showScores(highlightId = null) {
      ctx.showScores(`draw${g?.over ? g.draw : prefs.draw}`, { highlightId, variants: VARIANTS, fmt: (s, m) => `${format.int(s)} pts${m?.time ? ` · ${fmtTime(m.time)}` : ''}` })
    }

    async function showStats() {
      const st = await ctx.stats.get().catch(() => ({}))
      const row = (label, a, b) => h('tr', null, h('td', null, label), h('td', null, a), h('td', null, b))
      const rate = (w, p) => (p ? `${Math.round((100 * (w || 0)) / p)} %` : '—')
      const t = (ms) => (ms ? fmtTime(ms) : '—')
      const n = (v) => (v ? format.int(v) : '—')
      const body = h('div', null,
        h('table', { class: 'sol-stats' }, h('thead', null, h('tr', null, h('td'), h('th', null, 'Tirage 1'), h('th', null, 'Tirage 3'))),
          h('tbody', null,
            row('Parties', n(st.played_draw1), n(st.played_draw3)),
            row('Victoires', n(st.won_draw1), n(st.won_draw3)),
            row('Taux de victoire', rate(st.won_draw1, st.played_draw1), rate(st.won_draw3, st.played_draw3)),
            row('Meilleur score', n(st.best_score_draw1), n(st.best_score_draw3)),
            row('Meilleur temps', t(st.best_time_draw1), t(st.best_time_draw3)))),
        h('p', { style: { marginTop: '12px' } }, `Total : ${format.int(st.played || 0)} partie(s), ${format.int(st.won || 0)} victoire(s) (${rate(st.won, st.played)}).`),
        h('p', null, `Série en cours : ${st.streak || 0} · meilleure série : ${st.streak_max || 0}`),
        h('p', { class: 'muted', style: { fontSize: '12px', marginTop: '6px' } }, 'Une partie abandonnée (nouvelle donne en cours de jeu) compte comme perdue.'))
      ctx.dialog({ title: 'Statistiques', body })
    }

    function help() {
      const k = (s) => h('span', { class: 'kbd' }, s)
      ctx.dialog({
        title: 'Solitaire — aide', wide: true,
        body: h('div', null,
          h('p', null, 'But : monter les 52 cartes sur les 4 fondations, une par couleur, de l’As au Roi.'),
          h('ul', { style: { margin: '8px 0 8px 18px' } },
            h('li', null, 'Dans les colonnes, on empile en descendant et en alternant rouge / noir. Une suite ordonnée se déplace en bloc.'),
            h('li', null, 'Seul un Roi (ou une suite commençant par un Roi) peut occuper une colonne vide.'),
            h('li', null, 'La carte cachée découverte en haut d’une colonne se retourne toute seule.'),
            h('li', null, 'Cliquez sur la pioche pour retourner 1 ou 3 cartes ; pioche vide : un clic la recycle (sauf passages limités).'),
            h('li', null, 'Quand toutes les cartes sont visibles et la pioche vide, « Terminer » monte tout aux fondations.')),
          h('p', null, h('b', null, 'Score : '), 'carte vers une fondation +10, de la pioche vers une colonne +5, carte retournée +5, fondation vers colonne −15, recyclage de la pioche −100 (tirage 1) ou −20 à partir du 4e passage (tirage 3). Bonus de fin : 700 000 ÷ secondes (au plus 23 333). Classement par score, séparé par tirage.'),
          h('p', { style: { marginTop: '8px' } }, h('b', null, 'Souris : '), 'glisser-déposer, ou clic sur une carte puis clic sur la destination. Double-clic : envoi vers la fondation.'),
          h('p', { style: { marginTop: '8px' } }, h('b', null, 'Clavier / manette : '), k('←↑→↓'), ' curseur, ', k('Espace'), '/A prendre ou poser, ',
            k('Retour'), '/B annuler la sélection, ', k('C'), '/X piocher, ', k('V'), '/Y indice, ', k('Maj'), ' vers la fondation, ',
            k('Ctrl+Z'), '/L annuler, ', k('Ctrl+Y'), '/R refaire, ', k('H'), ' indice, ', k('F2'), ' nouvelle donne.')),
        buttons: [{ label: 'Fermer', value: true, primary: true }],
      })
    }

    // ── Pointer input ───────────────────────────────────────────────────────

    function hit(target) {
      const cd = target.closest?.('.cd')
      if (cd && board.contains(cd)) {
        const p = where[+cd.dataset.c]
        return p ? { pile: p.pile, idx: p.idx } : null
      }
      const sl = target.closest?.('.slot')
      if (sl) {
        const pile = sl.dataset.pile
        return { pile, idx: pile[0] === 't' ? 0 : -1 }
      }
      return null
    }

    function handleClick(t) {
      if (!t) { if (sel) { sel = null; render() } return }
      if (t.pile === 'stock') { sel = null; doDraw(); return }
      if (sel) {
        if (sel.pile === t.pile && (t.pile[0] !== 't' || sel.idx === t.idx)) { sel = null; render(); return }
        if (K.legal(core(), sel.pile, sel.idx, t.pile)) { const s = sel; sel = null; doMove(s.pile, s.idx, t.pile); return }
        if (K.movable(core(), t.pile, t.idx)) { sel = t; ctx.sfx('click'); render(); return }
        ctx.sfx('error')
        shake(K.movable(core(), sel.pile, sel.idx) || [])
        sel = null
        render()
        return
      }
      if (K.movable(core(), t.pile, t.idx)) { sel = t; ctx.sfx('click'); render() }
    }

    function dropTarget(from, idx, x, y) {
      let best = null, bestA = 0
      for (const p of PILES) {
        if (p === 'stock' || p === 'waste' || !K.legal(core(), from, idx, p)) continue
        const r = pileRect(p)
        const ox = Math.min(x + L.cw, r.x + r.w) - Math.max(x, r.x), oy = Math.min(y + L.ch, r.y + r.h) - Math.max(y, r.y)
        const a = ox > 0 && oy > 0 ? ox * oy : 0
        if (a > bestA) { bestA = a; best = p }
      }
      return best
    }

    board.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || !g || busy || g.over) return
      if (kb) { kb = false; renderCursor() }
      const t = hit(e.target)
      const cards = t && t.pile !== 'stock' ? K.movable(core(), t.pile, t.idx) : null
      drag = { t, cards, sx: e.clientX, sy: e.clientY, moved: false, id: e.pointerId,
        origin: cards ? cards.map((c) => ({ x: where[c].x, y: where[c].y })) : null }
    })
    board.addEventListener('pointermove', (e) => {
      if (!drag || !drag.cards || e.pointerId !== drag.id) return
      const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 5) return
        drag.moved = true
        try { board.setPointerCapture(e.pointerId) } catch { /* pointer gone */ }
        clearHint()
        sel = null
        render()
        drag.cards.forEach((c, i) => { nodes[c].classList.add('drag'); nodes[c].style.zIndex = 3000 + i; nodes[c]._tf = 'drag' })
      }
      drag.cards.forEach((c, i) => { nodes[c].style.transform = `translate(${drag.origin[i].x + dx}px, ${drag.origin[i].y + dy}px)` })
      const tgt = dropTarget(drag.t.pile, drag.t.idx, drag.origin[0].x + dx, drag.origin[0].y + dy)
      for (const p of PILES) slots[p].classList.toggle('ok', p === tgt)
    })
    const endDrag = (e, cancel) => {
      if (!drag || e.pointerId !== drag.id) return
      const d = drag
      drag = null
      for (const p of PILES) slots[p].classList.remove('ok')
      if (d.moved) {
        d.cards.forEach((c) => nodes[c].classList.remove('drag'))
        const tgt = cancel ? null : dropTarget(d.t.pile, d.t.idx, d.origin[0].x + e.clientX - d.sx, d.origin[0].y + e.clientY - d.sy)
        if (!tgt || !doMove(d.t.pile, d.t.idx, tgt)) { if (!cancel) ctx.sfx('error'); render() }
        return
      }
      if (!cancel) handleClick(d.t)
    }
    board.addEventListener('pointerup', (e) => endDrag(e, false))
    board.addEventListener('pointercancel', (e) => endDrag(e, true))
    board.addEventListener('dblclick', (e) => {
      if (!g || busy || g.over) return
      const t = hit(e.target)
      if (!t || t.pile === 'stock' || t.pile[0] === 'f') return
      sel = null
      if (!toFoundation(t.pile, t.idx)) render()
    })
    wrap.addEventListener('pointerdown', (e) => { if (e.target === wrap && sel) { sel = null; render() } })

    // ── Keyboard / gamepad ──────────────────────────────────────────────────

    const colOf = (pile) => (pile[0] === 't' ? +pile[1] : pile === 'stock' ? 0 : pile === 'waste' ? 1 : 3 + +pile[1])
    const lastIdx = (i) => Math.max(0, core().tab[i].length - 1)

    function moveCursor(a) {
      let { pile, idx } = cursor
      if (pile[0] === 't') {
        const i = +pile[1]
        if (a === 'up') { if (idx > core().down[i] && core().tab[i].length) idx--; else { pile = TOPS[i] || 'waste'; idx = 0 } }
        else if (a === 'down') idx = Math.min(idx + 1, lastIdx(i))
        else { const j = (i + (a === 'left' ? 6 : 1)) % 7; pile = `t${j}`; idx = lastIdx(j) }
      } else {
        const col = colOf(pile)
        if (a === 'down') { pile = `t${col}`; idx = lastIdx(col) }
        else if (a === 'left' || a === 'right') {
          let k = col
          do k = (k + (a === 'left' ? 6 : 1)) % 7; while (!TOPS[k])
          pile = TOPS[k]
        }
      }
      cursor = { pile, idx }
    }

    function cursorPick() {
      const t = { pile: cursor.pile, idx: cursor.pile === 'waste' ? core().waste.length - 1 : cursor.pile[0] === 'f' ? 0 : cursor.idx }
      if (t.pile === 'stock') { sel = null; doDraw(); return }
      if (sel) {
        if (sel.pile === t.pile) { sel = null; render(); return }
        const s = sel
        if (doMove(s.pile, s.idx, t.pile)) { if (t.pile[0] === 't') cursor.idx = lastIdx(+t.pile[1]); return }
        ctx.sfx('error')
        return
      }
      if (K.movable(core(), t.pile, t.idx)) { sel = t; ctx.sfx('click'); render() } else ctx.sfx('error')
    }

    ctx.onAction((a) => {
      if (!g) return
      const ov = root.querySelector(':scope > .overlay')
      if (ov) { if (a === 'a') ov.querySelector('button.primary')?.click(); return }
      if (busy || g.over) return
      const wasKb = kb
      kb = true
      if (['up', 'down', 'left', 'right'].includes(a)) { if (wasKb) moveCursor(a); renderCursor(); return }
      if (a === 'a') { if (wasKb) cursorPick(); else renderCursor() }
      else if (a === 'b') { if (sel) { sel = null; render() } }
      else if (a === 'x') doDraw()
      else if (a === 'y') hint()
      else if (a === 'l') undo()
      else if (a === 'r') redo()
      else if (a === 'select') {
        const idx = cursor.pile === 'waste' ? core().waste.length - 1 : cursor.idx
        if (cursor.pile[0] === 't' && idx !== core().tab[+cursor.pile[1]].length - 1) { ctx.sfx('error'); return }
        toFoundation(cursor.pile, idx)
        if (cursor.pile[0] === 't') cursor.idx = lastIdx(+cursor.pile[1])
      }
      renderCursor()
    })

    const onKey = (e) => {
      if (document.querySelector('.dlg-back') || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target?.tagName || '')) return
      const k = e.key.toLowerCase()
      if ((e.ctrlKey || e.metaKey) && k === 'z' && !e.shiftKey) { e.preventDefault(); undo() }
      else if ((e.ctrlKey || e.metaKey) && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); redo() }
      else if (e.key === 'F2') { e.preventDefault(); newGame() }
      else if (k === 'h' && !e.ctrlKey && !e.metaKey && !e.altKey) hint()
    }
    document.addEventListener('keydown', onKey)
    ctx.onCleanup(() => document.removeEventListener('keydown', onKey))

    // ── Lifecycle ───────────────────────────────────────────────────────────

    const ro = new ResizeObserver(() => {
      const size = `${wrap.clientWidth}x${wrap.clientHeight}`
      if (cascade || size === lastSize) return
      lastSize = size
      layout()
      if (drag?.moved) return
      if (dealDelays) render({ delays: dealDelays })
      else render({ instant: true })
    })
    ro.observe(wrap)
    const tick = setInterval(() => { if (g && running) timePill.textContent = `⏱ ${fmtTime(elapsed())}` }, 500)

    function pause() {
      if (!g || g.over || !running) return
      g.elapsed = elapsed()
      running = false
      persist(true)
    }

    layout()
    lastSize = `${wrap.clientWidth}x${wrap.clientHeight}`
    let saved = null
    try { saved = await ctx.save.get() } catch { saved = null }
    let restored = false
    if (saved && saved.v === 1) {
      try {
        const c = K.decode(saved.core)
        g = { seed: saved.seed, draw: saved.draw === 3 ? 3 : 1, limited: !!saved.limited, winnable: !!saved.winnable, core: c,
          hist: Array.isArray(saved.hist) ? saved.hist.slice(-MAX_HIST) : [], redo: Array.isArray(saved.redo) ? saved.redo : [],
          elapsed: +saved.elapsed || 0, over: false }
        prefs.draw = g.draw
        restored = true
      } catch { ctx.save.clear() }
    }
    if (restored) {
      render({ instant: true })
      if (K.isWon(core())) win()
      else if (K.canAutoFinish(core())) { if (prefs.autoFinish) finishTimer = setTimeout(() => autoFinish(), 500) }
      else checkStuck()
    } else await newGame(null, { ask: false })

    return {
      pause,
      destroy() {
        destroyed = true
        pause()
        if (g && !g.over && g.core.moves) ctx.save.set(snapshotState())
        clearTimeout(saveTimer)
        clearTimeout(finishTimer)
        clearTimeout(hintTimer)
        clearInterval(tick)
        cascade?.stop()
        ro.disconnect()
      },
    }
  },
}
