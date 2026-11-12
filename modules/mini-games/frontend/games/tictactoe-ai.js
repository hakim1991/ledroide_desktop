// Tic-tac-toe engines: exact minimax for 3×3, and for the 15×15 five-in-a-row variant an alpha-beta search
// over the best candidate cells, evaluated through incrementally maintained 5-cell windows.

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
const pick = (a) => a[Math.floor(Math.random() * a.length)]

// ── 3×3 ──────────────────────────────────────────────────────────────────────
const LINES3 = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [0, 3, 6], [1, 4, 7], [2, 5, 8], [0, 4, 8], [2, 4, 6]]
const won3 = (b, p) => LINES3.some(([a, c, d]) => b[a] === p && b[c] === p && b[d] === p)
const memo3 = new Map()

function solve3(b, p) {
  const key = b.join('') + p
  const m = memo3.get(key)
  if (m !== undefined) return m
  let best = -Infinity
  let empty = 0
  for (let i = 0; i < 9; i++) {
    if (b[i] !== 0) continue
    empty++
    b[i] = p
    const v = won3(b, p) ? 10 + countEmpty(b) : -solve3(b, 3 - p)
    b[i] = 0
    if (v > best) best = v
  }
  const r = empty ? best : 0
  memo3.set(key, r)
  return r
}
const countEmpty = (b) => b.reduce((n, v) => n + (v === 0), 0)

function scores3(b, p) {
  const out = []
  for (let i = 0; i < 9; i++) {
    if (b[i] !== 0) continue
    b[i] = p
    out.push([i, won3(b, p) ? 10 + countEmpty(b) : -solve3(b, 3 - p)])
    b[i] = 0
  }
  return out
}

function choose3(moves, level) {
  const b = new Array(9).fill(0)
  moves.forEach((m, i) => { b[m] = (i & 1) + 1 })
  const p = (moves.length & 1) + 1, o = 3 - p
  const free = [...b.keys()].filter((i) => b[i] === 0)
  const winning = free.filter((i) => { b[i] = p; const w = won3(b, p); b[i] = 0; return w })
  const blocking = free.filter((i) => { b[i] = o; const w = won3(b, o); b[i] = 0; return w })
  if (level === 'easy') {
    if (winning.length && Math.random() < 0.7) return pick(winning)
    if (blocking.length && Math.random() < 0.45) return pick(blocking)
    return pick(free)
  }
  if (level === 'medium') {
    if (winning.length) return pick(winning)
    if (blocking.length) return pick(blocking)
    if (Math.random() < 0.4) return pick(free)
  }
  const sc = scores3(b, p)
  const top = Math.max(...sc.map((x) => x[1]))
  return pick(sc.filter((x) => x[1] === top).map((x) => x[0]))
}

// ── 15×15, five in a row ─────────────────────────────────────────────────────
const N = 15, NN = N * N, K = 5
const WV = [0, 1, 12, 160, 2200, 0]
const winCells = []
const cellWins = Array.from({ length: NN }, () => [])
for (let r = 0; r < N; r++) for (let c = 0; c < N; c++) {
  for (const [dr, dc] of [[0, 1], [1, 0], [1, 1], [1, -1]]) {
    const er = r + dr * (K - 1), ec = c + dc * (K - 1)
    if (er < 0 || er >= N || ec < 0 || ec >= N) continue
    const cells = []
    for (let k = 0; k < K; k++) cells.push((r + dr * k) * N + c + dc * k)
    const w = winCells.length
    winCells.push(cells)
    for (const x of cells) cellWins[x].push(w)
  }
}
const NW = winCells.length
const CW = cellWins.map((a) => Int32Array.from(a))
const board = new Int8Array(NN)
const wc = [new Uint8Array(NW), new Uint8Array(NW)]
const T = [0, 0], four = [0, 0]
const near = new Uint8Array(NN)
let stones = 0, z1 = 0, z2 = 0
const Z1 = new Int32Array(NN * 2), Z2 = new Int32Array(NN * 2)
{
  let s = 0x2545F491
  const r = () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return s | 0 }
  for (let i = 0; i < NN * 2; i++) { Z1[i] = r(); Z2[i] = r() }
}
const NEAR = []
for (let i = 0; i < NN; i++) {
  const r = (i / N) | 0, c = i % N, list = []
  for (let dr = -2; dr <= 2; dr++) for (let dc = -2; dc <= 2; dc++) {
    if (!dr && !dc) continue
    const rr = r + dr, cc = c + dc
    if (rr >= 0 && rr < N && cc >= 0 && cc < N) list.push(rr * N + cc)
  }
  NEAR.push(Int32Array.from(list))
}

function contrib(w) {
  const a = wc[0][w], b = wc[1][w]
  if (a && b) return
  if (a) { T[0] += SIGN * WV[a]; if (a === 4) four[0] += SIGN }
  else if (b) { T[1] += SIGN * WV[b]; if (b === 4) four[1] += SIGN }
}
let SIGN = 1
function place(x, p) {
  const ws = CW[x]
  for (let i = 0; i < ws.length; i++) {
    const w = ws[i]
    SIGN = -1; contrib(w)
    wc[p][w]++
    SIGN = 1; contrib(w)
  }
  board[x] = p + 1
  for (const y of NEAR[x]) near[y]++
  z1 ^= Z1[x * 2 + p]; z2 ^= Z2[x * 2 + p]
  stones++
}
function remove(x, p) {
  const ws = CW[x]
  for (let i = 0; i < ws.length; i++) {
    const w = ws[i]
    SIGN = -1; contrib(w)
    wc[p][w]--
    SIGN = 1; contrib(w)
  }
  board[x] = 0
  for (const y of NEAR[x]) near[y]--
  z1 ^= Z1[x * 2 + p]; z2 ^= Z2[x * 2 + p]
  stones--
}

function reset15(moves) {
  board.fill(0); wc[0].fill(0); wc[1].fill(0); near.fill(0)
  T[0] = T[1] = four[0] = four[1] = 0; stones = 0; z1 = z2 = 0
  moves.forEach((m, i) => place(m, i & 1))
}

function fourCells(p) {
  const o = 1 - p, out = []
  for (let w = 0; w < NW; w++) {
    if (wc[p][w] !== 4 || wc[o][w]) continue
    for (const x of winCells[w]) if (!board[x] && !out.includes(x)) out.push(x)
  }
  return out
}

function cellScore(x, p) {
  const o = 1 - p, ws = CW[x]
  let att = 0, def = 0
  for (let i = 0; i < ws.length; i++) {
    const w = ws[i], a = wc[p][w], b = wc[o][w]
    if (!b) att += a === 4 ? 1e6 : WV[a + 1] - WV[a]
    if (!a) def += b === 4 ? 5e5 : WV[b + 1] - WV[b]
  }
  return att + def * 0.85
}

function candidates(p, limit) {
  const list = []
  for (let x = 0; x < NN; x++) if (!board[x] && near[x]) list.push([x, cellScore(x, p)])
  if (!list.length) list.push([(N * N) >> 1, 1])
  list.sort((a, b) => b[1] - a[1])
  return limit ? list.slice(0, limit) : list
}

const WIN = 1e7
const TT_SIZE = 1 << 18, TT_MASK = TT_SIZE - 1
const ttKey = new Int32Array(TT_SIZE), ttVal = new Float64Array(TT_SIZE), ttDepth = new Int8Array(TT_SIZE)
const ttFlag = new Int8Array(TT_SIZE), ttMove = new Int16Array(TT_SIZE)
let deadline = Infinity, aborted = false, nodes = 0

function evaluate(p) {
  const o = 1 - p
  return T[p] * 1.15 - T[o]
}

function negamax(p, depth, alpha, beta, ply) {
  if ((++nodes & 1023) === 0 && now() > deadline) aborted = true
  if (aborted) return 0
  const o = 1 - p
  if (four[p] > 0) return WIN - ply
  if (stones >= NN) return 0
  let moves
  if (four[o] > 0) {
    const f = fourCells(o)
    if (f.length > 1) return -(WIN - ply - 1)
    moves = f
  }
  if (depth <= 0 && !moves) return evaluate(p)

  const idx = z1 & TT_MASK
  let ttm = -1
  if (ttKey[idx] === z2) {
    ttm = ttMove[idx]
    if (ttDepth[idx] >= depth) {
      const v = ttVal[idx], f = ttFlag[idx]
      if (f === 1) return v
      if (f === 2 && v >= beta) return v
      if (f === 3 && v <= alpha) return v
    }
  }
  if (!moves) {
    const lim = depth >= 4 ? 9 : depth >= 2 ? 8 : 6
    moves = candidates(p, lim).map((x) => x[0])
    if (ttm >= 0 && board[ttm] === 0) { const i = moves.indexOf(ttm); if (i > 0) { moves.splice(i, 1); moves.unshift(ttm) } else if (i < 0) moves.unshift(ttm) }
  }
  const a0 = alpha
  let best = -Infinity, bm = moves[0]
  for (const x of moves) {
    place(x, p)
    const v = -negamax(o, depth - 1, -beta, -alpha, ply + 1)
    remove(x, p)
    if (aborted) return 0
    if (v > best) { best = v; bm = x }
    if (v > alpha) alpha = v
    if (alpha >= beta) break
  }
  ttKey[idx] = z2; ttVal[idx] = best; ttDepth[idx] = Math.max(0, depth); ttMove[idx] = bm
  ttFlag[idx] = best <= a0 ? 3 : best >= beta ? 2 : 1
  return best
}

function search15(p, maxTime, maxDepth, rootLimit) {
  deadline = now() + maxTime; aborted = false; nodes = 0
  let order = candidates(p, rootLimit).map((x) => x[0])
  let best = order[0], bestScore = 0, reached = 0
  for (let d = 1; d <= maxDepth; d++) {
    const sc = []
    let alpha = -Infinity
    for (const x of order) {
      place(x, p)
      const v = -negamax(1 - p, d - 1, -Infinity, -alpha + 1e-9, 1)
      remove(x, p)
      if (aborted) break
      sc.push([x, v])
      if (v > alpha) alpha = v
    }
    if (aborted) break
    sc.sort((a, b) => b[1] - a[1])
    best = sc[0][0]; bestScore = sc[0][1]; reached = d
    order = sc.map((x) => x[0])
    if (Math.abs(bestScore) > WIN / 2) break
  }
  return { move: best, score: bestScore, depth: reached, nodes }
}

function choose15(moves, level, time) {
  reset15(moves)
  const p = moves.length & 1, o = 1 - p
  if (!moves.length) return (N * N) >> 1
  const win = fourCells(p), threat = fourCells(o)
  if (level === 'easy') {
    if (win.length && Math.random() < 0.8) return pick(win)
    if (threat.length && Math.random() < 0.6) return pick(threat)
    const c = candidates(p, 8).map(([x, s]) => [x, s * (0.15 + Math.random() * 1.6)]).sort((a, b) => b[1] - a[1])
    return c[0][0]
  }
  if (win.length) return win[0]
  if (threat.length) return threat[0]
  if (level === 'medium') {
    deadline = Infinity; aborted = false
    const c = candidates(p, 10).map(([x]) => {
      place(x, p)
      const v = -negamax(o, 1, -Infinity, Infinity, 1)
      remove(x, p)
      return [x, v + Math.random() * 30]
    }).sort((a, b) => b[1] - a[1])
    return c[0][0]
  }
  return search15(p, time, 12, 14).move
}

export function chooseMove({ variant, moves, level = 'hard', time = 1000 }) {
  return variant === '3' ? choose3(moves, level) : choose15(moves, level, time)
}

export const _internals = { scores3, solve3, reset15, search15 }

if (typeof self !== 'undefined' && typeof window === 'undefined' && typeof self.postMessage === 'function') {
  self.onmessage = (e) => {
    const { token, moves, level, variant } = e.data
    self.postMessage({ token, move: chooseMove({ variant, moves, level }) })
  }
}
