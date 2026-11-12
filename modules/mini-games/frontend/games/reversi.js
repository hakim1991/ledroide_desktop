import { mountDuel, LEVELS } from './duel.js'

const DIRS = [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1]]
const FACE = {
  0: 'radial-gradient(circle at 35% 30%, #5a5a66, #22222a 55%, #0b0b0f)',
  1: 'radial-gradient(circle at 35% 30%, #ffffff, #e4e4ea 55%, #a9a9b6)',
}

const CSS = `
.g-reversi .rv { --cs: 60px; position: relative; padding: calc(var(--cs) * .38); border-radius: calc(var(--cs) * .22);
  background: linear-gradient(150deg, #3b2a1d, #2a1d13); box-shadow: 0 10px 30px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.08); }
.g-reversi .rv-grid { display: grid; grid-template-columns: repeat(8, var(--cs)); grid-auto-rows: var(--cs); gap: 1px; background: #0d3b2c;
  border: 1px solid #0d3b2c; touch-action: none; }
.g-reversi .rv-cell { background: radial-gradient(circle at 50% 40%, #1e7a57, #17654a); position: relative; cursor: default;
  display: grid; place-items: center; perspective: 300px; }
.g-reversi .rv-cell.dot::before { content: ''; position: absolute; left: 50%; top: 50%; width: 7px; height: 7px; margin: -3.5px; border-radius: 50%; background: #0d3b2c; z-index: 0; }
.g-reversi .rv-coord { position: absolute; color: #b79a7a; font-size: calc(var(--cs) * .2); font-weight: 600; display: grid; place-items: center; pointer-events: none; }
.g-reversi .rv-cell.lg { cursor: pointer; }
.g-reversi .rv-cell.lg::after { content: ''; position: absolute; width: 22%; height: 22%; border-radius: 50%; background: var(--hint); opacity: .55; transition: .12s; }
.g-reversi .rv.nohint .rv-cell.lg::after { opacity: 0; }
.g-reversi .rv:not(.locked) .rv-cell.lg:hover::after, .g-reversi .rv.kb:not(.locked) .rv-cell.lg.cur::after { width: 78%; height: 78%; opacity: .45; background: var(--ghost); }
.g-reversi .rv.t0 { --hint: rgba(10,10,14,.75); --ghost: ${FACE[0]}; } .g-reversi .rv.t1 { --hint: rgba(255,255,255,.7); --ghost: ${FACE[1]}; }
.g-reversi .rv.kb .rv-cell.cur { box-shadow: inset 0 0 0 2px rgba(255,236,170,.75); z-index: 1; }
.g-reversi .rv.locked .rv-cell.lg::after { opacity: 0; }
.g-reversi .disc { width: 80%; height: 80%; border-radius: 50%; position: relative; z-index: 1;
  box-shadow: 0 2px 3px rgba(0,0,0,.45), inset 0 -2px 2px rgba(0,0,0,.25); }
.g-reversi .disc.c0, .g-reversi .sw-disc.c0 { background: ${FACE[0]}; }
.g-reversi .disc.c1, .g-reversi .sw-disc.c1 { background: ${FACE[1]}; }
.g-reversi .disc.place { animation: rv-place .28s cubic-bezier(.3, 1.6, .5, 1) both; }
@keyframes rv-place { from { transform: scale(.3); opacity: 0 } }
.g-reversi .disc.flip.c0 { animation: rv-to0 .42s ease-in-out var(--delay, 0s) backwards; }
.g-reversi .disc.flip.c1 { animation: rv-to1 .42s ease-in-out var(--delay, 0s) backwards; }
@keyframes rv-to0 { 0% { transform: rotateY(0) scale(1); background: ${FACE[1]} } 49.9% { background: ${FACE[1]} }
  50% { transform: rotateY(90deg) scale(1.12); background: ${FACE[0]} } 100% { transform: rotateY(0) scale(1); background: ${FACE[0]} } }
@keyframes rv-to1 { 0% { transform: rotateY(0) scale(1); background: ${FACE[0]} } 49.9% { background: ${FACE[0]} }
  50% { transform: rotateY(90deg) scale(1.12); background: ${FACE[1]} } 100% { transform: rotateY(0) scale(1); background: ${FACE[1]} } }
.g-reversi .disc.last::after { content: ''; position: absolute; left: 50%; top: 50%; width: 20%; height: 20%; margin: -10%; border-radius: 50%; background: #f59e0b; box-shadow: 0 0 6px #f59e0b; }
.g-reversi .rv.over .disc:not(.wn) { opacity: .55; transition: opacity .6s .3s; }
.g-reversi .sw-disc { width: 20px; height: 20px; border-radius: 50%; box-shadow: 0 1px 2px rgba(0,0,0,.5); }
`

function init() {
  const b = new Array(64).fill(-1)
  b[27] = 1; b[36] = 1; b[28] = 0; b[35] = 0
  return { b, p: 0 }
}
function flipsFor(pos, i, p) {
  if (pos.b[i] >= 0) return []
  const r0 = i >> 3, c0 = i & 7, out = []
  for (const [dr, dc] of DIRS) {
    const tmp = []
    let r = r0 + dr, c = c0 + dc
    while (r >= 0 && r < 8 && c >= 0 && c < 8 && pos.b[r * 8 + c] === 1 - p) { tmp.push(r * 8 + c); r += dr; c += dc }
    if (tmp.length && r >= 0 && r < 8 && c >= 0 && c < 8 && pos.b[r * 8 + c] === p) out.push(...tmp)
  }
  return out
}
const legalFor = (pos, p) => { const out = []; for (let i = 0; i < 64; i++) if (flipsFor(pos, i, p).length) out.push(i); return out }
const legal = (pos) => legalFor(pos, pos.p)
function apply(pos, i) {
  const f = flipsFor(pos, i, pos.p)
  pos.b[i] = pos.p
  for (const x of f) pos.b[x] = pos.p
  pos.p ^= 1
  return { s: i, flips: f.map((x) => [x, Math.max(Math.abs((x >> 3) - (i >> 3)), Math.abs((x & 7) - (i & 7)))]) }
}
function pass(pos) { pos.p ^= 1 }
function replay(_v, moves) {
  const pos = init()
  for (const m of moves) {
    if (m === -1) { if (legal(pos).length) throw new Error('passe invalide'); pass(pos); continue }
    if (!flipsFor(pos, m, pos.p).length) throw new Error('coup invalide')
    apply(pos, m)
  }
  return pos
}
const counts = (pos) => pos.b.reduce((a, v) => { if (v >= 0) a[v]++; return a }, [0, 0])
function result(pos) {
  if (legalFor(pos, 0).length || legalFor(pos, 1).length) return null
  const [a, b] = counts(pos)
  return { winner: a > b ? 0 : b > a ? 1 : -1, line: [] }
}

function createView(ctx, api) {
  const { h } = ctx
  let cur = 27, pos = null, lg = new Set()
  const root = h('div', { class: 'rv' })
  const grid = h('div', { class: 'rv-grid' })
  root.append(grid)
  const cells = []
  const dots = new Set([18, 21, 42, 45])
  for (let i = 0; i < 64; i++) {
    const cell = h('div', { class: `rv-cell${dots.has(i) ? ' dot' : ''}`, 'data-i': i })
    cell._v = -1
    cell.addEventListener('click', () => { cur = i; api.pick(i) })
    cell.addEventListener('pointermove', () => { if (root.classList.contains('kb')) { root.classList.remove('kb'); cur = i; mark() } })
    cells.push(cell)
    grid.append(cell)
  }
  for (let k = 0; k < 8; k++) {
    root.append(h('div', { class: 'rv-coord', style: { left: `calc(var(--cs) * .38 + ${k} * (var(--cs) + 1px))`, top: '0', width: 'var(--cs)', height: 'calc(var(--cs) * .38)' } }, 'abcdefgh'[k]))
    root.append(h('div', { class: 'rv-coord', style: { top: `calc(var(--cs) * .38 + ${k} * (var(--cs) + 1px))`, left: '0', height: 'var(--cs)', width: 'calc(var(--cs) * .38)' } }, String(k + 1)))
  }
  function mark() { cells.forEach((c, i) => c.classList.toggle('cur', i === cur)) }

  return {
    el: root,
    reset() { for (const c of cells) { c._v = -1; c.replaceChildren() } },
    render(p, o) {
      pos = p
      lg = new Set(o.over ? [] : legal(p))
      root.classList.toggle('locked', !o.interactive)
      root.classList.toggle('t0', p.p === 0); root.classList.toggle('t1', p.p === 1)
      root.classList.toggle('over', !!o.over)
      const flipDelay = new Map((o.anim?.flips || []).map(([x, d]) => [x, d]))
      for (let i = 0; i < 64; i++) {
        const cell = cells[i], v = p.b[i]
        if (cell._v !== v) {
          const prev = cell._v
          cell._v = v
          if (v < 0) cell.replaceChildren()
          else {
            const d = h('div', { class: `disc c${v}` })
            if (o.anim && o.anim.s === i) d.classList.add('place')
            else if (o.anim && prev >= 0 && flipDelay.has(i)) { d.classList.add('flip'); d.style.setProperty('--delay', `${0.08 + flipDelay.get(i) * 0.07}s`) }
            cell.replaceChildren(d)
          }
        }
        cell.classList.toggle('lg', lg.has(i))
        const disc = cell.firstChild
        if (disc) {
          disc.classList.toggle('last', i === o.last && !o.over)
          disc.classList.toggle('wn', !!o.over && o.over.winner === v)
        }
      }
      mark()
    },
    fit(w, hh) {
      const cs = Math.max(30, Math.floor((Math.min(w, hh) - 8 - 9) / 8.76))
      root.style.setProperty('--cs', `${cs}px`)
    },
    moveCursor(dx, dy) {
      root.classList.add('kb')
      cur = ((((cur >> 3) + dy) + 8) % 8) * 8 + ((((cur & 7) + dx) + 8) % 8)
      mark()
    },
    cursorPick() {
      if (!root.classList.contains('kb')) { root.classList.add('kb'); mark(); return null }
      return cur
    },
  }
}

export default {
  mount(root, ctx) {
    return mountDuel(root, ctx, {
      id: 'reversi',
      name: 'Reversi',
      css: CSS,
      sides: ['Noirs', 'Blancs'],
      swatch: (i) => ctx.h('div', { class: `sw-disc c${i}` }),
      worker: () => new Worker(new URL('./reversi-ai.js', import.meta.url), { type: 'module' }),
      init, apply, replay, result, legal, pass,
      toMove: (pos) => pos.p,
      extra: (pos, i) => counts(pos)[i],
      view: createView,
      animMs: (a) => (a ? 500 + 70 * Math.max(0, ...a.flips.map((f) => f[1])) : 0),
      sfx: (a) => (a && a.flips.length >= 5 ? 'merge' : 'pop'),
      endDelay: 1200,
      endText: (pos) => { const [a, b] = counts(pos); return `Noirs ${a} – ${b} Blancs` },
      scoreFor: (pos, hi, level) => ({ variant: level, score: counts(pos)[hi], meta: { level } }),
      scoreVariants: LEVELS,
      scoreFmt: (s) => `${s} pions`,
      scoreTitle: 'Victoires contre l’ordinateur (pions)',
      help: () => ctx.h('div', null,
        ctx.h('p', null, 'Posez un pion de façon à encadrer, en ligne droite (horizontale, verticale ou diagonale), un ou plusieurs pions adverses : ils sont retournés et changent de couleur. Les noirs commencent.'),
        ctx.h('p', null, 'Les points sur le plateau indiquent les coups possibles. Un joueur sans coup possible passe automatiquement son tour ; la partie s’arrête quand plus personne ne peut jouer. Celui qui a le plus de pions gagne.'),
        ctx.h('p', null, 'Conseils : les coins ne peuvent jamais être repris ; évitez les cases voisines d’un coin libre, et limitez les coups possibles de l’adversaire.'),
        ctx.h('p', { class: 'muted' }, 'Classement : nombre de pions en fin de partie, pour chaque victoire contre l’ordinateur (par niveau).')),
    })
  },
}
