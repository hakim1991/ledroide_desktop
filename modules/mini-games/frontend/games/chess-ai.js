// Chess AI (Web Worker or plain module): iterative deepening alpha-beta (PVS), quiescence, Zobrist transposition table,
// MVV-LVA / killers / history ordering, null move, late move reductions, small opening book.
import { Position, START_FEN, toUci, mFrom, mTo, mPromo, mFlag, F_EP, VALUE, PAWN, KING } from './chess-core.js'

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
const INF = 32000, MATE = 31000, MATE_BOUND = 30000, MAX_PLY = 64
const TT_BITS = 20, TT_SIZE = 1 << TT_BITS, TT_MASK = TT_SIZE - 1
const ttLo = new Int32Array(TT_SIZE), ttHi = new Int32Array(TT_SIZE), ttMove = new Int32Array(TT_SIZE)
const ttScore = new Int16Array(TT_SIZE), ttDepth = new Int8Array(TT_SIZE), ttFlag = new Int8Array(TT_SIZE)
const EXACT = 1, LOWER = 2, UPPER = 3

const BOOK = [
  'e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7 f1e1 b7b5 a4b3 d7d6',
  'e2e4 e7e5 g1f3 b8c6 f1b5 g8f6 e1g1 f6e4 d2d4 e4d6',
  'e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 c2c3 g8f6 d2d3 d7d6',
  'e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8e7 e1g1 e8g8',
  'e2e4 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 g8f6 d4c6 b7c6',
  'e2e4 e7e5 g1f3 g8f6 f3e5 d7d6 e5f3 f6e4 d2d4 d6d5',
  'e2e4 e7e5 b1c3 g8f6 g1f3 b8c6 f1b5 f8b4',
  'e2e4 e7e5 f2f4 e5f4 g1f3 g7g5 h2h4 g5g4',
  'e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6 c1e3 e7e5',
  'e2e4 c7c5 g1f3 b8c6 d2d4 c5d4 f3d4 g8f6 b1c3 e7e5 d4b5 d7d6',
  'e2e4 c7c5 g1f3 e7e6 d2d4 c5d4 f3d4 b8c6 b1c3 d8c7',
  'e2e4 c7c5 b1c3 b8c6 g2g3 g7g6 f1g2 f8g7 d2d3 d7d6',
  'e2e4 c7c5 c2c3 g8f6 e4e5 f6d5 d2d4 c5d4 g1f3 b8c6',
  'e2e4 e7e6 d2d4 d7d5 b1c3 g8f6 c1g5 f8e7 e4e5 f6d7',
  'e2e4 e7e6 d2d4 d7d5 e4e5 c7c5 c2c3 b8c6 g1f3 d8b6',
  'e2e4 c7c6 d2d4 d7d5 b1c3 d5e4 c3e4 c8f5 e4g3 f5g6',
  'e2e4 c7c6 d2d4 d7d5 e4e5 c8f5 g1f3 e7e6',
  'e2e4 d7d5 e4d5 d8d5 b1c3 d5a5 d2d4 g8f6 g1f3 c8f5',
  'e2e4 g7g6 d2d4 f8g7 b1c3 d7d6 g1f3 g8f6',
  'e2e4 d7d6 d2d4 g8f6 b1c3 g7g6 f2f4 f8g7',
  'd2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 f8e7 e2e3 e8g8 g1f3 b8d7',
  'd2d4 d7d5 c2c4 c7c6 g1f3 g8f6 b1c3 d5c4 a2a4 c8f5',
  'd2d4 d7d5 c2c4 d5c4 g1f3 g8f6 e2e3 e7e6 f1c4 c7c5',
  'd2d4 g8f6 c2c4 e7e6 b1c3 f8b4 e2e3 e8g8 f1d3 d7d5',
  'd2d4 g8f6 c2c4 g7g6 b1c3 f8g7 e2e4 d7d6 g1f3 e8g8 f1e2 e7e5',
  'd2d4 g8f6 c2c4 g7g6 b1c3 d7d5 c4d5 f6d5 e2e4 d5c3 b2c3 f8g7',
  'd2d4 g8f6 c2c4 e7e6 g1f3 b7b6 g2g3 c8b7 f1g2 f8e7',
  'd2d4 g8f6 c2c4 c7c5 d4d5 e7e6 b1c3 e6d5 c4d5 d7d6',
  'd2d4 d7d5 g1f3 g8f6 c1f4 e7e6 e2e3 c7c5 c2c3 b8c6',
  'd2d4 f7f5 g2g3 g8f6 f1g2 g7g6 g1f3 f8g7',
  'c2c4 e7e5 b1c3 g8f6 g1f3 b8c6 g2g3 d7d5 c4d5 f6d5',
  'c2c4 g8f6 b1c3 e7e6 e2e4 d7d5 e4e5 d5d4',
  'c2c4 c7c5 g1f3 g8f6 b1c3 b8c6 g2g3 g7g6',
  'g1f3 d7d5 g2g3 g8f6 f1g2 e7e6 e1g1 f8e7 d2d3 e8g8',
  'g1f3 g8f6 c2c4 e7e6 b1c3 d7d5 d2d4 f8e7',
]
const BOOK_LINES = BOOK.map((l) => l.split(' '))

function bookMove(moves, rnd) {
  const cands = []
  for (const line of BOOK_LINES) {
    if (line.length <= moves.length) continue
    let ok = true
    for (let i = 0; i < moves.length; i++) if (line[i] !== moves[i]) { ok = false; break }
    if (ok) cands.push(line[moves.length])
  }
  return cands.length ? cands[Math.floor(rnd() * cands.length)] : null
}

class Search {
  constructor(pos, { timeMs = 1000, maxDepth = MAX_PLY, rootNoise = 0, rnd = Math.random } = {}) {
    this.pos = pos
    this.timeMs = timeMs
    this.maxDepth = maxDepth
    this.rootNoise = rootNoise
    this.rnd = rnd
    this.nodes = 0
    this.stop = false
    this.killers = new Int32Array(MAX_PLY * 2 + 8)
    this.history = new Int32Array(2 * 128 * 128)
    this.moves = []
    this.scores = []
    for (let i = 0; i < MAX_PLY + 8; i++) { this.moves.push(new Int32Array(256)); this.scores.push(new Int32Array(256)) }
    this.rootPly = pos.ply
  }

  timeUp() {
    if ((++this.nodes & 2047) === 0 && now() > this.deadline) this.stop = true
    return this.stop
  }

  isDraw() {
    const p = this.pos
    if (p.half >= 100) return true
    for (let i = p.ply - 4; i >= 0 && i >= p.ply - p.half; i -= 2) if (p.hLo[i] === p.lo && p.hHi[i] === p.hi) return true
    return false
  }

  orderMoves(n, ply, ttm) {
    const p = this.pos, b = p.b, moves = this.moves[ply], sc = this.scores[ply]
    const k1 = this.killers[ply * 2], k2 = this.killers[ply * 2 + 1], side = p.side
    for (let i = 0; i < n; i++) {
      const m = moves[i]
      if (m === ttm) { sc[i] = 1 << 30; continue }
      const to = mTo(m), from = mFrom(m)
      const victim = mFlag(m) === F_EP ? PAWN : Math.abs(b[to])
      if (victim) sc[i] = 1e8 + VALUE[victim] * 16 - Math.abs(b[from])
      else if (mPromo(m)) sc[i] = 9e7 + mPromo(m)
      else if (m === k1) sc[i] = 8e7
      else if (m === k2) sc[i] = 7e7
      else sc[i] = this.history[(side << 14) | (from << 7) | to]
    }
  }

  pick(i, n, ply) {
    const moves = this.moves[ply], sc = this.scores[ply]
    let best = i
    for (let j = i + 1; j < n; j++) if (sc[j] > sc[best]) best = j
    if (best !== i) {
      const m = moves[i]; moves[i] = moves[best]; moves[best] = m
      const s = sc[i]; sc[i] = sc[best]; sc[best] = s
    }
    return moves[i]
  }

  quiesce(alpha, beta, ply) {
    if (this.timeUp()) return 0
    const p = this.pos
    const stand = p.evaluate()
    if (ply >= MAX_PLY) return stand
    if (stand >= beta) return stand
    if (stand > alpha) alpha = stand
    const n = p.gen(this.moves[ply], 0, true)
    this.orderMoves(n, ply, 0)
    for (let i = 0; i < n; i++) {
      const m = this.pick(i, n, ply)
      const victim = mFlag(m) === F_EP ? PAWN : Math.abs(p.b[mTo(m)])
      if (!mPromo(m) && stand + VALUE[victim] + 200 < alpha) continue
      if (!p.make(m)) continue
      const s = -this.quiesce(-beta, -alpha, ply + 1)
      p.unmake()
      if (this.stop) return 0
      if (s > alpha) { alpha = s; if (s >= beta) return s }
    }
    return alpha
  }

  search(depth, alpha, beta, ply, allowNull) {
    const p = this.pos
    if (ply > 0) {
      if (this.isDraw()) return 0
      alpha = Math.max(alpha, -MATE + ply)
      beta = Math.min(beta, MATE - ply - 1)
      if (alpha >= beta) return alpha
    }
    const inCheck = p.inCheck()
    if (inCheck && ply < MAX_PLY - 2) depth++
    if (depth <= 0 || ply >= MAX_PLY - 2) return this.quiesce(alpha, beta, ply)
    if (this.timeUp()) return 0

    const idx = p.lo & TT_MASK
    let ttm = 0
    if (ttLo[idx] === p.lo && ttHi[idx] === p.hi) {
      ttm = ttMove[idx]
      if (ply > 0 && ttDepth[idx] >= depth) {
        let s = ttScore[idx]
        if (s > MATE_BOUND) s -= ply; else if (s < -MATE_BOUND) s += ply
        const f = ttFlag[idx]
        if (f === EXACT || (f === LOWER && s >= beta) || (f === UPPER && s <= alpha)) return s
      }
    }

    const pv = beta - alpha > 1
    if (!pv && !inCheck && ply > 0) {
      const ev = p.evaluate()
      if (depth <= 3 && ev - 120 * depth >= beta && Math.abs(beta) < MATE_BOUND) return ev
      if (allowNull && depth >= 3 && ev >= beta && this.hasPieces()) {
        p.makeNull()
        const s = -this.search(depth - 1 - (depth >= 6 ? 3 : 2), -beta, -beta + 1, ply + 1, false)
        p.unmakeNull()
        if (this.stop) return 0
        if (s >= beta) return Math.abs(s) > MATE_BOUND ? beta : s
      }
    }

    const moves = this.moves[ply]
    const n = p.gen(moves, 0)
    this.orderMoves(n, ply, ttm)
    const a0 = alpha
    let best = -INF, bestMove = 0, legal = 0
    for (let i = 0; i < n; i++) {
      const m = this.pick(i, n, ply)
      const quiet = !p.b[mTo(m)] && !mPromo(m) && mFlag(m) !== F_EP
      if (!p.make(m)) continue
      legal++
      const gives = p.inCheck()
      let s
      if (legal === 1) s = -this.search(depth - 1, -beta, -alpha, ply + 1, true)
      else {
        let r = 0
        if (depth >= 3 && quiet && !inCheck && !gives && legal > 3) r = legal > 10 ? 2 : 1
        s = -this.search(depth - 1 - r, -alpha - 1, -alpha, ply + 1, true)
        if (s > alpha && r) s = -this.search(depth - 1, -alpha - 1, -alpha, ply + 1, true)
        if (s > alpha && s < beta) s = -this.search(depth - 1, -beta, -alpha, ply + 1, true)
      }
      p.unmake()
      if (this.stop) return 0
      if (s > best) {
        best = s; bestMove = m
        if (s > alpha) {
          alpha = s
          if (s >= beta) {
            if (quiet) {
              if (this.killers[ply * 2] !== m) { this.killers[ply * 2 + 1] = this.killers[ply * 2]; this.killers[ply * 2] = m }
              const hi = (p.side << 14) | (mFrom(m) << 7) | mTo(m)
              this.history[hi] = Math.min(6e7, this.history[hi] + depth * depth)
            }
            break
          }
        }
      }
    }
    if (!legal) return inCheck ? -MATE + ply : 0
    let st = best
    if (st > MATE_BOUND) st += ply; else if (st < -MATE_BOUND) st -= ply
    if (ttDepth[idx] <= depth || ttLo[idx] !== p.lo) {
      ttLo[idx] = p.lo; ttHi[idx] = p.hi; ttMove[idx] = bestMove; ttScore[idx] = st; ttDepth[idx] = depth
      ttFlag[idx] = best >= beta ? LOWER : best > a0 ? EXACT : UPPER
    }
    return best
  }

  hasPieces() {
    const b = this.pos.b, sgn = this.pos.side ? -1 : 1
    for (let i = 0; i < 128; i++) { if (i & 0x88) { i += 7; continue } const q = b[i] * sgn; if (q > PAWN && q < KING) return true }
    return false
  }

  // Root: returns { move, score, depth }. Every root move gets an exact score when rootNoise > 0 (weaker levels).
  run() {
    const p = this.pos
    this.deadline = now() + this.timeMs
    const root = p.legalMoves()
    if (!root.length) return { move: 0, score: 0, depth: 0 }
    if (root.length === 1) return { move: root[0], score: 0, depth: 0 }
    let order = root.map((m) => ({ m, s: 0 }))
    let best = order[0], bestDepth = 0
    for (let d = 1; d <= this.maxDepth; d++) {
      let alpha = -INF
      const scored = []
      let done = true
      for (let i = 0; i < order.length; i++) {
        const m = order[i].m
        p.make(m)
        let s
        if (this.rootNoise || i === 0) s = -this.search(d - 1, -INF, this.rootNoise ? INF : -alpha, 1, true)
        else {
          s = -this.search(d - 1, -alpha - 1, -alpha, 1, true)
          if (s > alpha && !this.stop) s = -this.search(d - 1, -INF, -alpha, 1, true)
        }
        p.unmake()
        if (this.stop) { done = false; break }
        scored.push({ m, s: i === 0 || s > alpha || this.rootNoise ? s : -INF + i })
        if (s > alpha) alpha = s
      }
      if (!done) {
        if (scored.length && !this.rootNoise) best = scored.reduce((a, x) => (x.s > a.s ? x : a))
        break
      }
      scored.sort((a, b) => b.s - a.s)
      order = scored.concat(order.filter((x) => !scored.includes(x) && !scored.some((y) => y.m === x.m)))
      best = scored[0]
      bestDepth = d
      this.lastScores = scored
      if (Math.abs(best.s) > MATE_BOUND && d >= 2 && !this.rootNoise) break
      if (now() > this.deadline - this.timeMs * 0.55) break
    }
    if (this.rootNoise && this.lastScores) {
      let pick = this.lastScores[0], top = -INF
      for (const x of this.lastScores) {
        const v = Math.abs(x.s) > MATE_BOUND ? x.s : x.s + (this.rnd() * 2 - 1) * this.rootNoise
        if (v > top) { top = v; pick = x }
      }
      return { move: pick.m, score: pick.s, depth: bestDepth }
    }
    return { move: best.m, score: best.s, depth: bestDepth }
  }
}

export const LEVEL_CONF = {
  easy: { timeMs: 400, maxDepth: 2, rootNoise: 160, bookPlies: 6 },
  medium: { timeMs: 500, maxDepth: 4, rootNoise: 25, bookPlies: 12 },
  hard: { timeMs: 1500, maxDepth: MAX_PLY, rootNoise: 0, bookPlies: 16 },
}

export function buildPosition(start, moves) {
  const pos = Position.fromFen(start || START_FEN)
  for (const u of moves) {
    const m = pos.legalMoves().find((x) => toUci(x) === u)
    if (m == null) throw new Error(`illegal ${u}`)
    pos.make(m)
  }
  return pos
}

export function chooseMove({ start = null, moves = [], level = 'medium', timeMs, rnd = Math.random } = {}) {
  const conf = { ...(LEVEL_CONF[level] || LEVEL_CONF.medium) }
  if (timeMs) conf.timeMs = timeMs
  const pos = buildPosition(start, moves)
  const legal = pos.legalMoves().map(toUci)
  if (!legal.length) return { move: null }
  if (!start && moves.length < conf.bookPlies) {
    const bm = bookMove(moves, rnd)
    if (bm && legal.includes(bm)) return { move: bm, book: true }
  }
  const maxDepth = level === 'easy' ? (rnd() < 0.5 ? 1 : 2) : conf.maxDepth
  const s = new Search(pos, { ...conf, maxDepth, rnd })
  const r = s.run()
  return { move: toUci(r.move), score: r.score, depth: r.depth, nodes: s.nodes }
}

if (typeof self !== 'undefined' && typeof window === 'undefined' && typeof self.postMessage === 'function') {
  self.onmessage = (e) => {
    const { token, moves, level, start } = e.data
    const r = chooseMove({ start, moves, level })
    self.postMessage({ token, move: r.move })
  }
}
