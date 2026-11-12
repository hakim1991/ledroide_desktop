import { mountDuel } from './duel.js'

const W = 7, H = 6

const CSS = `
.g-connect4 .c4 { --cs: 60px; position: relative; display: flex; flex-direction: column; align-items: center; }
.g-connect4 .c4-board { display: flex; gap: 0; padding: calc(var(--cs) * .12); border-radius: calc(var(--cs) * .22);
  background: linear-gradient(160deg, #1f4e6b, #163a52 60%, #112d40); box-shadow: 0 10px 30px rgba(0,0,0,.45), inset 0 2px 0 rgba(255,255,255,.08);
  overflow: hidden; touch-action: none; }
.g-connect4 .c4-col { display: flex; flex-direction: column; cursor: pointer; border-radius: calc(var(--cs) * .16); position: relative; }
.g-connect4 .c4.locked .c4-col { cursor: default; }
.g-connect4 .c4:not(.locked) .c4-col:hover, .g-connect4 .c4.kb:not(.locked) .c4-col.cur { background: rgba(255,255,255,.07); }
.g-connect4 .c4.kb:not(.locked) .c4-col.cur { box-shadow: inset 0 0 0 2px rgba(255,255,255,.35); }
.g-connect4 .c4-cell { width: var(--cs); height: var(--cs); position: relative; display: grid; place-items: center; }
.g-connect4 .c4-cell::before { content: ''; position: absolute; width: 78%; height: 78%; border-radius: 50%;
  background: radial-gradient(circle at 50% 35%, #0a1a26, #0d2232); box-shadow: inset 0 3px 6px rgba(0,0,0,.6); }
.g-connect4 .disc { position: absolute; width: 76%; height: 76%; border-radius: 50%; z-index: 1; }
.g-connect4 .disc.s0, .g-connect4 .sw-disc.s0 { background: radial-gradient(circle at 35% 30%, #ff9c8a, #ef5b4c 45%, #b8342a);
  box-shadow: inset 0 -3px 0 rgba(0,0,0,.18), inset 0 0 0 calc(var(--cs) * .07) rgba(255,255,255,.08); }
.g-connect4 .disc.s1, .g-connect4 .sw-disc.s1 { background: radial-gradient(circle at 35% 30%, #fff1a8, #f6c945 45%, #c58f12);
  box-shadow: inset 0 -3px 0 rgba(0,0,0,.18), inset 0 0 0 calc(var(--cs) * .07) rgba(255,255,255,.1); }
.g-connect4 .disc::after { content: ''; position: absolute; inset: 22%; border-radius: 50%; border: 2px solid rgba(255,255,255,.18); }
.g-connect4 .disc.drop { z-index: 2; animation: c4-drop var(--dur, .4s) cubic-bezier(.45, 0, .9, .55) both; }
@keyframes c4-drop {
  0% { transform: translateY(calc(var(--dy) * -1 * var(--cs))) }
  78% { transform: translateY(0) }
  88% { transform: translateY(calc(var(--cs) * -.12)) }
  100% { transform: translateY(0) }
}
.g-connect4 .disc.last::after { border-color: rgba(255,255,255,.75); border-width: 3px; }
.g-connect4 .c4.over .c4-board .disc:not(.wl) { opacity: .45; transition: opacity .5s .3s; }
.g-connect4 .disc.wl { animation: c4-win 1s .35s ease-in-out infinite; }
.g-connect4 .disc.wl.drop { animation: c4-drop var(--dur, .4s) cubic-bezier(.45, 0, .9, .55) both, c4-win 1s .7s ease-in-out infinite; }
@keyframes c4-win { 0%, 100% { filter: brightness(1); transform: scale(1) } 50% { filter: brightness(1.35); transform: scale(1.08) } }
.g-connect4 .c4-pv { height: calc(var(--cs) * .9); width: 100%; display: flex; justify-content: flex-start; padding: 0 calc(var(--cs) * .12); }
.g-connect4 .c4-pv .slot { width: var(--cs); height: 100%; display: grid; place-items: center; position: relative; }
.g-connect4 .c4-pv .disc { width: 70%; height: 70%; opacity: 0; transition: opacity .12s; position: relative; }
.g-connect4 .c4-pv .slot.show .disc { opacity: .9; animation: c4-hover 1.2s ease-in-out infinite; }
@keyframes c4-hover { 0%, 100% { transform: translateY(0) } 50% { transform: translateY(calc(var(--cs) * -.06)) } }
.g-connect4 .sw-disc { width: 20px; height: 20px; border-radius: 50%; --cs: 20px; }
`

function init() {
  return { b: new Array(W * H).fill(-1), hgt: new Array(W).fill(0), n: 0 }
}
function apply(pos, c) {
  const r = pos.hgt[c]
  pos.b[r * W + c] = pos.n & 1
  pos.hgt[c]++
  pos.n++
  return { c, r }
}
function replay(_v, moves) {
  const pos = init()
  for (const c of moves) {
    if (!(c >= 0 && c < W) || pos.hgt[c] >= H) throw new Error('coup invalide')
    apply(pos, c)
  }
  return pos
}
function result(pos) {
  for (let r = 0; r < H; r++) for (let c = 0; c < W; c++) {
    const p = pos.b[r * W + c]
    if (p < 0) continue
    for (const [dr, dc] of [[0, 1], [1, 0], [1, 1], [1, -1]]) {
      const line = [r * W + c]
      let rr = r + dr, cc = c + dc
      while (rr >= 0 && rr < H && cc >= 0 && cc < W && pos.b[rr * W + cc] === p) { line.push(rr * W + cc); rr += dr; cc += dc }
      if (line.length >= 4 && !(r - dr >= 0 && r - dr < H && c - dc >= 0 && c - dc < W && pos.b[(r - dr) * W + c - dc] === p)) {
        return { winner: p, line }
      }
    }
  }
  return pos.n >= W * H ? { winner: -1, line: [] } : null
}
const legal = (pos) => [0, 1, 2, 3, 4, 5, 6].filter((c) => pos.hgt[c] < H)

function createView(ctx, api) {
  const { h } = ctx
  let cursor = 3, hover = -1, cs = 60, pos = null, turn = 0
  const root = h('div', { class: 'c4' })
  const pv = h('div', { class: 'c4-pv' })
  const board = h('div', { class: 'c4-board' })
  root.append(pv, board)
  const slots = [], cols = [], cells = []
  for (let c = 0; c < W; c++) {
    const slot = h('div', { class: 'slot' }, h('div', { class: 'disc' }))
    slots.push(slot); pv.append(slot)
    const col = h('div', { class: 'c4-col', 'data-col': c })
    for (let r = H - 1; r >= 0; r--) {
      const cell = h('div', { class: 'c4-cell' })
      cells[r * W + c] = cell
      col.append(cell)
    }
    col.addEventListener('pointerenter', () => { hover = c; root.classList.remove('kb'); updatePreview() })
    col.addEventListener('pointerleave', () => { if (hover === c) hover = -1; updatePreview() })
    col.addEventListener('pointermove', () => { if (root.classList.contains('kb')) { root.classList.remove('kb'); hover = c; updatePreview() } })
    col.addEventListener('click', () => { cursor = c; api.pick(c) })
    cols.push(col); board.append(col)
  }

  function updatePreview() {
    const kb = root.classList.contains('kb')
    const at = kb ? cursor : hover
    const can = api.canPlay()
    root.classList.toggle('locked', !can)
    slots.forEach((s, c) => {
      s.classList.toggle('show', can && c === at && pos && pos.hgt[c] < H)
      s.firstChild.className = `disc s${turn}`
    })
    cols.forEach((col, c) => col.classList.toggle('cur', c === cursor))
  }

  return {
    el: root,
    reset() { cursor = 3 },
    render(p, o) {
      pos = p
      turn = o.turn < 0 ? 0 : o.turn
      const line = new Set(o.over?.line || [])
      const lastIdx = o.last != null ? (p.hgt[o.last] - 1) * W + o.last : -1
      root.classList.toggle('over', !!o.over && o.over.winner >= 0)
      for (let i = 0; i < W * H; i++) {
        const v = p.b[i], cell = cells[i]
        const cur = cell.firstChild
        if (v < 0) { cur?.remove(); continue }
        let d = cur
        if (!d || !d.classList.contains(`s${v}`) || (o.anim && i === lastIdx)) {
          d = h('div', { class: `disc s${v}` })
          cell.replaceChildren(d)
          if (o.anim && i === lastIdx) {
            const dy = H - o.anim.r + 0.4
            d.classList.add('drop')
            d.style.setProperty('--dy', dy)
            d.style.setProperty('--dur', `${Math.round(180 + 55 * dy)}ms`)
          }
        }
        d.classList.toggle('last', i === lastIdx && !o.over)
        d.classList.toggle('wl', line.has(i))
      }
      updatePreview()
    },
    fit(w, hh) {
      cs = Math.max(30, Math.floor(Math.min((w - 8) / (W + 0.3), (hh - 8) / (H + 1.25))))
      root.style.setProperty('--cs', `${cs}px`)
    },
    moveCursor(dx) {
      if (!dx) return
      root.classList.add('kb')
      cursor = (cursor + dx + W) % W
      updatePreview()
    },
    cursorPick() {
      if (!root.classList.contains('kb')) { root.classList.add('kb'); updatePreview(); return null }
      return cursor
    },
  }
}

export default {
  mount(root, ctx) {
    return mountDuel(root, ctx, {
      id: 'connect4',
      name: 'Quatre en ligne',
      css: CSS,
      sides: ['Corail', 'Ambre'],
      swatch: (i) => ctx.h('div', { class: `sw-disc s${i}` }),
      worker: () => new Worker(new URL('./connect4-ai.js', import.meta.url), { type: 'module' }),
      init, apply, replay, result, legal,
      toMove: (pos) => pos.n & 1,
      view: createView,
      animMs: (a) => (a ? 180 + 55 * (H - a.r + 0.4) : 0),
      sfx: () => 'drop',
      endDelay: 1100,
      help: () => ctx.h('div', null,
        ctx.h('p', null, 'Chaque joueur fait tomber à tour de rôle un jeton dans une colonne : il descend jusqu’à la case libre la plus basse.'),
        ctx.h('p', null, 'Le premier qui aligne quatre jetons de sa couleur — horizontalement, verticalement ou en diagonale — gagne. Grille pleine sans alignement : match nul.'),
        ctx.h('p', null, 'Le joueur qui commence joue les jetons corail. Astuce : la colonne centrale offre le plus de possibilités.')),
    })
  },
}
