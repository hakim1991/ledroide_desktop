import { mountDuel, LEVELS } from './duel.js'
import * as K from './checkers-core.js'


const CSS = `
.g-checkers .ck { display: flex; gap: 12px; align-items: stretch; justify-content: center; }
.g-checkers .ck.vert { flex-direction: column; align-items: center; }
.g-checkers .ck-bw { --cs: 40px; position: relative; padding: 7px; border-radius: 10px; flex-shrink: 0;
  background: linear-gradient(145deg, #4a3222, #2b1c12); box-shadow: 0 10px 30px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.08); }
.g-checkers .ck-in { position: relative; width: calc(var(--cs) * var(--n)); height: calc(var(--cs) * var(--n)); }
.g-checkers .ck-grid { position: absolute; inset: 0; display: grid; grid-template-columns: repeat(var(--n), var(--cs)); grid-auto-rows: var(--cs);
  border-radius: 3px; overflow: hidden; touch-action: none; box-shadow: 0 0 0 1px rgba(0,0,0,.5); }
.g-checkers .ck-c { position: relative; background: #dcc9a4; }
.g-checkers .ck-c.dk { background: #6f4d36; cursor: pointer; }
.g-checkers .ck-c .no { position: absolute; left: 3%; top: 1%; font-size: calc(var(--cs) * .2); line-height: 1; color: rgba(255,240,220,.32);
  font-variant-numeric: tabular-nums; pointer-events: none; }
.g-checkers .ck-c.lm { background: #8a6230; }
.g-checkers .ck-c.via { background: #4f6b4a; }
.g-checkers .ck-c.tgt::after, .g-checkers .ck-c.step::after { content: ''; position: absolute; left: 50%; top: 50%; border-radius: 50%; pointer-events: none; }
.g-checkers .ck-c.tgt::after { width: 30%; height: 30%; margin: -15%; background: rgba(134, 239, 172, .85); box-shadow: 0 0 8px rgba(134,239,172,.8); }
.g-checkers .ck-c.step::after { width: 46%; height: 46%; margin: -23%; border: 2px dashed rgba(253, 224, 71, .85); box-sizing: border-box; }
.g-checkers .ck-c.tgt.step::after { border: 2px solid rgba(253, 224, 71, .9); background: rgba(134, 239, 172, .6); }
.g-checkers .ck-c.tgt:hover::after { transform: scale(1.35); }
.g-checkers .ck.locked .ck-c.dk { cursor: default; }
.g-checkers .ck-layer { position: absolute; inset: 0; pointer-events: none; }
.g-checkers .ck-p { position: absolute; width: calc(100% / var(--n)); height: calc(100% / var(--n)); display: grid; place-items: center; }
.g-checkers .ck-p.moving { z-index: 5; }
.g-checkers .disc { width: 80%; height: 80%; border-radius: 50%; position: relative; transition: transform .15s, box-shadow .15s, opacity .3s; }
.g-checkers .w .disc, .g-checkers .ck-sw.w { background: radial-gradient(circle at 38% 32%, #fffdf6, #efe4cc 55%, #bba98a);
  box-shadow: 0 3px 4px rgba(0,0,0,.45), inset 0 0 0 calc(var(--cs) * .07) rgba(170,150,115,.35), inset 0 -2px 3px rgba(0,0,0,.25); }
.g-checkers .b .disc, .g-checkers .ck-sw.b { background: radial-gradient(circle at 38% 32%, #6b5a52, #2b211e 55%, #120d0b);
  box-shadow: 0 3px 4px rgba(0,0,0,.55), inset 0 0 0 calc(var(--cs) * .07) rgba(255,255,255,.07), inset 0 -2px 3px rgba(0,0,0,.4); }
.g-checkers .k .disc::after { content: '♛'; position: absolute; inset: 0; display: grid; place-items: center; font-size: calc(var(--cs) * .42);
  line-height: 1; color: #d4a017; text-shadow: 0 1px 1px rgba(0,0,0,.6); }
.g-checkers .b.k .disc::after { color: #f2c94c; }
.g-checkers .ck-p.crown .disc::after { animation: ck-crown .45s cubic-bezier(.3,1.7,.5,1) both; }
@keyframes ck-crown { from { transform: scale(0) rotate(-40deg); opacity: 0 } }
.g-checkers .ck-p.mv .disc { box-shadow: 0 3px 4px rgba(0,0,0,.45), 0 0 0 2px rgba(129,140,248,.85); }
.g-checkers .ck-p.mv.must .disc { animation: ck-must 1.1s ease-in-out infinite; }
@keyframes ck-must { 50% { box-shadow: 0 3px 4px rgba(0,0,0,.45), 0 0 0 3px rgba(245,158,11,.95), 0 0 12px rgba(245,158,11,.6) } }
.g-checkers .ck-p.sel .disc { transform: translateY(-3px) scale(1.06); box-shadow: 0 8px 10px rgba(0,0,0,.5), 0 0 0 3px #fde047; animation: none; }
.g-checkers .ck-p.tc .disc { opacity: .7; box-shadow: 0 0 0 3px rgba(239,68,68,.9), 0 0 10px rgba(239,68,68,.6); }
.g-checkers .ck-p.dead .disc { animation: ck-dead .3s ease-in forwards; }
@keyframes ck-dead { to { transform: scale(.3); opacity: 0 } }
.g-checkers .ck.over .ck-p:not(.wn) .disc { opacity: .55; transition: opacity .6s .4s; }
.g-checkers .ck-cur { position: absolute; width: calc(100% / var(--n)); height: calc(100% / var(--n)); box-sizing: border-box; display: none;
  border: 3px solid #fde047; border-radius: 4px; box-shadow: 0 0 10px rgba(253,224,71,.7), inset 0 0 6px rgba(253,224,71,.5); pointer-events: none; z-index: 6; transition: left .08s, top .08s; }
.g-checkers .ck.kb .ck-cur { display: block; }
.g-checkers .ck-side { display: flex; flex-direction: column; gap: 6px; min-width: 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 8px; }
.g-checkers .ck-side .hd { display: flex; align-items: center; justify-content: space-between; color: var(--muted); font-size: 12px;
  text-transform: uppercase; letter-spacing: .06em; }
.g-checkers .ck-list { flex: 1; min-height: 0; overflow: auto; font-variant-numeric: tabular-nums; font-size: 13px; }
.g-checkers .ck-list .rw { display: grid; grid-template-columns: 2.6em 1fr 1fr; gap: 4px; padding: 1px 4px; border-radius: 4px; }
.g-checkers .ck-list .rw:nth-child(odd) { background: var(--surface2); }
.g-checkers .ck-list .n { color: var(--muted); }
.g-checkers .ck-list .lt { color: var(--accent-h); font-weight: 600; }
.g-checkers .ck.vert .ck-list { display: flex; flex-wrap: wrap; gap: 2px 10px; align-content: flex-start; }
.g-checkers .ck.vert .ck-list .rw { display: inline-flex; gap: 5px; background: none; padding: 0; }
.g-checkers .ck-hint { font-size: 12px; color: var(--warn); min-height: 16px; line-height: 1.35; }
.g-checkers .ck-side button { font-size: 12px; }
.g-checkers .ck-sw { width: 20px; height: 20px; border-radius: 50%; }
`

const SIDE_NAMES = ['Blancs', 'Noirs']

function segMs(g, a, b) {
  return 130 + 45 * Math.abs(g.row[a] - g.row[b])
}
function animDur(g, a) {
  if (!a || a.draw) return 0
  let t = 0
  for (let i = 1; i < a.path.length; i++) t += segMs(g, a.path[i - 1], a.path[i])
  return t
}

function createView(ctx, api) {
  const { h } = ctx
  let pos = null, g = null, n = 10
  let flip = null
  let sel = null
  let shown = null, shownFlip = null, shownPly = -1
  let interactive = false, over = null, lastKey = null
  let cur = { r: 0, c: 0 }
  let hintMsg = '', hintTimer = 0
  let fitSize = null
  const timers = []
  const pieceEls = new Map()
  let cells = []

  const grid = h('div', { class: 'ck-grid' })
  const layer = h('div', { class: 'ck-layer' })
  const curEl = h('div', { class: 'ck-cur' })
  const inner = h('div', { class: 'ck-in' }, grid, layer, curEl)
  const bw = h('div', { class: 'ck-bw' }, inner)
  const listEl = h('div', { class: 'ck-list' })
  const hintEl = h('div', { class: 'ck-hint' })
  const drawBtn = h('button', { type: 'button', title: 'Proposer la nulle à l’adversaire', onclick: () => offerDraw() }, '🤝 Proposer la nulle')
  const countEl = h('span')
  const side = h('div', { class: 'ck-side' }, h('div', { class: 'hd' }, h('span', null, 'Coups'), countEl), listEl, hintEl, drawBtn)
  const el = h('div', { class: 'ck' }, bw, side)

  bw.addEventListener('pointermove', () => { if (el.classList.contains('kb')) el.classList.remove('kb') })

  const disp = (s) => {
    const r = g.row[s], c = g.col[s]
    return flip ? { r: n - 1 - r, c: n - 1 - c } : { r, c }
  }
  const realAt = (r, c) => (flip ? g.at(n - 1 - r, n - 1 - c) : g.at(r, c))
  const place = (node, s) => {
    const d = disp(s)
    node.style.left = `${(d.c * 100) / n}%`
    node.style.top = `${(d.r * 100) / n}%`
  }
  const sideOf = (v) => (v > 0 ? 0 : 1)
  const later = (fn, ms) => { timers.push(setTimeout(fn, ms)) }

  const players = () => api.state()?.players || []

  function buildGrid() {
    grid.replaceChildren()
    cells = []
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        const s = realAt(r, c)
        const cell = h('div', { class: `ck-c${s >= 0 ? ' dk' : ''}` }, s >= 0 ? h('span', { class: 'no' }, s + 1) : null)
        cell.addEventListener('click', () => {
          cur = { r, c }
          const k = clickSquare(s)
          if (k) api.pick(k)
        })
        cells.push(cell)
        grid.append(cell)
      }
    }
  }
  const cellOf = (s) => { const d = disp(s); return cells[d.r * n + d.c] }

  function makePiece(v, s) {
    const node = h('div', { class: `ck-p ${v > 0 ? 'w' : 'b'}${Math.abs(v) === 2 ? ' k' : ''}` }, h('div', { class: 'disc' }))
    place(node, s)
    layer.append(node)
    return node
  }

  function renderPieces(anim) {
    for (const t of timers.splice(0)) clearTimeout(t)
    layer.replaceChildren()
    pieceEls.clear()
    for (let s = 0; s < g.N; s++) if (pos.b[s]) pieceEls.set(s, makePiece(pos.b[s], s))
    shown = Int8Array.from(pos.b)
    shownFlip = flip
    if (!anim || anim.draw) return
    const mover = pieceEls.get(anim.to)
    if (!mover) return
    const total = animDur(g, anim)
    if (anim.promo) mover.classList.remove('k')
    mover.classList.add('moving')
    const frames = []
    let t = 0
    for (let i = 0; i < anim.path.length; i++) {
      if (i) t += segMs(g, anim.path[i - 1], anim.path[i])
      const d = disp(anim.path[i])
      frames.push({ left: `${(d.c * 100) / n}%`, top: `${(d.r * 100) / n}%`, offset: total ? t / total : 1, easing: 'ease-in-out' })
    }
    if (frames.length > 1) mover.animate(frames, { duration: total, fill: 'backwards' })
    later(() => {
      mover.classList.remove('moving')
      if (anim.promo) { mover.classList.add('k', 'crown'); ctx.sfx('pop') }
    }, total)
    let tc = 0
    anim.caps.forEach((s, i) => {
      tc += segMs(g, anim.path[i], anim.path[i + 1])
      const ghost = makePiece(anim.capTypes[i], s)
      later(() => ghost.classList.add('tc'), tc - 60)
      later(() => ghost.classList.add('dead'), total + 40 * i)
      later(() => ghost.remove(), total + 40 * i + 320)
    })
    if (anim.caps.length) later(() => ctx.sfx('eat'), total)
  }

  const prefixOf = (m, partial) => partial.every((s, i) => m.path[i] === s)

  function legal() { return interactive ? K.legalMoves(pos).moves : [] }

  function hint(text, ms = 2600) {
    clearTimeout(hintTimer)
    hintMsg = text
    hintEl.textContent = text
    if (ms) hintTimer = setTimeout(() => { hintMsg = ''; decorate() }, ms)
  }

  function select(s) {
    sel = { from: s, partial: [s] }
    ctx.sfx('click')
    decorate()
  }

  function clickSquare(s) {
    if (!interactive || !pos) return null
    const moves = legal()
    const movable = new Set(moves.map((m) => m.from))
    const own = s >= 0 && pos.b[s] !== 0 && sideOf(pos.b[s]) === pos.p
    if (sel) {
      if (s === sel.from && sel.partial.length === 1) {
        if (movable.size > 1) { sel = null; decorate() }
        return null
      }
      const cands = moves.filter((m) => prefixOf(m, sel.partial))
      const k = sel.partial.length
      const ends = cands.filter((m) => m.to === s && m.path.length > k)
      const nexts = cands.filter((m) => m.path.length - 1 > k && m.path[k] === s)
      if (ends.length === 1) { sel = null; return K.moveKey(ends[0]) }
      if (nexts.length) {
        sel.partial.push(s)
        const c2 = cands.filter((m) => prefixOf(m, sel.partial))
        if (c2.length === 1) { sel = null; return K.moveKey(c2[0]) }
        ctx.sfx('tick')
        hint('Plusieurs suites possibles : cliquez la case suivante.', 0)
        decorate()
        return null
      }
      if (ends.length > 1) {
        ctx.sfx('error')
        hint('Plusieurs chemins mènent à cette case : cliquez d’abord les cases intermédiaires (en pointillés).')
        return null
      }
      if (own && movable.has(s)) { select(s); return null }
      ctx.sfx('error')
      if (sel.partial.length > 1) { sel.partial = [sel.from]; hint('Rafle recommencée depuis la pièce choisie.') }
      else if (movable.size > 1) sel = null
      decorate()
      return null
    }
    if (own) {
      if (movable.has(s)) select(s)
      else {
        ctx.sfx('error')
        hint(moves[0]?.caps.length ? 'Prise obligatoire : une autre pièce doit prendre.' : 'Cette pièce ne peut pas bouger.')
      }
    }
    return null
  }

  function decorate() {
    const moves = legal()
    const must = !!moves[0]?.caps.length
    if (interactive && !sel) {
      const froms = new Set(moves.map((m) => m.from))
      if (froms.size === 1) sel = { from: [...froms][0], partial: [[...froms][0]] }
    }
    const cands = sel ? moves.filter((m) => prefixOf(m, sel.partial)) : []
    const movable = new Set(sel ? [] : moves.map((m) => m.from))
    const tgt = new Set(), step = new Set(), tc = new Set(), via = new Set(sel ? sel.partial.slice(1) : [])
    const k = sel ? sel.partial.length : 0
    for (const m of cands) {
      tgt.add(m.to)
      for (let i = k; i < m.path.length - 1; i++) step.add(m.path[i])
      for (let i = k - 1; i < m.caps.length; i++) if (i >= 0) tc.add(m.caps[i])
    }
    if (sel) for (const m of cands) for (let i = 0; i < k - 1; i++) tc.add(m.caps[i])
    const last = new Set()
    if (lastKey && lastKey !== '=') for (const x of lastKey.split(/[-x]/)) last.add(+x - 1)
    for (let s = 0; s < g.N; s++) {
      const c = cellOf(s)
      c.classList.toggle('tgt', tgt.has(s))
      c.classList.toggle('step', step.has(s))
      c.classList.toggle('via', via.has(s))
      c.classList.toggle('lm', !sel && last.has(s))
    }
    for (const [s, node] of pieceEls) {
      node.classList.toggle('mv', movable.has(s))
      node.classList.toggle('must', must && movable.has(s))
      node.classList.toggle('sel', !!sel && sel.from === s)
      node.classList.toggle('tc', tc.has(s))
      node.classList.toggle('wn', !!over && over.winner >= 0 && sideOf(pos.b[s]) === over.winner)
    }
    el.classList.toggle('locked', !interactive)
    el.classList.toggle('over', !!over)
    if (!hintMsg) {
      let t = ''
      if (interactive && must) {
        const nc = moves[0].caps.length
        t = nc > 1 ? `Rafle obligatoire : ${nc} pièces à prendre.` : 'Prise obligatoire.'
        if (sel && cands.some((m) => m.path.length > k + 1) && new Set(cands.map((m) => m.to)).size < cands.length) t += ' Cliquez les cases intermédiaires.'
      }
      hintEl.textContent = t
    }
    const ps = players()
    drawBtn.classList.toggle('hidden', !(ps.length === 2 && !ps[0].ai && !ps[1].ai) || !!over)
    drawBtn.disabled = !interactive
    placeCursor()
  }

  function renderList() {
    const hist = pos.hist
    if (shownPly === hist.length && listEl.childElementCount) return
    listEl.replaceChildren()
    for (let i = 0; i < hist.length; i += 2) {
      listEl.append(h('div', { class: 'rw' },
        h('span', { class: 'n' }, `${i / 2 + 1}.`),
        h('span', { class: i === hist.length - 1 ? 'lt' : '' }, hist[i]),
        h('span', { class: i + 1 === hist.length - 1 ? 'lt' : '' }, hist[i + 1] || '')))
    }
    countEl.textContent = hist.length ? `${Math.ceil(hist.length / 2)}` : ''
    listEl.scrollTop = listEl.scrollHeight
  }

  function placeCursor() {
    curEl.style.left = `${(cur.c * 100) / n}%`
    curEl.style.top = `${(cur.r * 100) / n}%`
  }

  async function offerDraw() {
    if (!interactive || over) return
    const ps = players()
    const me = ps[pos.p]?.name || SIDE_NAMES[pos.p], other = ps[pos.p ^ 1]?.name || SIDE_NAMES[pos.p ^ 1]
    const ok = await ctx.confirm(`${me} propose la nulle. ${other} accepte-t-il ?`, { ok: 'Accepter la nulle' })
    if (!ok || !interactive || over) return
    pos.offer = true
    api.pick('=')
    pos.offer = false
  }

  function applyFit() {
    if (!fitSize) return
    const [w, hh] = fitSize
    const gap = 12, frame = 14
    const horiz = w - hh >= 140
    let bs
    if (horiz) {
      const sideW = Math.min(220, Math.max(130, w - hh - gap))
      bs = Math.min(hh, w - sideW - gap)
      side.style.width = `${sideW}px`
    } else {
      bs = Math.min(w, hh - 96 - gap)
    }
    const cs = Math.max(18, Math.floor((bs - frame) / n))
    bw.style.setProperty('--cs', `${cs}px`)
    bw.style.setProperty('--n', n)
    const full = cs * n + frame
    el.classList.toggle('vert', !horiz)
    if (horiz) { side.style.height = `${full}px` } else { side.style.width = `${full}px`; side.style.height = '96px' }
  }

  return {
    el,
    reset(p) {
      pos = p; g = p.g; n = p.n
      sel = null; shown = null; flip = null; shownPly = -1
      cur = { r: n - 1, c: 0 }
      applyFit()
    },
    render(p, o) {
      pos = p
      if (p.g !== g) { g = p.g; n = p.n; flip = null; applyFit() }
      over = o.over
      lastKey = o.last
      interactive = !!o.interactive
      const ps = players()
      const nf = ps.length === 2 && ps[0].ai && !ps[1].ai
      if (nf !== flip) { flip = nf; buildGrid() }
      if (shownPly !== p.hist.length) { sel = null; clearTimeout(hintTimer); hintMsg = '' }
      const changed = !shown || shownFlip !== flip || shown.length !== p.b.length || shown.some((v, i) => v !== p.b[i])
      if (o.anim || changed) renderPieces(o.anim && !o.anim.draw && changed ? o.anim : null)
      renderList()
      shownPly = p.hist.length
      if (!interactive) sel = null
      decorate()
    },
    fit(w, hh) { fitSize = [w, hh]; applyFit() },
    moveCursor(dx, dy) {
      if (!el.classList.contains('kb')) { el.classList.add('kb'); return placeCursor() }
      cur = { r: (cur.r + dy + n) % n, c: (cur.c + dx + n) % n }
      placeCursor()
    },
    cursorPick() {
      if (!el.classList.contains('kb')) {
        el.classList.add('kb')
        const s = sel?.from ?? legal()[0]?.from
        if (s != null) cur = disp(s)
        placeCursor()
        return null
      }
      return clickSquare(realAt(cur.r, cur.c))
    },
    destroy() {
      for (const t of timers.splice(0)) clearTimeout(t)
      clearTimeout(hintTimer)
    },
  }
}

function extra(pos, i) {
  const c = K.counts(pos)[i]
  return c.kings ? `${c.men + c.kings} (${c.kings}♛)` : `${c.men}`
}

export default {
  mount(root, ctx) {
    const { h } = ctx
    return mountDuel(root, ctx, {
      id: 'checkers',
      name: 'Dames',
      css: CSS,
      sides: SIDE_NAMES,
      variants: K.VARIANTS,
      swatch: (i) => h('div', { class: `ck-sw ${i ? 'b' : 'w'}` }),
      worker: () => new Worker(new URL('./checkers-ai.js', import.meta.url), { type: 'module' }),
      init: (variant) => K.newPosition(variant),
      apply: (pos, m) => K.applyKey(pos, m),
      replay: (variant, moves) => K.replayKeys(variant, moves),
      result: (pos) => K.result(pos),
      legal: (pos) => (pos.offer ? [...K.legalMoves(pos).keys, '='] : K.legalMoves(pos).keys),
      pass: () => {},
      toMove: (pos) => pos.p,
      extra,
      view: createView,
      animMs: (a) => (a && !a.draw ? animDur(K.geom(a.n || 10), a) + 300 : 0),
      sfx: (a) => (a.draw ? 'line' : a.caps.length > 1 ? 'merge' : a.caps.length ? 'hit' : 'move'),
      endDelay: 1300,
      endText: (pos, res) => {
        const n = Math.ceil(pos.hist.length / 2)
        return `${res.reason || ''}\n${n} coup${n > 1 ? 's' : ''} joué${n > 1 ? 's' : ''}.`
      },
      scoreFor: (pos, hi, level) => {
        const c = K.counts(pos)[hi]
        return { variant: pos.n === 10 ? level : `${level}-8`, score: c.men + c.kings, meta: { level, board: pos.n, moves: Math.ceil(pos.hist.length / 2) } }
      },
      scoreVariants: [...LEVELS.map(([k, l]) => [k, `${l} · 10×10`]), ...LEVELS.map(([k, l]) => [`${k}-8`, `${l} · 8×8`])],
      scoreFmt: (s, meta) => `${s} pièce${s > 1 ? 's' : ''} restante${s > 1 ? 's' : ''}${meta?.moves ? ` · ${meta.moves} coups` : ''}`,
      scoreTitle: 'Victoires contre l’ordinateur (pièces restantes)',
      help: () => h('div', null,
        h('p', null, 'Les pions avancent d’une case en diagonale, vers l’adversaire, sur les cases foncées. Les blancs commencent. ',
          'Variante 10×10 : 20 pions chacun ; variante 8×8 : 12 pions, mêmes règles.'),
        h('p', null, h('b', null, 'Prises : '), 'la prise est obligatoire, en avant comme en arrière, en sautant une pièce adverse vers la case libre juste derrière. ',
          'Si, après une prise, une autre prise est possible, il faut continuer (rafle). Il faut toujours choisir la rafle qui prend le plus de pièces (prise majoritaire, les dames comptent comme les pions). ',
          'Les pièces prises ne sont retirées qu’à la fin de la rafle : on ne peut pas sauter deux fois la même pièce.'),
        h('p', null, h('b', null, 'Dames : '), 'un pion qui termine son coup sur la dernière rangée devient dame (pas s’il ne fait qu’y passer pendant une rafle). ',
          'La dame se déplace et prend à longue distance sur sa diagonale (dame volante) et peut s’arrêter sur n’importe quelle case libre après la pièce prise, sauf si une autre prise impose une case précise.'),
        h('p', null, h('b', null, 'Fin : '), 'un joueur qui ne peut plus jouer (plus de pièces ou pièces bloquées) a perdu. Nulle après 25 coups de dames de chaque côté sans prise ni mouvement de pion, après trois répétitions de la même position, ou d’un commun accord (à deux joueurs).'),
        h('p', null, h('b', null, 'Jouer : '), 'cliquez une pièce (les pièces jouables sont entourées, en orange quand la prise est obligatoire) puis la case d’arrivée (point vert). ',
          'Pour une rafle, cliquez directement la case finale si le chemin est unique ; sinon cliquez les cases intermédiaires (cercles en pointillés) une à une. ',
          'Les cases sont numérotées selon la notation officielle (1 à 50, ou 1 à 32) : « 32-28 » pour un déplacement, « 19x30 » pour une prise.'),
        h('p', { class: 'muted' }, 'Classement : nombre de pièces restantes pour chaque victoire contre l’ordinateur, par niveau et par plateau.')),
    })
  },
}
