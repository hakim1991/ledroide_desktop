import { mountDuel } from './duel.js'

const SPEC = { 3: { n: 3, k: 3 }, 15: { n: 15, k: 5 } }
const SVGNS = 'http://www.w3.org/2000/svg'
const GHOST_X = "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cpath d='M26 26L74 74M74 26L26 74' stroke='%232dd4bf' stroke-width='12' stroke-linecap='round' opacity='.22'/%3E%3C/svg%3E\")"
const GHOST_O = "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ccircle cx='50' cy='50' r='25' fill='none' stroke='%23fb7185' stroke-width='12' opacity='.22'/%3E%3C/svg%3E\")"

const CSS = `
.g-tictactoe .tt { --cs: 100px; --n: 3; position: relative; }
.g-tictactoe .tt-grid { display: grid; grid-template-columns: repeat(var(--n), var(--cs)); grid-auto-rows: var(--cs);
  background: #23232a; gap: 2px; padding: 2px; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.45); touch-action: none; }
.g-tictactoe .tt.v3 .tt-grid { gap: 6px; padding: 6px; border-radius: 14px; background: #2a2a33; }
.g-tictactoe .tt.v15 .tt-grid { gap: 1px; padding: 1px; border-radius: 6px; background: #2e2a24; }
.g-tictactoe .tt-cell { background: #17171c; position: relative; cursor: pointer; transition: background .15s; }
.g-tictactoe .tt.v3 .tt-cell { border-radius: 9px; }
.g-tictactoe .tt.v15 .tt-cell { background: #1d1b18; }
.g-tictactoe .tt.v15 .tt-cell.star::before { content: ''; position: absolute; left: 50%; top: 50%; width: 5px; height: 5px; margin: -2.5px; border-radius: 50%; background: #4a4438; }
.g-tictactoe .tt.locked .tt-cell { cursor: default; }
.g-tictactoe .tt:not(.locked) .tt-cell.e:hover, .g-tictactoe .tt.kb:not(.locked) .tt-cell.e.cur { background-image: var(--ghost); background-size: 100% 100%; background-color: #202027; }
.g-tictactoe .tt.v15:not(.locked) .tt-cell.e:hover, .g-tictactoe .tt.v15.kb:not(.locked) .tt-cell.e.cur { background-color: #2a2620; }
.g-tictactoe .tt.kb .tt-cell.cur { box-shadow: inset 0 0 0 2px rgba(255,255,255,.55); z-index: 1; }
.g-tictactoe .tt.t0 { --ghost: ${GHOST_X}; } .g-tictactoe .tt.t1 { --ghost: ${GHOST_O}; }
.g-tictactoe .tt-cell svg { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; overflow: visible; }
.g-tictactoe .mk path, .g-tictactoe .mk circle { fill: none; stroke-linecap: round; }
.g-tictactoe .mk.x path { stroke: #2dd4bf; filter: drop-shadow(0 0 6px rgba(45,212,191,.35)); }
.g-tictactoe .mk.o circle { stroke: #fb7185; filter: drop-shadow(0 0 6px rgba(251,113,133,.35)); }
.g-tictactoe .tt.v3 .mk path, .g-tictactoe .tt.v3 .mk circle { stroke-width: 11; }
.g-tictactoe .tt.v15 .mk path, .g-tictactoe .tt.v15 .mk circle { stroke-width: 13; }
.g-tictactoe .mk.draw path, .g-tictactoe .mk.draw circle { stroke-dasharray: 100; animation: tt-draw .28s ease-out both; }
.g-tictactoe .mk.draw path + path { animation-delay: .14s; }
@keyframes tt-draw { from { stroke-dashoffset: 100 } to { stroke-dashoffset: 0 } }
.g-tictactoe .tt-cell.last { background: #24242d; }
.g-tictactoe .tt.v15 .tt-cell.last { background: #332d22; box-shadow: inset 0 0 0 1px rgba(245,200,120,.45); }
.g-tictactoe .tt.over .tt-cell:not(.wl) .mk { opacity: .35; transition: opacity .5s .2s; }
.g-tictactoe .tt-cell.wl { animation: tt-glow 1.1s .3s ease-in-out infinite; }
@keyframes tt-glow { 50% { background-color: #2b3a37 } }
.g-tictactoe .tt-line { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 2; }
.g-tictactoe .tt-line line { stroke: rgba(255,255,255,.85); stroke-linecap: round; stroke-dasharray: 1; stroke-dashoffset: 1; animation: tt-strike .45s .15s ease-out forwards; }
@keyframes tt-strike { to { stroke-dashoffset: 0 } }
.g-tictactoe .sw-mk { width: 22px; height: 22px; }
.g-tictactoe .sw-mk path, .g-tictactoe .sw-mk circle { stroke-width: 13; }
`

function markSvg(p, draw) {
  const svg = document.createElementNS(SVGNS, 'svg')
  svg.setAttribute('viewBox', '0 0 100 100')
  svg.setAttribute('class', `mk ${p ? 'o' : 'x'}${draw ? ' draw' : ''}`)
  if (p === 0) {
    for (const d of ['M27 27L73 73', 'M73 27L27 73']) {
      const el = document.createElementNS(SVGNS, 'path')
      el.setAttribute('d', d); el.setAttribute('pathLength', '100')
      svg.append(el)
    }
  } else {
    const c = document.createElementNS(SVGNS, 'circle')
    c.setAttribute('cx', 50); c.setAttribute('cy', 50); c.setAttribute('r', 24); c.setAttribute('pathLength', '100')
    c.setAttribute('transform', 'rotate(-90 50 50)')
    svg.append(c)
  }
  return svg
}

function init(variant) {
  const { n, k } = SPEC[variant]
  return { n, k, b: new Array(n * n).fill(-1), count: 0 }
}
function apply(pos, x) {
  pos.b[x] = pos.count & 1
  pos.count++
  return { x }
}
function replay(variant, moves) {
  const pos = init(variant)
  for (const x of moves) {
    if (!(x >= 0 && x < pos.b.length) || pos.b[x] >= 0) throw new Error('coup invalide')
    apply(pos, x)
  }
  return pos
}
function result(pos) {
  const { n, k, b } = pos
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) {
    const p = b[r * n + c]
    if (p < 0) continue
    for (const [dr, dc] of [[0, 1], [1, 0], [1, 1], [1, -1]]) {
      const pr = r - dr, pc = c - dc
      if (pr >= 0 && pr < n && pc >= 0 && pc < n && b[pr * n + pc] === p) continue
      const line = [r * n + c]
      let rr = r + dr, cc = c + dc
      while (rr >= 0 && rr < n && cc >= 0 && cc < n && b[rr * n + cc] === p) { line.push(rr * n + cc); rr += dr; cc += dc }
      if (line.length >= k) return { winner: p, line }
    }
  }
  return pos.count >= n * n ? { winner: -1, line: [] } : null
}
const legal = (pos) => pos.b.reduce((a, v, i) => (v < 0 ? (a.push(i), a) : a), [])

function createView(ctx, api) {
  const { h } = ctx
  let n = 3, cells = [], cur = 4, pos = null
  const root = h('div', { class: 'tt v3' })
  const grid = h('div', { class: 'tt-grid' })
  const lineSvg = document.createElementNS(SVGNS, 'svg')
  lineSvg.setAttribute('class', 'tt-line')
  root.append(grid, lineSvg)

  function build(size) {
    n = size
    root.className = `tt v${n}`
    root.style.setProperty('--n', n)
    cells = []
    grid.replaceChildren()
    const stars = n === 15 ? new Set([3 * 15 + 3, 3 * 15 + 11, 7 * 15 + 7, 11 * 15 + 3, 11 * 15 + 11]) : new Set()
    for (let i = 0; i < n * n; i++) {
      const cell = h('div', { class: `tt-cell e${stars.has(i) ? ' star' : ''}`, 'data-i': i })
      cell.addEventListener('click', () => { cur = i; api.pick(i) })
      cell.addEventListener('pointermove', () => { if (root.classList.contains('kb')) { root.classList.remove('kb'); cur = i; mark() } })
      cell._v = -1
      cells.push(cell)
      grid.append(cell)
    }
    cur = (n * n) >> 1
  }
  build(3)

  function mark() { cells.forEach((c, i) => c.classList.toggle('cur', i === cur)) }

  return {
    el: root,
    reset(p) { build(p.n) },
    render(p, o) {
      pos = p
      if (p.n !== n) build(p.n)
      root.classList.toggle('locked', !o.interactive)
      root.classList.toggle('t0', o.turn !== 1); root.classList.toggle('t1', o.turn === 1)
      const line = o.over?.line || []
      const wl = new Set(line)
      root.classList.toggle('over', !!o.over && o.over.winner >= 0)
      for (let i = 0; i < cells.length; i++) {
        const cell = cells[i], v = p.b[i]
        if (cell._v !== v) {
          cell._v = v
          cell.replaceChildren()
          if (v >= 0) cell.append(markSvg(v, !!o.anim && o.anim.x === i))
          cell.classList.toggle('e', v < 0)
        }
        cell.classList.toggle('last', i === o.last && !o.over)
        cell.classList.toggle('wl', wl.has(i))
      }
      lineSvg.replaceChildren()
      if (line.length) {
        lineSvg.setAttribute('viewBox', `0 0 ${n} ${n}`)
        const a = line[0], b = line[line.length - 1]
        const ax = (a % n) + 0.5, ay = Math.floor(a / n) + 0.5, bx = (b % n) + 0.5, by = Math.floor(b / n) + 0.5
        const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1, ext = n === 3 ? 0.32 : 0.3
        const l = document.createElementNS(SVGNS, 'line')
        l.setAttribute('x1', ax - (dx / len) * ext); l.setAttribute('y1', ay - (dy / len) * ext)
        l.setAttribute('x2', bx + (dx / len) * ext); l.setAttribute('y2', by + (dy / len) * ext)
        l.setAttribute('pathLength', '1')
        l.setAttribute('stroke-width', n === 3 ? 0.07 : 0.16)
        lineSvg.append(l)
      }
      mark()
    },
    fit(w, hh) {
      const pad = n === 3 ? 12 + 2 * 6 : n + 1
      const cs = Math.max(n === 3 ? 50 : 18, Math.floor((Math.min(w, hh) - 8 - pad) / n))
      root.style.setProperty('--cs', `${n === 3 ? Math.min(cs, 150) : cs}px`)
    },
    moveCursor(dx, dy) {
      root.classList.add('kb')
      const r = Math.floor(cur / n), c = cur % n
      cur = ((r + dy + n) % n) * n + ((c + dx + n) % n)
      mark()
    },
    cursorPick() {
      if (!root.classList.contains('kb')) { root.classList.add('kb'); mark(); return null }
      return pos && pos.b[cur] < 0 ? cur : null
    },
  }
}

export default {
  mount(root, ctx) {
    const sw = (i) => { const s = markSvg(i, false); s.setAttribute('class', `${s.getAttribute('class')} sw-mk`); return s }
    return mountDuel(root, ctx, {
      id: 'tictactoe',
      name: 'Morpion',
      css: CSS,
      sides: ['Croix', 'Ronds'],
      variants: [['3', 'Classique 3×3'], ['15', 'Grand plateau 15×15 (5 alignés)']],
      swatch: sw,
      worker: () => new Worker(new URL('./tictactoe-ai.js', import.meta.url), { type: 'module' }),
      init, apply, replay, result, legal,
      toMove: (pos) => pos.count & 1,
      view: createView,
      animMs: () => 300,
      sfx: (a) => (a ? 'pop' : 'click'),
      endDelay: 1000,
      help: () => ctx.h('div', null,
        ctx.h('p', null, 'Chacun à son tour, les joueurs posent leur symbole (croix ou rond) sur une case libre. Les croix commencent.'),
        ctx.h('ul', null,
          ctx.h('li', null, ctx.h('b', null, 'Classique 3×3 : '), 'alignez 3 symboles. En difficile, l’ordinateur joue parfaitement : le mieux que vous puissiez obtenir est un match nul.'),
          ctx.h('li', null, ctx.h('b', null, 'Grand plateau 15×15 : '), 'alignez 5 symboles ou plus. Surveillez les alignements de trois ouverts aux deux bouts : ils deviennent imparables.'))),
    })
  },
}
