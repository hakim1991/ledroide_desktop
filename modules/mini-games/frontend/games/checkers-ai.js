// Draughts AI: alpha-beta (negamax) with iterative deepening, transposition table, killer/history ordering
// and capture extension (captures are compulsory, so a position with a capture is never evaluated statically).
import { genMoves, replayKeys, moveKey, promoRow, QUIET_LIMIT } from './checkers-core.js'

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
const WIN = 100000
const MAXPLY = 96

const TT_BITS = 20, TT_SIZE = 1 << TT_BITS, TT_MASK = TT_SIZE - 1
const ttKey = new Int32Array(TT_SIZE), ttDepth = new Int8Array(TT_SIZE), ttFlag = new Int8Array(TT_SIZE)
const ttVal = new Int32Array(TT_SIZE), ttMove = new Int32Array(TT_SIZE)

const Z1 = new Int32Array(50 * 5), Z2 = new Int32Array(50 * 5)
let ZS1 = 0, ZS2 = 0
{
  let s = 0x2545F491
  const r = () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return s | 0 }
  for (let i = 0; i < Z1.length; i++) { Z1[i] = r(); Z2[i] = r() }
  ZS1 = r(); ZS2 = r()
}

let g = null, b = null, side = 0, h1 = 0, h2 = 0, quiet = 0
let nodes = 0, deadline = Infinity, aborted = false
const hs1 = new Int32Array(1024), hs2 = new Int32Array(1024)
let hsp = 0, irr = 0
const killers = new Int32Array(MAXPLY * 2)
let history = new Int32Array(50 * 50)
let ADV = null, CENTER = null, BACK = null

function initTables() {
  const n = g.n
  ADV = new Int16Array(g.N * 2)
  CENTER = new Int8Array(g.N)
  BACK = new Int8Array(g.N * 2)
  const advW = n === 10 ? [0, 1, 2, 4, 6, 9, 13, 18, 24, 30] : [0, 1, 3, 6, 10, 15, 22, 30]
  for (let s = 0; s < g.N; s++) {
    const r = g.row[s], c = g.col[s]
    ADV[s * 2] = advW[n - 1 - r]
    ADV[s * 2 + 1] = advW[r]
    const cd = Math.abs(c - (n - 1) / 2), rd = Math.abs(r - (n - 1) / 2)
    CENTER[s] = cd < 2 && rd < 2 ? 6 : cd < 3 && rd < 3 ? 3 : 0
    BACK[s * 2] = r === n - 1 ? 1 : 0
    BACK[s * 2 + 1] = r === 0 ? 1 : 0
  }
}

const zi = (s, v) => s * 5 + v + 2

function load(variant, keys) {
  const pos = replayKeys(variant, keys)
  const ng = pos.g
  if (ng !== g) { g = ng; initTables(); ttKey.fill(0); ttDepth.fill(0) }
  b = Int8Array.from(pos.b)
  side = pos.p
  quiet = pos.quiet
  h1 = side ? ZS1 : 0; h2 = side ? ZS2 : 0
  for (let s = 0; s < g.N; s++) if (b[s]) { h1 ^= Z1[zi(s, b[s])]; h2 ^= Z2[zi(s, b[s])] }
  hsp = 0; irr = 0
  hs1[hsp] = h1; hs2[hsp] = h2; hsp++
  history = new Int32Array(g.N * g.N)
  killers.fill(-1)
  return pos
}

const code = (m) => (m.from * 64 + m.to) * 64 + (m.path.length > 2 ? m.path[1] : 63)

function make(m) {
  const piece = b[m.from]
  const capV = m.caps.map((s) => b[s])
  h1 ^= Z1[zi(m.from, piece)]; h2 ^= Z2[zi(m.from, piece)]
  b[m.from] = 0
  for (let i = 0; i < m.caps.length; i++) {
    const s = m.caps[i]
    h1 ^= Z1[zi(s, capV[i])]; h2 ^= Z2[zi(s, capV[i])]
    b[s] = 0
  }
  let np = piece
  if ((piece === 1 || piece === -1) && g.row[m.to] === promoRow(g, side)) np = piece * 2
  b[m.to] = np
  h1 ^= Z1[zi(m.to, np)] ^ ZS1; h2 ^= Z2[zi(m.to, np)] ^ ZS2
  const undo = { piece, capV, quiet, irr }
  const reversible = !m.caps.length && (piece === 2 || piece === -2)
  quiet = reversible ? quiet + 1 : 0
  if (!reversible) irr = hsp
  side ^= 1
  hs1[hsp] = h1; hs2[hsp] = h2; hsp++
  return undo
}

function unmake(m, u) {
  side ^= 1
  hsp--
  b[m.to] = 0
  for (let i = 0; i < m.caps.length; i++) b[m.caps[i]] = u.capV[i]
  b[m.from] = u.piece
  quiet = u.quiet; irr = u.irr
  h1 = hs1[hsp - 1]; h2 = hs2[hsp - 1]
}

function repeated() {
  for (let i = hsp - 3; i >= irr; i -= 2) if (hs1[i] === h1 && hs2[i] === h2) return true
  return false
}

// Static evaluation from the side to move's point of view.
function evaluate() {
  const n = g.n, nb = g.nb
  let sc = 0, wm = 0, bm = 0, wk = 0, bk = 0
  const KING = n === 10 ? 320 : 270
  for (let s = 0; s < g.N; s++) {
    const v = b[s]
    if (!v) continue
    if (v === 1) {
      wm++
      let x = 100 + ADV[s * 2] + CENTER[s]
      if (BACK[s * 2]) x += 6
      const l = nb[s * 4 + 2], r = nb[s * 4 + 3]
      if ((l < 0 || b[l] > 0) && (r < 0 || b[r] > 0)) x += 4
      sc += x
    } else if (v === -1) {
      bm++
      let x = 100 + ADV[s * 2 + 1] + CENTER[s]
      if (BACK[s * 2 + 1]) x += 6
      const l = nb[s * 4], r = nb[s * 4 + 1]
      if ((l < 0 || b[l] < 0) && (r < 0 || b[r] < 0)) x += 4
      sc -= x
    } else if (v === 2) { wk++; sc += KING + CENTER[s] * 2 } else { bk++; sc -= KING + CENTER[s] * 2 }
  }
  const total = wm + bm + wk + bk
  const mat = (wm - bm) * 100 + (wk - bk) * KING
  sc += Math.round((mat * 6) / (total + 4))
  if (!wm && !bm && Math.abs(wk - bk) <= 1 && wk + bk <= 4) sc = Math.round(sc / 8)
  return side ? -sc : sc
}

function order(moves, ttm, ply) {
  if (moves.length < 2) return moves
  const k0 = killers[ply * 2], k1 = killers[ply * 2 + 1]
  const sc = moves.map((m) => {
    const c = code(m)
    let s = history[m.from * g.N + m.to]
    if (c === ttm) s += 1e9
    else if (c === k0) s += 1e8
    else if (c === k1) s += 1e8 - 1
    const v = b[m.from]
    if ((v === 1 || v === -1) && g.row[m.to] === promoRow(g, side)) s += 5e7
    return [s, m]
  })
  sc.sort((a, b2) => b2[0] - a[0])
  return sc.map((x) => x[1])
}

function search(depth, alpha, beta, ply) {
  if ((++nodes & 2047) === 0 && now() > deadline) aborted = true
  if (aborted) return 0
  if (ply > 0 && (quiet >= QUIET_LIMIT || repeated())) return 0
  const moves = genMoves(g, b, side)
  if (!moves.length) return -WIN + ply
  const isCap = moves[0].caps.length > 0
  if (ply >= MAXPLY) return evaluate()
  if (depth <= 0 && !isCap) return evaluate()
  if (moves.length === 1 && ply > 0 && depth > 0) depth++
  const idx = h1 & TT_MASK
  let ttm = -1
  if (ttKey[idx] === h2 && ttFlag[idx]) {
    ttm = ttMove[idx]
    if (ply > 0 && ttDepth[idx] >= depth) {
      let v = ttVal[idx]
      if (v > WIN - 1000) v -= ply; else if (v < -WIN + 1000) v += ply
      const f = ttFlag[idx]
      if (f === 1 || (f === 2 && v >= beta) || (f === 3 && v <= alpha)) return v
    }
  }
  const a0 = alpha
  let best = -Infinity, bestCode = -1
  for (const m of order(moves, ttm, ply)) {
    const u = make(m)
    const v = -search(depth - 1, -beta, -alpha, ply + 1)
    unmake(m, u)
    if (aborted) return 0
    if (v > best) { best = v; bestCode = code(m) }
    if (v > alpha) alpha = v
    if (alpha >= beta) {
      if (!m.caps.length) {
        if (killers[ply * 2] !== bestCode) { killers[ply * 2 + 1] = killers[ply * 2]; killers[ply * 2] = bestCode }
        history[m.from * g.N + m.to] += depth * depth
      }
      break
    }
  }
  if (depth >= ttDepth[idx] || ttKey[idx] !== h2) {
    let sv = best
    if (sv > WIN - 1000) sv += ply; else if (sv < -WIN + 1000) sv -= ply
    ttKey[idx] = h2; ttDepth[idx] = Math.max(0, Math.min(127, depth)); ttVal[idx] = sv; ttMove[idx] = bestCode
    ttFlag[idx] = best <= a0 ? 3 : best >= beta ? 2 : 1
  }
  return best
}

// Root search: returns [[move, score]] sorted best first for the deepest completed iteration.
function rootSearch(moves, { time = 1000, maxDepth = 64 } = {}) {
  const start = now()
  deadline = start + time
  aborted = false
  nodes = 0
  let scored = moves.map((m) => [m, 0])
  for (let d = 1; d <= maxDepth; d++) {
    const cur = []
    let alpha = -Infinity
    for (const [m] of scored) {
      const u = make(m)
      let v
      if (cur.length === 0) v = -search(d - 1, -Infinity, Infinity, 1)
      else {
        v = -search(d - 1, -alpha - 1, -alpha, 1)
        if (!aborted && v > alpha) v = -search(d - 1, -Infinity, -alpha, 1)
      }
      unmake(m, u)
      if (aborted) break
      cur.push([m, v])
      if (v > alpha) alpha = v
    }
    if (aborted && cur.length) {
      const bestCur = cur.reduce((a, x) => (x[1] > a[1] ? x : a))
      scored = [bestCur, ...scored.filter((x) => x[0] !== bestCur[0])]
    }
    if (aborted) break
    cur.sort((a, c) => c[1] - a[1])
    scored = cur
    if (Math.abs(scored[0][1]) > WIN - 1000) break
    if (now() - start > time * 0.55) break
  }
  return scored
}

const LEVEL = {
  easy: { time: 150, maxDepth: 2, noise: 90, random: 0.3 },
  medium: { time: 350, maxDepth: 4, noise: 12, random: 0 },
  hard: { time: 1000, maxDepth: 64, noise: 0, random: 0 },
}

export function chooseMove(variant, keys, level = 'hard', opts = {}) {
  load(variant, keys)
  const moves = genMoves(g, b, side)
  if (!moves.length) return null
  if (moves.length === 1) return moveKey(moves[0])
  const L = { ...(LEVEL[level] || LEVEL.hard), ...opts }
  const rnd = opts.rnd || Math.random
  if (L.random && rnd() < L.random) return moveKey(moves[Math.floor(rnd() * moves.length)])
  let scored = rootSearch(moves, L)
  if (L.noise) {
    scored = scored.map(([m, v]) => [m, v + (rnd() * 2 - 1) * L.noise])
    scored.sort((a, c) => c[1] - a[1])
  }
  return moveKey(scored[0][0])
}

export const _internals = { load, evaluate, search, get nodes() { return nodes } }

if (typeof self !== 'undefined' && typeof window === 'undefined' && typeof self.postMessage === 'function') {
  self.onmessage = (e) => {
    const { token, moves, level, variant } = e.data
    let move = null
    try { move = chooseMove(variant, moves, level) } catch { move = null }
    self.postMessage({ token, move })
  }
}
