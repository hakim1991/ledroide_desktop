// Reversi engine on a 10×10 mailbox (border = 3). Players: 1 = black (moves first), 2 = white.
// External move format: square 0..63 (row * 8 + col), -1 = pass.

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
const DIRS = [-11, -10, -9, -1, 1, 9, 10, 11]
const SQ = []
for (let r = 1; r <= 8; r++) for (let c = 1; c <= 8; c++) SQ.push(r * 10 + c)
const toMb = (i) => ((i >> 3) + 1) * 10 + (i & 7) + 1
const fromMb = (s) => ((s / 10 | 0) - 1) * 8 + (s % 10) - 1

const WEIGHTS = [
  100, -25, 10, 5, 5, 10, -25, 100,
  -25, -45, -2, -2, -2, -2, -45, -25,
  10, -2, 3, 1, 1, 3, -2, 10,
  5, -2, 1, 0, 0, 1, -2, 5,
  5, -2, 1, 0, 0, 1, -2, 5,
  10, -2, 3, 1, 1, 3, -2, 10,
  -25, -45, -2, -2, -2, -2, -45, -25,
  100, -25, 10, 5, 5, 10, -25, 100,
]
const W = new Int16Array(100)
SQ.forEach((s, i) => { W[s] = WEIGHTS[i] })
const CORNERS = [11, 18, 81, 88]
const XSQ = { 11: [22, 12, 21], 18: [27, 17, 28], 81: [72, 71, 82], 88: [77, 78, 87] }
const ORDERED = [...SQ].sort((a, b) => W[b] - W[a])

const board = new Int8Array(100)
const stack = new Int8Array(4096)
let sp = 0
let empties = 60
const Z1 = new Int32Array(300), Z2 = new Int32Array(300)
{
  let s = 0x6C8E9CF5
  const r = () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return s | 0 }
  for (let i = 0; i < 300; i++) { Z1[i] = r(); Z2[i] = r() }
}
let z1 = 0, z2 = 0

export function setup(moves) {
  board.fill(3)
  for (const s of SQ) board[s] = 0
  board[44] = 2; board[55] = 2; board[45] = 1; board[54] = 1
  empties = 60; sp = 0; z1 = z2 = 0
  for (const s of SQ) if (board[s]) { z1 ^= Z1[board[s] * 100 + s]; z2 ^= Z2[board[s] * 100 + s] }
  let p = 1
  for (const m of moves) {
    if (m >= 0) { if (!make(toMb(m), p)) throw new Error('illegal') }
    p = 3 - p
  }
  return p
}

function canPlay(s, p) {
  if (board[s]) return false
  const o = 3 - p
  for (const d of DIRS) {
    let t = s + d
    if (board[t] !== o) continue
    t += d
    while (board[t] === o) t += d
    if (board[t] === p) return true
  }
  return false
}

function make(s, p) {
  const o = 3 - p, base = sp
  for (const d of DIRS) {
    let t = s + d
    if (board[t] !== o) continue
    t += d
    while (board[t] === o) t += d
    if (board[t] !== p) continue
    for (let u = s + d; u !== t; u += d) {
      board[u] = p; stack[sp++] = u
      z1 ^= Z1[o * 100 + u] ^ Z1[p * 100 + u]; z2 ^= Z2[o * 100 + u] ^ Z2[p * 100 + u]
    }
  }
  const n = sp - base
  if (!n) return 0
  board[s] = p; stack[sp++] = n; stack[sp++] = s
  z1 ^= Z1[p * 100 + s]; z2 ^= Z2[p * 100 + s]
  empties--
  return n
}

function unmake(p) {
  const s = stack[--sp], n = stack[--sp], o = 3 - p
  board[s] = 0
  z1 ^= Z1[p * 100 + s]; z2 ^= Z2[p * 100 + s]
  for (let i = 0; i < n; i++) {
    const u = stack[--sp]
    board[u] = o
    z1 ^= Z1[o * 100 + u] ^ Z1[p * 100 + u]; z2 ^= Z2[o * 100 + u] ^ Z2[p * 100 + u]
  }
  empties++
}

function genMoves(p) {
  const out = []
  for (const s of ORDERED) if (canPlay(s, p)) out.push(s)
  return out
}
function mobility(p) {
  let n = 0
  for (const s of SQ) if (canPlay(s, p)) n++
  return n
}
function discDiff(p) {
  let d = 0
  for (const s of SQ) { const v = board[s]; if (v === p) d++; else if (v) d-- }
  return d
}

function edgeStable(p) {
  let n = 0
  const walk = (start, step, len) => { let k = 0; for (let i = 0, s = start; i < len && board[s] === p; i++, s += step) k++; return k }
  for (const [c, a, b] of [[11, 1, 10], [18, -1, 10], [81, 1, -10], [88, -1, -10]]) {
    if (board[c] !== p) continue
    n += walk(c, a, 8) + walk(c, b, 8) - 1
  }
  return n
}

function evaluate(p, light = false) {
  const o = 3 - p
  let pos = 0
  for (const s of SQ) {
    const v = board[s]
    if (!v) continue
    let w = W[s]
    if (w < -5) for (const c of CORNERS) if (board[c] && XSQ[c].includes(s)) { w = 4; break }
    pos += v === p ? w : -w
  }
  if (light) return pos
  const mp = mobility(p), mo = mobility(o)
  const mob = (100 * (mp - mo)) / (mp + mo + 2)
  const st = edgeStable(p) - edgeStable(o)
  let front = 0
  for (const s of SQ) {
    const v = board[s]
    if (!v) continue
    for (const d of DIRS) if (board[s + d] === 0) { front += v === p ? -1 : 1; break }
  }
  const late = empties < 18 ? (18 - empties) * 1.5 * discDiff(p) : 0
  return pos + mob * 1.6 + st * 14 + front * 3 + late
}

const TT_SIZE = 1 << 18, TT_MASK = TT_SIZE - 1
const ttKey = new Int32Array(TT_SIZE), ttVal = new Float64Array(TT_SIZE), ttDepth = new Int8Array(TT_SIZE)
const ttFlag = new Int8Array(TT_SIZE), ttMove = new Int8Array(TT_SIZE)
let deadline = Infinity, aborted = false, nodes = 0
const FINAL = 1e6

function negamax(p, depth, alpha, beta, passed) {
  if ((++nodes & 2047) === 0 && now() > deadline) aborted = true
  if (aborted) return 0
  if (depth <= 0) return evaluate(p)
  const o = 3 - p
  const key = (z1 ^ (p === 2 ? 0x5bd1e995 : 0)) & TT_MASK
  const chk = z2 ^ p
  let ttm = 0
  if (ttKey[key] === chk) {
    ttm = ttMove[key]
    if (ttDepth[key] >= depth) {
      const v = ttVal[key], f = ttFlag[key]
      if (f === 1) return v
      if (f === 2 && v >= beta) return v
      if (f === 3 && v <= alpha) return v
    }
  }
  const moves = genMoves(p)
  if (!moves.length) {
    if (passed) { const d = discDiff(p); return d > 0 ? FINAL + d : d < 0 ? -FINAL + d : 0 }
    return -negamax(o, depth, -beta, -alpha, true)
  }
  if (ttm) { const i = moves.indexOf(ttm); if (i > 0) { moves.splice(i, 1); moves.unshift(ttm) } }
  const a0 = alpha
  let best = -Infinity, bm = moves[0]
  for (const s of moves) {
    make(s, p)
    const v = -negamax(o, depth - 1, -beta, -alpha, false)
    unmake(p)
    if (aborted) return 0
    if (v > best) { best = v; bm = s }
    if (v > alpha) alpha = v
    if (alpha >= beta) break
  }
  ttKey[key] = chk; ttVal[key] = best; ttDepth[key] = depth; ttMove[key] = bm
  ttFlag[key] = best <= a0 ? 3 : best >= beta ? 2 : 1
  return best
}

function solve(p, alpha, beta, passed) {
  if ((++nodes & 2047) === 0 && now() > deadline) aborted = true
  if (aborted) return 0
  const o = 3 - p
  const moves = genMoves(p)
  if (!moves.length) {
    if (passed) return discDiff(p)
    return -solve(o, -beta, -alpha, true)
  }
  if (empties > 7 && moves.length > 1) {
    const sc = moves.map((s) => { make(s, p); const m = mobility(o); unmake(p); return m * 100 - W[s] })
    const idx = moves.map((_, i) => i).sort((a, b) => sc[a] - sc[b])
    const sorted = idx.map((i) => moves[i])
    moves.length = 0; moves.push(...sorted)
  }
  let best = -Infinity
  for (const s of moves) {
    make(s, p)
    const v = -solve(o, -beta, -alpha, false)
    unmake(p)
    if (aborted) return 0
    if (v > best) best = v
    if (v > alpha) alpha = v
    if (alpha >= beta) break
  }
  return best
}

function searchHard(p, time) {
  const start = now()
  deadline = start + time; aborted = false; nodes = 0
  let order = genMoves(p)
  if (order.length === 1) return order[0]
  let best = order[0]
  for (let d = 1; d <= 60; d++) {
    const sc = []
    let alpha = -Infinity
    for (const s of order) {
      make(s, p)
      const v = -negamax(3 - p, d - 1, -Infinity, -alpha + 1e-6, false)
      unmake(p)
      if (aborted) break
      sc.push([s, v])
      if (v > alpha) alpha = v
    }
    if (aborted) break
    sc.sort((a, b) => b[1] - a[1])
    best = sc[0][0]
    order = sc.map((x) => x[0])
    if (d >= empties) break
  }
  if (empties <= 14 && now() < start + time * 0.6) {
    deadline = start + time * 1.3; aborted = false
    let bestV = -Infinity, bestS = best
    let alpha = -65
    for (const s of order) {
      make(s, p)
      const v = -solve(3 - p, -65, -alpha, false)
      unmake(p)
      if (aborted) break
      if (v > bestV) { bestV = v; bestS = s }
      if (v > alpha) alpha = v
    }
    if (!aborted) best = bestS
  }
  return best
}

export function chooseMove(moves, level = 'hard', { time = 1000 } = {}) {
  const p = setup(moves)
  const legal = genMoves(p)
  if (!legal.length) return -1
  if (legal.length === 1) return fromMb(legal[0])
  if (level === 'easy') {
    if (Math.random() < 0.3) return fromMb(legal[Math.floor(Math.random() * legal.length)])
    const sc = legal.map((s) => { make(s, p); const v = -evaluate(3 - p, true) + Math.random() * 25; unmake(p); return [s, v] })
    sc.sort((a, b) => b[1] - a[1])
    return fromMb(sc[0][0])
  }
  if (level === 'medium') {
    deadline = Infinity; aborted = false
    const sc = legal.map((s) => { make(s, p); const v = -negamax(3 - p, 2, -Infinity, Infinity, false) + Math.random() * 6; unmake(p); return [s, v] })
    sc.sort((a, b) => b[1] - a[1])
    return fromMb(sc[0][0])
  }
  return fromMb(searchHard(p, time))
}

export const _internals = { setup, genMoves, toMb, fromMb }

if (typeof self !== 'undefined' && typeof window === 'undefined' && typeof self.postMessage === 'function') {
  self.onmessage = (e) => {
    const { token, moves, level } = e.data
    self.postMessage({ token, move: chooseMove(moves, level) })
  }
}
