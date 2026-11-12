import { mountDuel, LEVELS } from './duel.js'
import { Game, Position, parsePgn, parseSq, sqName, SQUARES, START_FEN, PAWN, KING } from './chess-core.js'

const GLYPH = ['', '♟', '♞', '♝', '♜', '♛', '♚'].map((g) => (g ? g + '︎' : ''))
const PIECE_VALUE = [0, 1, 3, 3, 5, 9, 0]
const REASON = {
  mate: 'Échec et mat.',
  stalemate: 'Pat : le camp au trait n’a aucun coup légal et n’est pas en échec.',
  fifty: 'Règle des 50 coups : ni prise ni mouvement de pion depuis 50 coups.',
  repetition: 'Répétition : la même position est apparue trois fois.',
  material: 'Matériel insuffisant pour mater.',
}

const CSS = `
.g-chess .ch { display: flex; gap: 12px; align-items: stretch; justify-content: center; }
.g-chess .ch.col { flex-direction: column; align-items: center; }
.g-chess .ch-bw { --sq: 56px; position: relative; width: calc(var(--sq) * 8); height: calc(var(--sq) * 8); flex-shrink: 0;
  border-radius: 6px; box-shadow: 0 10px 30px rgba(0,0,0,.5), 0 0 0 3px #2b2118; touch-action: none; overflow: hidden; }
.g-chess .ch-grid { display: grid; grid-template-columns: repeat(8, var(--sq)); grid-auto-rows: var(--sq); }
.g-chess .ch-sq { position: relative; background: #d9c7a5; }
.g-chess .ch-sq.d { background: #9a7350; }
.g-chess .ch-sq.last { background-image: linear-gradient(rgba(250,204,21,.38), rgba(250,204,21,.38)); }
.g-chess .ch-sq.sel { background-image: linear-gradient(rgba(99,102,241,.55), rgba(99,102,241,.55)); }
.g-chess .ch-sq.chk { background-image: radial-gradient(circle, rgba(239,68,68,.95) 0%, rgba(239,68,68,.55) 40%, rgba(239,68,68,0) 72%); }
.g-chess .ch-sq.tgt::after { content: ''; position: absolute; left: 50%; top: 50%; width: 30%; height: 30%; transform: translate(-50%, -50%);
  border-radius: 50%; background: rgba(20,20,30,.32); pointer-events: none; }
.g-chess .ch-sq.tgt.cap::after { width: 92%; height: 92%; background: transparent; box-shadow: inset 0 0 0 calc(var(--sq) * .07) rgba(20,20,30,.35); }
.g-chess .ch-sq.tgt.hov { background-image: linear-gradient(rgba(99,102,241,.35), rgba(99,102,241,.35)); }
.g-chess .ch-bw.kb .ch-sq.cur { box-shadow: inset 0 0 0 3px #fde68a, inset 0 0 0 5px rgba(0,0,0,.35); }
.g-chess .ch-co { position: absolute; font-size: calc(var(--sq) * .19); font-weight: 700; pointer-events: none; line-height: 1; }
.g-chess .ch-co.r { left: 3px; top: 3px; } .g-chess .ch-co.f { right: 3px; bottom: 2px; }
.g-chess .ch-sq .ch-co { color: #9a7350; } .g-chess .ch-sq.d .ch-co { color: #e9dcc3; }
.g-chess .ch-layer { position: absolute; inset: 0; pointer-events: none; }
.g-chess .ch-pc { position: absolute; left: 0; top: 0; width: var(--sq); height: var(--sq); display: grid; place-items: center;
  font-family: 'Segoe UI Symbol', 'Noto Sans Symbols 2', 'DejaVu Sans', 'Arial Unicode MS', serif; font-size: calc(var(--sq) * .8);
  line-height: 1; transition: transform .2s ease; will-change: transform; }
.g-chess .ch-pc span { display: block; transform: translateY(-3%); }
.g-chess .ch-pc.w { color: #fbf8f0; text-shadow: 0 0 1px #111, 1px 0 0 #1a1a1a, -1px 0 0 #1a1a1a, 0 1px 0 #1a1a1a, 0 -1px 0 #1a1a1a, 0 3px 4px rgba(0,0,0,.45); }
.g-chess .ch-pc.b { color: #1c1b20; text-shadow: 1px 0 0 rgba(255,255,255,.28), -1px 0 0 rgba(255,255,255,.28), 0 -1px 0 rgba(255,255,255,.28), 0 1px 0 rgba(255,255,255,.18), 0 3px 4px rgba(0,0,0,.5); }
.g-chess .ch-pc.drag { transition: none; z-index: 5; filter: drop-shadow(0 8px 8px rgba(0,0,0,.45)); }
.g-chess .ch-pc.drag span { transform: translateY(-3%) scale(1.12); }
.g-chess .ch-pc.lift { opacity: .35; }
.g-chess .ch-pc.ghost { animation: ch-fade .25s ease forwards; }
@keyframes ch-fade { to { opacity: 0; transform: var(--t) scale(.6); } }
.g-chess .ch-bw.over .ch-pc:not(.win) { opacity: .8; }
.g-chess .ch-promo-back { position: absolute; inset: 0; background: rgba(10,10,14,.45); z-index: 10; }
.g-chess .ch-promo { position: absolute; width: var(--sq); display: flex; flex-direction: column; background: #f4efe4; border-radius: 6px;
  box-shadow: 0 8px 22px rgba(0,0,0,.6); overflow: hidden; z-index: 11; animation: ch-pop .15s ease-out; }
.g-chess .ch-promo.up { flex-direction: column-reverse; }
.g-chess .ch-promo button { all: unset; cursor: pointer; width: var(--sq); height: var(--sq); position: relative; }
.g-chess .ch-promo button .ch-pc { transition: none; }
.g-chess .ch-promo button:hover, .g-chess .ch-promo button.cur { background: #c7d2fe; }
@keyframes ch-pop { from { transform: scale(.85); opacity: 0 } }
.g-chess .ch-side { display: flex; flex-direction: column; gap: 8px; min-width: 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 8px; }
.g-chess .ch.col .ch-side { flex-direction: row; align-items: stretch; }
.g-chess .ch-moves { flex: 1; min-height: 0; overflow-y: auto; font-variant-numeric: tabular-nums; font-size: 13px; }
.g-chess .ch-moves table { width: 100%; border-collapse: collapse; }
.g-chess .ch-moves td { padding: 2px 4px; white-space: nowrap; }
.g-chess .ch-moves td.n { color: var(--muted); width: 1%; text-align: right; padding-right: 6px; }
.g-chess .ch-moves tr:nth-child(odd) td { background: rgba(255,255,255,.025); }
.g-chess .ch-moves td.cur { background: rgba(99,102,241,.28) !important; border-radius: 4px; font-weight: 600; }
.g-chess .ch-moves .empty { color: var(--muted); padding: 6px 4px; }
.g-chess .ch-tools { display: flex; flex-wrap: wrap; gap: 6px; }
.g-chess .ch.col .ch-tools { flex-direction: column; }
.g-chess .ch-tools button { flex: 1; padding: 4px 8px; }
.g-chess .ch-cap { display: flex; align-items: center; gap: 0; font-size: 15px; font-weight: 400; line-height: 1;
  font-family: 'Segoe UI Symbol', 'Noto Sans Symbols 2', 'DejaVu Sans', serif; color: var(--text); max-width: 150px; flex-wrap: wrap; justify-content: flex-end; }
.g-chess .ch-cap i { font-style: normal; margin-left: -2px; }
.g-chess .ch-cap i.w { color: #fbf8f0; text-shadow: 1px 0 0 #111, -1px 0 0 #111, 0 1px 0 #111, 0 -1px 0 #111; }
.g-chess .ch-cap i.b { color: #3f3f48; text-shadow: 1px 0 0 #b4b4bd, -1px 0 0 #b4b4bd, 0 1px 0 #b4b4bd, 0 -1px 0 #b4b4bd; }
.g-chess .ch-cap b { font-family: system-ui, sans-serif; font-size: 12px; margin-left: 6px; color: var(--ok); font-weight: 700; }
.g-chess .sw-k { font-family: 'Segoe UI Symbol', 'Noto Sans Symbols 2', 'DejaVu Sans', serif; font-size: 22px; line-height: 1; }
.g-chess .sw-k.w { color: #fbf8f0; text-shadow: 1px 0 0 #111, -1px 0 0 #111, 0 1px 0 #111, 0 -1px 0 #111; }
.g-chess .sw-k.b { color: #1c1b20; text-shadow: 1px 0 0 #aaa, -1px 0 0 #aaa, 0 1px 0 #aaa, 0 -1px 0 #aaa; }
.g-chess-io .row { margin-bottom: 12px; } .g-chess-io label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }
.g-chess-io .line { display: flex; gap: 6px; } .g-chess-io input, .g-chess-io textarea { flex: 1; width: 100%; font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.g-chess-io textarea { resize: vertical; min-height: 70px; }
`

const pieceEl = (h, p) => h('div', { class: `ch-pc ${p > 0 ? 'w' : 'b'}` }, h('span', null, GLYPH[Math.abs(p)]))

function copyText(ctx, text) {
  const done = () => ctx.toast('Copié dans le presse-papiers.', 'ok')
  const fallback = () => {
    const ta = ctx.h('textarea', { style: { position: 'fixed', left: '-9999px' } })
    ta.value = text
    document.body.append(ta)
    ta.select()
    try { document.execCommand('copy'); done() } catch { ctx.toast('Copie impossible.', 'err') }
    ta.remove()
  }
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, fallback)
  else fallback()
}

function humanColor(S) {
  if (!S || S.mode !== 'ai') return 0
  const i = S.players.findIndex((p) => !p.ai)
  return i < 0 ? 0 : i
}

function pgnHeaders(S) {
  const d = new Date()
  return {
    Event: 'Partie amicale', Site: 'Mini-jeux',
    Date: `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`,
    White: S?.players[0]?.name || '?', Black: S?.players[1]?.name || '?',
  }
}

function createView(ctx, api) {
  const { h } = ctx
  const wrap = h('div', { class: 'ch' })
  const bw = h('div', { class: 'ch-bw' })
  const grid = h('div', { class: 'ch-grid' })
  const layer = h('div', { class: 'ch-layer' })
  bw.append(grid, layer)
  const movesEl = h('div', { class: 'ch-moves' })
  const flipBtn = h('button', { title: 'Retourner l’échiquier (F)', onclick: () => { manualFlip = !manualFlip; redraw() } }, '⇅ Retourner')
  const ioBtn = h('button', { title: 'Exporter / importer une position (FEN) ou une partie (PGN)', onclick: () => openIO() }, '⇄ FEN / PGN')
  const side = h('div', { class: 'ch-side' }, movesEl, h('div', { class: 'ch-tools' }, flipBtn, ioBtn))
  wrap.append(bw, side)

  let game = null, opts = {}, flipped = false, manualFlip = false
  let sel = -1, cur = 12, kb = false, drag = null, promo = null
  let sqEls = new Map()
  let sqSize = 56

  const interactive = () => !!opts.interactive && api.canPlay()
  const legalFrom = (sq) => {
    if (!game || sq < 0) return []
    const name = sqName(sq)
    return game.legal().filter((u) => u.startsWith(name))
  }
  const pieceAt = (sq) => (sq >= 0 ? game.pos.b[sq] : 0)
  const own = (sq) => { const p = pieceAt(sq); return p && (p > 0 ? 0 : 1) === game.side }
  const xy = (sq) => {
    const f = sq & 7, r = sq >> 4
    return [(flipped ? 7 - f : f) * sqSize, (flipped ? r : 7 - r) * sqSize]
  }
  const sqAt = (clientX, clientY) => {
    const rect = bw.getBoundingClientRect()
    const c = Math.floor(((clientX - rect.left) / rect.width) * 8), rr = Math.floor(((clientY - rect.top) / rect.height) * 8)
    if (c < 0 || c > 7 || rr < 0 || rr > 7) return -1
    return (flipped ? rr : 7 - rr) * 16 + (flipped ? 7 - c : c)
  }

  function buildGrid() {
    grid.replaceChildren()
    sqEls = new Map()
    for (let row = 0; row < 8; row++) {
      for (let col = 0; col < 8; col++) {
        const f = flipped ? 7 - col : col, r = flipped ? row : 7 - row, sq = r * 16 + f
        const el = h('div', { class: `ch-sq${(f + r) % 2 === 0 ? ' d' : ''}`, 'data-sq': sqName(sq) })
        if (col === 0) el.append(h('span', { class: 'ch-co r' }, String(r + 1)))
        if (row === 7) el.append(h('span', { class: 'ch-co f' }, 'abcdefgh'[f]))
        grid.append(el)
        sqEls.set(sq, el)
      }
    }
  }

  function place(el, sq) { const [x, y] = xy(sq); el.style.transform = `translate(${x}px, ${y}px)` }

  function drawPieces(anim) {
    layer.replaceChildren()
    const movers = []
    for (const sq of SQUARES) {
      const p = game.pos.b[sq]
      if (!p) continue
      const el = pieceEl(h, p)
      el.dataset.sq = sqName(sq)
      if (opts.over && opts.over.winner >= 0 && Math.abs(p) === KING && (p > 0 ? 0 : 1) === opts.over.winner) el.classList.add('win')
      if (anim && sq === anim.to) { place(el, anim.from); movers.push([el, sq]) }
      else if (anim && anim.rook && sq === anim.rook[1]) { place(el, anim.rook[0]); movers.push([el, sq]) }
      else place(el, sq)
      layer.append(el)
    }
    if (anim && anim.captured) {
      const g = pieceEl(h, anim.captured)
      place(g, anim.capSq)
      g.style.setProperty('--t', g.style.transform)
      g.classList.add('ghost')
      layer.prepend(g)
    }
    if (movers.length) {
      layer.getBoundingClientRect()
      for (const [el, sq] of movers) place(el, sq)
    }
  }

  function decorate() {
    const last = opts.last ? [parseSq(opts.last.slice(0, 2)), parseSq(opts.last.slice(2, 4))] : []
    const targets = new Map()
    if (sel >= 0) for (const u of legalFrom(sel)) targets.set(parseSq(u.slice(2, 4)), true)
    const checkSq = game.pos.inCheck() ? game.pos.kings[game.side] : -1
    for (const [sq, el] of sqEls) {
      el.classList.toggle('last', last.includes(sq))
      el.classList.toggle('sel', sq === sel)
      el.classList.toggle('chk', sq === checkSq)
      el.classList.toggle('tgt', targets.has(sq))
      el.classList.toggle('cap', targets.has(sq) && (!!pieceAt(sq) || (Math.abs(pieceAt(sel)) === PAWN && (sq & 7) !== (sel & 7))))
      el.classList.toggle('cur', sq === cur)
      el.classList.toggle('hov', !!drag?.moved && drag.over === sq && targets.has(sq))
    }
    bw.classList.toggle('kb', kb)
    bw.classList.toggle('over', !!opts.over)
  }

  function renderMoves() {
    const sans = game.sans
    if (!sans.length) {
      movesEl.replaceChildren(h('div', { class: 'empty' }, game.custom ? 'Position importée.' : 'Aucun coup joué.'))
      return
    }
    const rows = []
    let n = game.startFull, i = 0
    if (game.startSide === 1) {
      rows.push(h('tr', null, h('td', { class: 'n' }, `${n}.`), h('td', null, '…'), h('td', { class: sans.length === 1 ? 'cur' : '' }, sans[0])))
      i = 1; n++
    }
    for (; i < sans.length; i += 2, n++) {
      rows.push(h('tr', null, h('td', { class: 'n' }, `${n}.`),
        h('td', { class: i === sans.length - 1 ? 'cur' : '' }, sans[i]),
        h('td', { class: i + 1 === sans.length - 1 ? 'cur' : '' }, sans[i + 1] || '')))
    }
    movesEl.replaceChildren(h('table', null, h('tbody', null, rows)))
    movesEl.scrollTop = movesEl.scrollHeight
  }

  function redraw(anim = null) {
    if (!game) return
    const S = api.state()
    const want = (humanColor(S) === 1) !== manualFlip
    if (want !== flipped || !sqEls.size) { flipped = want; buildGrid(); anim = null }
    drawPieces(anim)
    decorate()
  }

  function closePromo() { promo?.el.remove(); promo?.back.remove(); promo = null }
  function cancelPromo() { closePromo(); sel = -1; redraw() }

  function openPromo(from, to) {
    closePromo()
    const [x] = xy(to)
    const top = (flipped ? (to >> 4) : 7 - (to >> 4)) === 0
    const back = h('div', { class: 'ch-promo-back', onpointerdown: (e) => { e.stopPropagation(); cancelPromo() } })
    const sgn = game.side ? -1 : 1
    const choices = ['q', 'n', 'r', 'b']
    const el = h('div', { class: `ch-promo${top ? '' : ' up'}`, style: { left: `${x}px`, [top ? 'top' : 'bottom']: '0px' } },
      choices.map((c, k) => {
        const b = h('button', { title: { q: 'Dame', n: 'Cavalier', r: 'Tour', b: 'Fou' }[c], 'data-p': c,
          onpointerdown: (e) => e.stopPropagation(),
          onclick: (e) => { e.stopPropagation(); choose(k) } }, pieceEl(h, sgn * (' pnbrqk'.indexOf(c))))
        b.firstChild.style.transform = 'none'
        return b
      }))
    const choose = (k) => {
      const u = sqName(from) + sqName(to) + choices[k]
      closePromo(); sel = -1
      api.pick(u)
    }
    promo = { el, back, k: 0, choose, from, to }
    bw.append(back, el)
    markPromo()
  }
  function markPromo() { if (promo) [...promo.el.children].forEach((b, i) => b.classList.toggle('cur', kb && i === promo.k)) }

  function tryMove(from, to) {
    const u = sqName(from) + sqName(to)
    const list = game.legal().filter((x) => x.startsWith(u))
    if (!list.length) return false
    if (list.length > 1) { openPromo(from, to); return true }
    sel = -1
    api.pick(list[0])
    return true
  }

  function select(sq) { sel = sq; decorate(); if (sq >= 0) ctx.sfx('tick') }

  bw.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || promo) return
    if (kb) { kb = false }
    const sq = sqAt(e.clientX, e.clientY)
    if (sq < 0 || !game) return
    cur = sq
    if (!interactive()) { decorate(); return }
    if (sel >= 0 && sq !== sel && legalFrom(sel).some((u) => u.slice(2, 4) === sqName(sq))) {
      tryMove(sel, sq)
      return
    }
    if (own(sq)) {
      if (!legalFrom(sq).length) { ctx.sfx('error'); select(-1); return }
      const el = layer.querySelector(`.ch-pc[data-sq="${sqName(sq)}"]`)
      drag = { sq, el, moved: false, x: e.clientX, y: e.clientY, over: sq, wasSel: sel === sq, id: e.pointerId }
      bw.setPointerCapture(e.pointerId)
      select(sq)
      return
    }
    select(-1)
  })
  bw.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return
    if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return
    drag.moved = true
    const rect = bw.getBoundingClientRect(), k = rect.width / (sqSize * 8)
    const x = (e.clientX - rect.left) / k - sqSize / 2, y = (e.clientY - rect.top) / k - sqSize / 2
    if (drag.el) { drag.el.classList.add('drag'); drag.el.style.transform = `translate(${x}px, ${y}px)` }
    const over = sqAt(e.clientX, e.clientY)
    if (over !== drag.over) { drag.over = over; decorate() }
  })
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return
    const d = drag
    drag = null
    try { bw.releasePointerCapture(e.pointerId) } catch { /* released */ }
    if (!d.moved) {
      if (d.wasSel) select(-1)
      return
    }
    const to = e.type === 'pointerup' ? sqAt(e.clientX, e.clientY) : -1
    if (to >= 0 && to !== d.sq && interactive() && tryMove(d.sq, to)) {
      if (d.el && !promo) d.el.classList.remove('drag')
      if (promo && d.el) { d.el.classList.remove('drag'); place(d.el, to) }
      return
    }
    if (d.el) { d.el.classList.remove('drag'); place(d.el, d.sq) }
    decorate()
  }
  bw.addEventListener('pointerup', endDrag)
  bw.addEventListener('pointercancel', endDrag)

  const onKey = (e) => {
    if (document.querySelector('.dlg-back') || e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
    if (e.code === 'KeyF' && !e.ctrlKey && !e.metaKey && !e.altKey) { manualFlip = !manualFlip; redraw() }
    if (e.key === 'Escape' && promo) { e.stopPropagation(); cancelPromo() }
  }
  document.addEventListener('keydown', onKey, true)

  async function openIO() {
    if (!game) return
    const S = api.state()
    const fen = game.pos.fen()
    const pgn = game.pgn(pgnHeaders(S))
    const fenIn = h('input', { readonly: true, value: fen })
    const pgnIn = h('textarea', { readonly: true, rows: 5 })
    pgnIn.value = pgn
    const imp = h('textarea', { rows: 5, placeholder: 'Collez ici une position FEN ou une partie PGN…' })
    const body = h('div', { class: 'g-chess-io' },
      h('div', { class: 'row' }, h('label', null, 'Position actuelle (FEN)'),
        h('div', { class: 'line' }, fenIn, h('button', { onclick: () => copyText(ctx, fen) }, 'Copier'))),
      h('div', { class: 'row' }, h('label', null, 'Partie (PGN)'),
        h('div', { class: 'line' }, pgnIn, h('button', { onclick: () => copyText(ctx, pgn) }, 'Copier'))),
      h('div', { class: 'row' }, h('label', null, 'Importer (la partie importée n’est pas comptée dans les statistiques)'), imp))
    const v = await ctx.dialog({
      title: 'Échanger une position ou une partie', body, wide: true,
      buttons: [{ label: 'Fermer', value: null }, { label: 'Importer', value: () => imp.value, primary: true }],
    })
    const text = (v || '').trim()
    if (!text) return
    try {
      let start = null, moves = []
      if (!/\[|\d+\s*\./.test(text) && /^[1-8pnbrqkPNBRQK]+(\/[1-8pnbrqkPNBRQK]+){7}(\s|$)/.test(text)) {
        start = Position.fromFen(text).fen()
        if (start === START_FEN) start = null
      } else {
        const r = parsePgn(text)
        start = r.start; moves = r.ucis
        if (!moves.length && !start) throw new Error('Aucun coup reconnu dans ce texte.')
      }
      const g = Game.replay(start, moves)
      const ok = await api.load({ start, moves, first: api.state()?.mode === 'ai' ? (g.side === 0 ? 'human' : 'ai') : 'human' })
      if (ok !== false) ctx.toast(moves.length ? `Partie importée (${moves.length} demi-coups).` : 'Position importée.', 'ok')
    } catch (e) { ctx.toast(e.message, 'err') }
  }

  return {
    el: wrap,
    reset() { sel = -1; manualFlip = false; closePromo(); drag = null },
    render(p, o) {
      game = p
      opts = o
      if (promo) closePromo()
      if (!interactive()) sel = -1
      if (sel >= 0 && !own(sel)) sel = -1
      redraw(o.anim)
      renderMoves()
    },
    fit(w, hh) {
      const sideBySide = w - hh >= 150
      wrap.classList.toggle('col', !sideBySide)
      let board
      if (sideBySide) {
        const panel = Math.max(150, Math.min(230, w - hh - 12))
        board = Math.min(hh - 6, w - panel - 12 - 6)
        side.style.width = `${panel}px`; side.style.height = `${board}px`
      } else {
        board = Math.min(w - 6, hh - 96)
        side.style.width = `${board}px`; side.style.height = '84px'
      }
      sqSize = Math.max(28, Math.floor(board / 8))
      bw.style.setProperty('--sq', `${sqSize}px`)
      if (game) redraw()
    },
    moveCursor(dx, dy) {
      if (promo) {
        kb = true
        const dir = promo.el.classList.contains('up') ? -dy : dy
        promo.k = (promo.k + dir + 4) % 4
        markPromo()
        return
      }
      if (!kb) { kb = true; if (sel >= 0) cur = sel; decorate(); return }
      const f = cur & 7, r = cur >> 4
      const nf = Math.min(7, Math.max(0, f + (flipped ? -dx : dx))), nr = Math.min(7, Math.max(0, r + (flipped ? dy : -dy)))
      cur = nr * 16 + nf
      decorate()
    },
    cursorPick() {
      if (promo) { if (kb) promo.choose(promo.k); else { kb = true; markPromo() } return null }
      if (!kb) { kb = true; decorate(); return null }
      if (!game || !interactive()) return null
      if (sel >= 0 && cur !== sel && legalFrom(sel).some((u) => u.slice(2, 4) === sqName(cur))) {
        const u = sqName(sel) + sqName(cur)
        const list = game.legal().filter((x) => x.startsWith(u))
        if (list.length > 1) { openPromo(sel, cur); markPromo(); return null }
        sel = -1
        return list[0]
      }
      if (own(cur) && legalFrom(cur).length) select(cur === sel ? -1 : cur)
      else { if (sel >= 0) select(-1); ctx.sfx('error') }
      return null
    },
    destroy() { document.removeEventListener('keydown', onKey, true) },
  }
}

function capturedEl(h, pos, i) {
  const caps = [...pos.captured[i]].sort((a, b) => PIECE_VALUE[b] - PIECE_VALUE[a] || b - a)
  const adv = pos.material() * (i === 0 ? 1 : -1)
  if (!caps.length && adv <= 0) return null
  const sgn = i === 0 ? -1 : 1
  return h('span', { class: 'ch-cap', title: 'Pièces capturées et avantage matériel' },
    caps.map((t) => h('i', { class: `${sgn > 0 ? 'w' : 'b'}` }, GLYPH[t])),
    adv > 0 ? h('b', null, `+${adv}`) : null)
}

function humanMoves(pos, color) {
  let n = 0
  for (let k = 0; k < pos.ucis.length; k++) if (((pos.startSide + k) & 1) === color) n++
  return n
}

export default {
  mount(root, ctx) {
    const { h } = ctx
    return mountDuel(root, ctx, {
      id: 'chess',
      name: 'Échecs',
      css: CSS,
      sides: ['Blancs', 'Noirs'],
      firstLabel: 'Qui joue les Blancs ?',
      swatch: (i) => h('span', { class: `sw-k ${i ? 'b' : 'w'}` }, GLYPH[KING]),
      worker: () => new Worker(new URL('./chess-ai.js', import.meta.url), { type: 'module' }),
      init: (_v, start) => new Game(start || null),
      replay: (_v, moves, start) => Game.replay(start || null, moves),
      apply: (pos, m) => pos.play(m),
      result: (pos) => pos.result(),
      legal: (pos) => pos.legal(),
      pass: () => {},
      toMove: (pos) => pos.side,
      note: (pos) => (pos.pos.inCheck() && pos.legal().length ? 'Échec !' : ''),
      extra: (pos, i) => capturedEl(h, pos, i),
      view: createView,
      animMs: (a) => (a ? 260 : 0),
      sfx: (a) => (!a ? 'move' : a.check ? 'flag' : a.promo ? 'merge' : a.captured ? 'hit' : a.rook ? 'rotate' : 'move'),
      endDelay: 1100,
      endText: (pos, res) => REASON[res.reason] || '',
      scoreFor: (pos, hi, level) => {
        if (pos.custom) return null
        const n = humanMoves(pos, hi)
        return { variant: level, score: Math.max(1, 1000 - n), meta: { level, moves: n } }
      },
      scoreVariants: LEVELS,
      scoreFmt: (s, meta) => `Mat en ${meta?.moves ?? 1000 - s} coups`,
      scoreTitle: 'Victoires contre l’ordinateur (mat le plus rapide)',
      help: () => h('div', null,
        h('p', null, 'Mettez le roi adverse en échec et mat. Les Blancs commencent ; en mode « contre l’ordinateur », l’échiquier est retourné automatiquement si vous jouez les Noirs.'),
        h('ul', null,
          h('li', null, h('b', null, 'Roque : '), 'déplacez le roi de deux cases vers la tour. Impossible si le roi ou la tour a déjà bougé, si le roi est en échec ou s’il traverse une case attaquée.'),
          h('li', null, h('b', null, 'Prise en passant : '), 'un pion qui avance de deux cases peut être pris par un pion adverse voisin, comme s’il n’avait avancé que d’une case — uniquement au coup suivant.'),
          h('li', null, h('b', null, 'Promotion : '), 'un pion qui atteint la dernière rangée devient la pièce de votre choix (dame, tour, fou ou cavalier).'),
          h('li', null, h('b', null, 'Nulles : '), 'pat (aucun coup légal sans être en échec), triple répétition de la position, 50 coups sans prise ni mouvement de pion, matériel insuffisant pour mater.')),
        h('p', null, 'Jouez en cliquant la pièce puis la case d’arrivée, ou en la faisant glisser. Les points indiquent les coups légaux ; le dernier coup est surligné, un roi en échec apparaît en rouge. ',
          h('span', { class: 'kbd' }, 'F'), ' retourne l’échiquier. Au clavier/manette : déplacez le curseur, ', h('span', { class: 'kbd' }, 'Espace'), ' pour choisir la pièce puis la case.'),
        h('p', null, 'Le bouton « FEN / PGN » copie la position ou la partie (notation algébrique) et permet d’en importer une : vous prenez alors le camp au trait.'),
        h('p', { class: 'muted' }, 'Niveaux : Facile (calcul très court, erreurs fréquentes), Moyen (quelques coups d’avance), Difficile (recherche approfondie d’environ une seconde). Classement : nombre de coups pour mater l’ordinateur, par niveau.')),
    })
  },
}
