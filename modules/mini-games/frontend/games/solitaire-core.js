// Klondike rules, pure (no DOM) so it can be tested with node.
// Card id c = suit * 13 + (rank - 1); suits 0 ♠, 1 ♥, 2 ♣, 3 ♦ (odd = red).
// Piles: 'stock', 'waste', 'f0'..'f3' (one per suit), 't0'..'t6'. Last array element = top card.

export const SUITS = ['♠', '♥', '♣', '♦']
export const RANKS = ['', 'A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'V', 'D', 'R']
const ALPH = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

export const suitOf = (c) => (c / 13) | 0
export const rankOf = (c) => (c % 13) + 1
export const isRed = (c) => (suitOf(c) & 1) === 1
export const cardName = (c) => `${RANKS[rankOf(c)]}${SUITS[suitOf(c)]}`
const top = (a) => (a.length ? a[a.length - 1] : -1)

function rng(seed) {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6D2B79F5) >>> 0
    let t = s
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function deal(seed) {
  const r = rng(seed * 7919 + 17)
  const deck = Array.from({ length: 52 }, (_, i) => i)
  for (let i = deck.length - 1; i > 0; i--) {
    const j = Math.floor(r() * (i + 1));
    [deck[i], deck[j]] = [deck[j], deck[i]]
  }
  const tab = [[], [], [], [], [], [], []]
  for (let row = 0; row < 7; row++) for (let col = row; col < 7; col++) tab[col].push(deck.pop())
  return { stock: deck, waste: [], found: [0, 0, 0, 0], tab, down: [0, 1, 2, 3, 4, 5, 6], score: 0, rec: 0, moves: 0 }
}

// Order in which deal() lays tableau cards (for the dealing animation): [col, index] pairs.
export const DEAL_ORDER = (() => {
  const o = []
  for (let row = 0; row < 7; row++) for (let col = row; col < 7; col++) o.push([col, row])
  return o
})()

export const clone = (s) => ({ ...s, stock: s.stock.slice(), waste: s.waste.slice(), found: s.found.slice(),
  tab: s.tab.map((t) => t.slice()), down: s.down.slice() })

const cs = (a) => a.map((c) => ALPH[c]).join('')
const sc = (str) => [...str].map((ch) => ALPH.indexOf(ch))

export function encode(s) {
  return [cs(s.stock), cs(s.waste), s.found.join(','), ...s.tab.map(cs), s.down.join(','), s.score, s.rec, s.moves].join('|')
}

export function decode(str) {
  const p = String(str).split('|')
  if (p.length !== 14) throw new Error('état invalide')
  const s = {
    stock: sc(p[0]), waste: sc(p[1]), found: p[2].split(',').map(Number), tab: p.slice(3, 10).map(sc),
    down: p[10].split(',').map(Number), score: +p[11] || 0, rec: +p[12] || 0, moves: +p[13] || 0,
  }
  const seen = new Set([...s.stock, ...s.waste, ...s.tab.flat()])
  s.found.forEach((n, suit) => { for (let r = 0; r < n; r++) seen.add(suit * 13 + r) })
  const count = s.stock.length + s.waste.length + s.tab.flat().length + s.found.reduce((a, b) => a + b, 0)
  if (seen.size !== 52 || count !== 52 || seen.has(-1) || s.found.length !== 4 || s.down.length !== 7
    || s.down.some((d, i) => !(d >= 0) || (d >= s.tab[i].length && s.tab[i].length > 0) || (d > 0 && !s.tab[i].length))) {
    throw new Error('état invalide')
  }
  return s
}

export const maxPasses = (opts) => (opts.limited ? (opts.draw === 1 ? 1 : 3) : Infinity)
const canRecycleAt = (rec, opts) => rec + 1 < maxPasses(opts)
export const canRecycle = (s, opts) => !s.stock.length && s.waste.length > 0 && canRecycleAt(s.rec, opts)
export const canDraw = (s, opts) => s.stock.length > 0 || canRecycle(s, opts)

export const canFound = (s, c) => s.found[suitOf(c)] === rankOf(c) - 1

export function canTab(s, c, col) {
  const t = s.tab[col]
  if (!t.length) return rankOf(c) === 13
  const u = top(t)
  return isRed(u) !== isRed(c) && rankOf(u) === rankOf(c) + 1
}

export function pileTop(s, pile) {
  if (pile === 'stock') return top(s.stock)
  if (pile === 'waste') return top(s.waste)
  if (pile[0] === 'f') { const su = +pile[1]; return s.found[su] ? su * 13 + s.found[su] - 1 : -1 }
  return top(s.tab[+pile[1]])
}

// Cards that would be lifted by grabbing pile at idx, or null if not movable.
export function movable(s, pile, idx) {
  if (pile === 'waste') return s.waste.length && idx === s.waste.length - 1 ? [top(s.waste)] : null
  if (pile[0] === 'f') { const c = pileTop(s, pile); return c >= 0 ? [c] : null }
  if (pile[0] === 't') {
    const i = +pile[1], t = s.tab[i]
    return idx >= s.down[i] && idx < t.length ? t.slice(idx) : null
  }
  return null
}

export function legal(s, from, idx, to) {
  if (from === to || to === 'stock' || to === 'waste') return false
  const cards = movable(s, from, idx)
  if (!cards) return false
  if (to[0] === 'f') return cards.length === 1 && to === `f${suitOf(cards[0])}` && canFound(s, cards[0])
  if (from[0] === 'f' && to[0] !== 't') return false
  return canTab(s, cards[0], +to[1])
}

const POINTS = { wasteTab: 5, toFound: 10, foundTab: -15, reveal: 5 }

// Mutates s. Returns { cards, revealed } (revealed = card id turned face up, or -1).
export function applyMove(s, from, idx, to) {
  let cards
  if (from === 'waste') cards = [s.waste.pop()]
  else if (from[0] === 'f') { const su = +from[1]; cards = [su * 13 + s.found[su] - 1]; s.found[su]-- }
  else cards = s.tab[+from[1]].splice(idx)
  if (to[0] === 'f') s.found[suitOf(cards[0])]++
  else s.tab[+to[1]].push(...cards)
  if (to[0] === 'f') s.score += POINTS.toFound
  else if (from === 'waste') s.score += POINTS.wasteTab
  else if (from[0] === 'f') s.score += POINTS.foundTab
  let revealed = -1
  if (from[0] === 't') {
    const i = +from[1]
    if (s.down[i] > 0 && s.tab[i].length === s.down[i]) { s.down[i]--; revealed = top(s.tab[i]); s.score += POINTS.reveal }
  }
  s.score = Math.max(0, s.score)
  s.moves++
  return { cards, revealed }
}

// Mutates s. Returns 'draw' | 'recycle' | null.
export function drawStock(s, opts) {
  if (s.stock.length) {
    for (let k = 0; k < opts.draw && s.stock.length; k++) s.waste.push(s.stock.pop())
    s.moves++
    return 'draw'
  }
  if (!canRecycle(s, opts)) return null
  s.stock = s.waste.reverse()
  s.waste = []
  s.rec++
  if (opts.draw === 1) s.score -= 100
  else if (s.rec >= 3) s.score -= 20
  s.score = Math.max(0, s.score)
  s.moves++
  return 'recycle'
}

export const timeBonus = (ms) => Math.round(700000 / Math.max(30, ms / 1000))

// Cards that will show up on top of the waste by drawing only (no play in between).
export function stockCycle(s, opts) {
  let st = s.stock.slice(), wa = s.waste.slice(), rec = s.rec
  const seen = new Set(), out = []
  const steps = 2 * (Math.ceil((st.length + wa.length) / opts.draw) + 1) + 2
  for (let i = 0; i < steps; i++) {
    if (st.length) {
      for (let k = 0; k < opts.draw && st.length; k++) wa.push(st.pop())
      const t = top(wa)
      if (!seen.has(t)) { seen.add(t); out.push(t) }
    } else if (wa.length && canRecycleAt(rec, opts)) {
      st = wa.reverse(); wa = []; rec++
    } else break
  }
  return out
}

const playable = (s, c) => canFound(s, c) || s.tab.some((_, j) => canTab(s, c, j))

// Useful moves, best first. { from, idx, to } or { from: 'stock', to: 'waste', draw: true }.
export function findMoves(s, opts) {
  const out = []
  const cyc = stockCycle(s, opts)
  const wTop = top(s.waste)
  const avail = new Set(cyc)
  if (wTop >= 0) avail.add(wTop)
  for (let m = 0; m < 7; m++) if (s.down[m] > 0 && s.tab[m].length > s.down[m]) avail.add(s.tab[m][s.down[m]])
  const kingWaiting = (exceptCol) => [...avail].some((c) => rankOf(c) === 13 && !(s.tab[exceptCol].includes(c)))

  if (wTop >= 0 && canFound(s, wTop)) out.push({ from: 'waste', idx: s.waste.length - 1, to: `f${suitOf(wTop)}`, prio: 0 })
  for (let i = 0; i < 7; i++) {
    const t = s.tab[i]
    if (!t.length) continue
    const c = top(t)
    if (canFound(s, c)) out.push({ from: `t${i}`, idx: t.length - 1, to: `f${suitOf(c)}`, prio: t.length - 1 === s.down[i] && s.down[i] > 0 ? 0 : 1 })
  }
  for (let i = 0; i < 7; i++) {
    const t = s.tab[i]
    for (let k = s.down[i]; k < t.length; k++) {
      const c = t[k]
      for (let j = 0; j < 7; j++) {
        if (j === i || !canTab(s, c, j)) continue
        let prio = -1
        if (k === s.down[i] && s.down[i] > 0) prio = 2
        else if (k === 0) { if (s.tab[j].length && kingWaiting(i)) prio = 5 }
        else {
          const u = t[k - 1]
          if (canFound(s, u)) prio = 3
          else if ([...avail].some((x) => !t.includes(x) && isRed(x) !== isRed(u) && rankOf(x) === rankOf(u) - 1)) prio = 6
        }
        if (prio >= 0) out.push({ from: `t${i}`, idx: k, to: `t${j}`, prio })
      }
    }
  }
  if (wTop >= 0) for (let j = 0; j < 7; j++) if (canTab(s, wTop, j)) { out.push({ from: 'waste', idx: s.waste.length - 1, to: `t${j}`, prio: 4 }); break }
  if (cyc.some((c) => c !== wTop && playable(s, c))) out.push({ from: 'stock', idx: s.stock.length - 1, to: 'waste', draw: true, prio: 7 })
  return out.sort((a, b) => a.prio - b.prio)
}

export const isWon = (s) => s.found.every((n) => n === 13)
export const canAutoFinish = (s) => !s.stock.length && !s.waste.length && s.down.every((d) => d === 0) && !isWon(s)

export function nextAutoMove(s) {
  let best = null
  const cands = s.tab.map((t, i) => [`t${i}`, t.length - 1, top(t)])
  cands.push(['waste', s.waste.length - 1, top(s.waste)])
  for (const [from, idx, c] of cands) {
    if (c < 0 || !canFound(s, c)) continue
    if (!best || rankOf(c) < rankOf(best.c)) best = { from, idx, to: `f${suitOf(c)}`, c }
  }
  return best
}

// Plays the deal with the hint order; true if that simple strategy wins (so the deal is provably winnable).
export function greedyWins(seed, opts, maxSteps = 2500) {
  const s = deal(seed)
  const seen = new Set()
  for (let step = 0; step < maxSteps; step++) {
    if (isWon(s)) return true
    const m = findMoves(s, opts)[0]
    if (!m) return false
    const key = `${encode({ ...s, score: 0, moves: 0 })}${m.from}${m.idx}${m.to}`
    if (m.draw || seen.has(key)) {
      if (!drawStock(s, opts)) return false
      continue
    }
    seen.add(key)
    applyMove(s, m.from, m.idx, m.to)
  }
  return false
}
