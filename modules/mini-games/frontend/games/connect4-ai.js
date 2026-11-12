// Connect four engine. Bitboard: bit = row * 8 + col (col 7 is an always-empty sentinel), rows 0-2 in a "lo"
// 24-bit word and rows 3-5 in a "hi" word, so every operation stays in 32-bit integer arithmetic.

const M24 = 0xFFFFFF
const BOARD = 0x7F7F7F
const WIN = 10000
const ORDER = [3, 2, 4, 1, 5, 0, 6]

let RL = 0, RH = 0
function shl(lo, hi, k) {
  if (k < 24) { RL = (lo << k) & M24; RH = ((hi << k) | (lo >>> (24 - k))) & M24 } else { RL = 0; RH = (lo << (k - 24)) & M24 }
}
function shr(lo, hi, k) {
  if (k < 24) { RL = ((lo >>> k) | (hi << (24 - k))) & M24; RH = hi >>> k } else { RL = hi >>> (k - 24); RH = 0 }
}

let WL = 0, WH = 0
const DIRS = [1, 7, 9]
// Empty cells that would complete four for the stones (pl, ph).
function winCells(pl, ph, ml, mh) {
  shl(pl, ph, 8); let al = RL, ah = RH
  shl(pl, ph, 16); al &= RL; ah &= RH
  shl(pl, ph, 24); let rl = al & RL, rh = ah & RH
  for (const d of DIRS) {
    shl(pl, ph, d); let xl = RL, xh = RH
    shl(pl, ph, 2 * d); xl &= RL; xh &= RH
    shl(pl, ph, 3 * d); rl |= xl & RL; rh |= xh & RH
    shr(pl, ph, d); rl |= xl & RL; rh |= xh & RH
    shr(pl, ph, d); let yl = RL, yh = RH
    shr(pl, ph, 2 * d); yl &= RL; yh &= RH
    shl(pl, ph, d); rl |= yl & RL; rh |= yh & RH
    shr(pl, ph, 3 * d); rl |= yl & RL; rh |= yh & RH
  }
  WL = rl & BOARD & ~ml; WH = rh & BOARD & ~mh
}

function pop24(x) {
  x -= (x >>> 1) & 0x555555
  x = (x & 0x333333) + ((x >>> 2) & 0x333333)
  x = (x + (x >>> 4)) & 0x0F0F0F
  return (x * 0x010101 >>> 16) & 0xFF
}

const bl = new Int32Array(2), bh = new Int32Array(2), hgt = new Int8Array(7)
let n = 0, z1 = 0, z2 = 0
const Z1 = new Int32Array(96), Z2 = new Int32Array(96)
{
  let s = 0x9E3779B9
  const r = () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return s | 0 }
  for (let i = 0; i < 96; i++) { Z1[i] = r(); Z2[i] = r() }
}

function play(c) {
  const r = hgt[c], p = n & 1
  if (r < 3) bl[p] |= 1 << (r * 8 + c); else bh[p] |= 1 << ((r - 3) * 8 + c)
  z1 ^= Z1[p * 48 + r * 8 + c]; z2 ^= Z2[p * 48 + r * 8 + c]
  hgt[c] = r + 1; n++
}
function unplay(c) {
  n--
  const r = --hgt[c], p = n & 1
  if (r < 3) bl[p] &= ~(1 << (r * 8 + c)); else bh[p] &= ~(1 << ((r - 3) * 8 + c))
  z1 ^= Z1[p * 48 + r * 8 + c]; z2 ^= Z2[p * 48 + r * 8 + c]
}
const colBitLo = (c) => (hgt[c] < 3 ? 1 << (hgt[c] * 8 + c) : 0)
const colBitHi = (c) => (hgt[c] >= 3 && hgt[c] < 6 ? 1 << ((hgt[c] - 3) * 8 + c) : 0)

function reset(moves) {
  bl[0] = bl[1] = bh[0] = bh[1] = 0; hgt.fill(0); n = 0; z1 = z2 = 0
  for (const c of moves) play(c)
}

const ODD_LO = 0x7F007F, ODD_HI = 0x007F00 // rows 1, 3, 5 counted from 1 (good threats for the first player)
const CENTER_LO = 0x080808, NEAR_LO = 0x141414

function evaluate() {
  const p = n & 1, o = p ^ 1, ml = bl[0] | bl[1], mh = bh[0] | bh[1]
  winCells(bl[p], bh[p], ml, mh); const al = WL, ah = WH
  winCells(bl[o], bh[o], ml, mh); const ol = WL, oh = WH
  const goodL = (q) => (q === 0 ? ODD_LO : ~ODD_LO & BOARD), goodH = (q) => (q === 0 ? ODD_HI : ~ODD_HI & BOARD)
  let s = 0
  s += 6 * (pop24(al & goodL(p)) + pop24(ah & goodH(p))) + 3 * (pop24(al & ~goodL(p)) + pop24(ah & ~goodH(p)))
  s -= 6 * (pop24(ol & goodL(o)) + pop24(oh & goodH(o))) + 3 * (pop24(ol & ~goodL(o)) + pop24(oh & ~goodH(o)))
  s += 3 * (pop24(bl[p] & CENTER_LO) + pop24(bh[p] & CENTER_LO) - pop24(bl[o] & CENTER_LO) - pop24(bh[o] & CENTER_LO))
  s += pop24(bl[p] & NEAR_LO) + pop24(bh[p] & NEAR_LO) - pop24(bl[o] & NEAR_LO) - pop24(bh[o] & NEAR_LO)
  return s
}

const TT_BITS = 20, TT_SIZE = 1 << TT_BITS, TT_MASK = TT_SIZE - 1
const ttKey = new Int32Array(TT_SIZE), ttVal = new Int16Array(TT_SIZE), ttDepth = new Int8Array(TT_SIZE)
const ttFlag = new Int8Array(TT_SIZE), ttMove = new Int8Array(TT_SIZE)
const EXACT = 1, LOWER = 2, UPPER = 3

let nodes = 0, deadline = Infinity, aborted = false
const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function negamax(alpha, beta, depth) {
  if ((++nodes & 4095) === 0 && now() > deadline) aborted = true
  if (aborted) return 0
  const p = n & 1, o = p ^ 1, ml = bl[0] | bl[1], mh = bh[0] | bh[1]
  let pl = 0, ph = 0
  for (let c = 0; c < 7; c++) { pl |= colBitLo(c); ph |= colBitHi(c) }
  winCells(bl[p], bh[p], ml, mh)
  if ((WL & pl) | (WH & ph)) return WIN - n - 1
  if (n >= 41) return 0
  winCells(bl[o], bh[o], ml, mh)
  const ol = WL, oh = WH
  let fl = pl & ol, fh = ph & oh
  if (fl | fh) {
    if (pop24(fl) + pop24(fh) > 1) return -(WIN - n - 2)
    pl = fl; ph = fh
  }
  shr(ol, oh, 8)
  pl &= ~RL; ph &= ~RH
  if (!(pl | ph)) return -(WIN - n - 2)

  const maxScore = WIN - n - 3
  if (beta > maxScore) { beta = maxScore; if (alpha >= beta) return beta }

  const idx = z1 & TT_MASK
  let ttm = -1
  if (ttKey[idx] === z2) {
    ttm = ttMove[idx]
    if (ttDepth[idx] >= depth) {
      const v = ttVal[idx], f = ttFlag[idx]
      if (f === EXACT) return v
      if (f === LOWER && v >= beta) return v
      if (f === UPPER && v <= alpha) return v
    }
  }
  if (depth <= 0) return evaluate()

  const cols = [], keys = []
  for (const c of ORDER) {
    const ll = colBitLo(c) & pl, hh = colBitHi(c) & ph
    if (!(ll | hh)) continue
    let k
    if (c === ttm) k = 1000
    else {
      winCells(bl[p] | ll, bh[p] | hh, ml | ll, mh | hh)
      k = pop24(WL) + pop24(WH)
    }
    let i = cols.length
    while (i > 0 && keys[i - 1] < k) { cols[i] = cols[i - 1]; keys[i] = keys[i - 1]; i-- }
    cols[i] = c; keys[i] = k
  }

  const a0 = alpha
  let best = -WIN, bestMove = cols[0]
  for (const c of cols) {
    play(c)
    const v = -negamax(-beta, -alpha, depth - 1)
    unplay(c)
    if (aborted) return 0
    if (v > best) { best = v; bestMove = c }
    if (v > alpha) alpha = v
    if (alpha >= beta) break
  }
  ttKey[idx] = z2; ttVal[idx] = best; ttDepth[idx] = depth; ttMove[idx] = bestMove
  ttFlag[idx] = best <= a0 ? UPPER : best >= beta ? LOWER : EXACT
  return best
}

const legal = () => ORDER.filter((c) => hgt[c] < 6)

function rootScores(depth) {
  const p = n & 1, ml = bl[0] | bl[1], mh = bh[0] | bh[1]
  const out = []
  for (const c of legal()) {
    winCells(bl[p], bh[p], ml, mh)
    if ((WL & colBitLo(c)) | (WH & colBitHi(c))) { out.push([c, WIN - n - 1]); continue }
    play(c)
    const v = -negamax(-WIN, WIN, depth - 1)
    unplay(c)
    if (aborted) return null
    out.push([c, v])
  }
  return out
}

function immediate() {
  const p = n & 1, o = p ^ 1, ml = bl[0] | bl[1], mh = bh[0] | bh[1]
  winCells(bl[p], bh[p], ml, mh); const myL = WL, myH = WH
  winCells(bl[o], bh[o], ml, mh); const opL = WL, opH = WH
  const win = legal().find((c) => (myL & colBitLo(c)) | (myH & colBitHi(c)))
  const block = legal().find((c) => (opL & colBitLo(c)) | (opH & colBitHi(c)))
  return { win, block }
}

function searchBest(maxTime, maxDepth) {
  deadline = now() + maxTime; aborted = false; nodes = 0
  let best = null, bestScore = 0
  const moves = legal()
  if (moves.length === 1) return { move: moves[0], score: 0, depth: 0 }
  let order = moves.slice()
  let reached = 0
  for (let d = 1; d <= Math.min(maxDepth, 42 - n); d++) {
    const sc = []
    let alpha = -WIN
    for (const c of order) {
      play(c)
      let v
      if (isFour((n - 1) & 1)) v = WIN - n
      else v = -negamax(-WIN, -alpha + 1, d - 1)
      unplay(c)
      if (aborted) break
      sc.push([c, v])
      if (v > alpha) alpha = v
    }
    if (aborted) break
    sc.sort((a, b) => b[1] - a[1])
    best = sc[0][0]; bestScore = sc[0][1]; reached = d
    order = sc.map((x) => x[0])
    if (Math.abs(bestScore) >= WIN - 50) break
  }
  if (best === null) best = order[0]
  return { move: best, score: bestScore, depth: reached, nodes }
}

function isFour(p) {
  const l = bl[p], hh = bh[p]
  for (const s of [1, 7, 8, 9]) {
    shr(l, hh, s); const ml = l & RL, mh = hh & RH
    shr(ml, mh, 2 * s)
    if ((ml & RL) | (mh & RH)) return true
  }
  return false
}

function rand(arr) { return arr[Math.floor(Math.random() * arr.length)] }
function weighted(cols) {
  const w = cols.map((c) => 4 - Math.abs(3 - c))
  let t = Math.random() * w.reduce((a, b) => a + b, 0)
  for (let i = 0; i < cols.length; i++) { t -= w[i]; if (t <= 0) return cols[i] }
  return cols[cols.length - 1]
}

export function chooseMove(moves, level = 'hard', { time = 1000 } = {}) {
  reset(moves)
  const cols = legal()
  if (!cols.length) return -1
  const { win, block } = immediate()
  if (level === 'easy') {
    if (win != null && Math.random() < 0.85) return win
    if (block != null && Math.random() < 0.55) return block
    if (Math.random() < 0.45) return weighted(cols)
    deadline = Infinity; aborted = false
    const sc = rootScores(2)
    const top = Math.max(...sc.map((x) => x[1]))
    return rand(sc.filter((x) => x[1] >= top - 4).map((x) => x[0]))
  }
  if (win != null) return win
  if (block != null) return block
  if (level === 'medium') {
    deadline = Infinity; aborted = false
    const sc = rootScores(5).sort((a, b) => b[1] - a[1])
    const top = sc[0][1]
    if (Math.abs(top) < WIN - 50 && sc.length > 1 && Math.random() < 0.15 && sc[1][1] > -(WIN - 50)) return sc[1][0]
    return rand(sc.filter((x) => x[1] === top).map((x) => x[0]))
  }
  if (moves.length === 0) return 3
  return searchBest(time, 42).move
}

export const _internals = { searchBest, reset, evaluate }

if (typeof self !== 'undefined' && typeof window === 'undefined' && typeof self.postMessage === 'function') {
  self.onmessage = (e) => {
    const { token, moves, level } = e.data
    const move = chooseMove(moves, level)
    self.postMessage({ token, move })
  }
}
