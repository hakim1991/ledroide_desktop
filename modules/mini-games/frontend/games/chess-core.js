// Chess rules shared by the UI and the AI worker. 0x88 board: square = rank * 16 + file (rank 0 = rank 1).
// Pieces: 1 pawn, 2 knight, 3 bishop, 4 rook, 5 queen, 6 king; white > 0, black < 0. side: 0 = white, 1 = black.
// Move (int): from | to << 7 | promo << 14 | flag << 17.

export const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
export const PAWN = 1, KNIGHT = 2, BISHOP = 3, ROOK = 4, QUEEN = 5, KING = 6
export const F_DOUBLE = 1, F_EP = 2, F_CASTLE = 3
const STACK = 4096

export const mFrom = (m) => m & 127
export const mTo = (m) => (m >> 7) & 127
export const mPromo = (m) => (m >> 14) & 7
export const mFlag = (m) => m >> 17
const mk = (from, to, promo = 0, flag = 0) => from | (to << 7) | (promo << 14) | (flag << 17)

export const sqName = (s) => 'abcdefgh'[s & 7] + ((s >> 4) + 1)
export const parseSq = (str) => {
  const f = 'abcdefgh'.indexOf(str[0]), r = +str[1] - 1
  return f < 0 || !(r >= 0 && r < 8) || str.length !== 2 ? -1 : r * 16 + f
}
export const toUci = (m) => sqName(mFrom(m)) + sqName(mTo(m)) + (mPromo(m) ? ' nbrq'[mPromo(m) - 1] : '')
export const SQUARES = []
for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) SQUARES.push(r * 16 + f)

const N_STEPS = [33, 31, 18, 14, -14, -18, -31, -33]
const K_STEPS = [1, -1, 16, -16, 15, 17, -15, -17]
const DIAG = [15, 17, -15, -17]
const ORTH = [1, -1, 16, -16]
const CR_MASK = new Uint8Array(128).fill(15)
CR_MASK[0] = 15 & ~2; CR_MASK[7] = 15 & ~1; CR_MASK[4] = 15 & ~3
CR_MASK[112] = 15 & ~8; CR_MASK[119] = 15 & ~4; CR_MASK[116] = 15 & ~12

const Z = (() => {
  let s = 0x2F6B1D3A
  const r = () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return s | 0 }
  const t = (n) => { const a = new Int32Array(n); for (let i = 0; i < n; i++) a[i] = r(); return a }
  return { pLo: t(12 * 128), pHi: t(12 * 128), cLo: t(16), cHi: t(16), eLo: t(8), eHi: t(8), sLo: r(), sHi: r() }
})()
const zIdx = (p) => ((p > 0 ? p - 1 : 5 - p) << 7)

// Piece-square tables (rank 8 first, white's point of view), material included below.
const PST = {
  1: [0, 0, 0, 0, 0, 0, 0, 0, 50, 50, 50, 50, 50, 50, 50, 50, 10, 10, 20, 30, 30, 20, 10, 10, 5, 5, 10, 25, 25, 10, 5, 5,
    0, 0, 0, 20, 20, 0, 0, 0, 5, -5, -10, 0, 0, -10, -5, 5, 5, 10, 10, -20, -20, 10, 10, 5, 0, 0, 0, 0, 0, 0, 0, 0],
  2: [-50, -40, -30, -30, -30, -30, -40, -50, -40, -20, 0, 0, 0, 0, -20, -40, -30, 0, 10, 15, 15, 10, 0, -30, -30, 5, 15, 20, 20, 15, 5, -30,
    -30, 0, 15, 20, 20, 15, 0, -30, -30, 5, 10, 15, 15, 10, 5, -30, -40, -20, 0, 5, 5, 0, -20, -40, -50, -40, -30, -30, -30, -30, -40, -50],
  3: [-20, -10, -10, -10, -10, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 10, 10, 5, 0, -10, -10, 5, 5, 10, 10, 5, 5, -10,
    -10, 0, 10, 10, 10, 10, 0, -10, -10, 10, 10, 10, 10, 10, 10, -10, -10, 5, 0, 0, 0, 0, 5, -10, -20, -10, -10, -10, -10, -10, -10, -20],
  4: [0, 0, 0, 0, 0, 0, 0, 0, 5, 10, 10, 10, 10, 10, 10, 5, -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5, -5, 0, 0, 0, 0, 0, 0, -5, 0, 0, 0, 5, 5, 0, 0, 0],
  5: [-20, -10, -10, -5, -5, -10, -10, -20, -10, 0, 0, 0, 0, 0, 0, -10, -10, 0, 5, 5, 5, 5, 0, -10, -5, 0, 5, 5, 5, 5, 0, -5,
    0, 0, 5, 5, 5, 5, 0, -5, -10, 5, 5, 5, 5, 5, 0, -10, -10, 0, 5, 0, 0, 0, 0, -10, -20, -10, -10, -5, -5, -10, -10, -20],
  6: [-30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30, -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20, -10, -20, -20, -20, -20, -20, -20, -10, 20, 20, 0, 0, 0, 0, 20, 20, 20, 30, 10, 0, 0, 10, 30, 20],
}
const KING_EG = [-50, -40, -30, -20, -20, -30, -40, -50, -30, -20, -10, 0, 0, -10, -20, -30, -30, -10, 20, 30, 30, 20, -10, -30, -30, -10, 30, 40, 40, 30, -10, -30,
  -30, -10, 30, 40, 40, 30, -10, -30, -30, -10, 20, 30, 30, 20, -10, -30, -30, -30, 0, 0, 0, 0, -30, -30, -50, -30, -30, -30, -30, -30, -30, -50]
const PAWN_EG_RANK = [0, 0, 5, 10, 20, 35, 60, 0]
export const VALUE = [0, 100, 320, 330, 500, 900, 0]
const VALUE_EG = [0, 120, 300, 320, 520, 940, 0]
const PHASE = [0, 0, 1, 1, 2, 4, 0]
const MG = [], EG = []
for (let t = 1; t <= 6; t++) {
  MG[t] = new Int16Array(64); EG[t] = new Int16Array(64)
  for (let i = 0; i < 64; i++) {
    MG[t][i] = VALUE[t] + PST[t][i]
    EG[t][i] = VALUE_EG[t] + (t === 6 ? KING_EG[i] : t === 1 ? PAWN_EG_RANK[7 - (i >> 3)] : PST[t][i])
  }
}
const PASSED_MG = [0, 5, 10, 15, 25, 40, 60, 0], PASSED_EG = [0, 10, 20, 35, 60, 100, 150, 0]

export class Position {
  constructor() {
    this.b = new Int8Array(128)
    this.side = 0
    this.castle = 0
    this.ep = -1
    this.half = 0
    this.full = 1
    this.kings = new Int32Array(2)
    this.mg = new Int32Array(2)
    this.eg = new Int32Array(2)
    this.phase = 0
    this.lo = 0; this.hi = 0
    this.ply = 0
    this.uMove = new Int32Array(STACK); this.uCap = new Int8Array(STACK); this.uCastle = new Int8Array(STACK)
    this.uEp = new Int16Array(STACK); this.uHalf = new Int16Array(STACK)
    this.hLo = new Int32Array(STACK); this.hHi = new Int32Array(STACK)
    this.buf = new Int32Array(256)
  }

  static fromFen(fen) {
    const p = new Position()
    const parts = String(fen || '').trim().split(/\s+/)
    const rows = (parts[0] || '').split('/')
    if (rows.length !== 8) throw new Error('FEN invalide : il faut 8 rangées.')
    const kings = [0, 0]
    for (let i = 0; i < 8; i++) {
      const r = 7 - i
      let f = 0
      for (const ch of rows[i]) {
        if (/[1-8]/.test(ch)) { f += +ch; continue }
        const t = 'pnbrqk'.indexOf(ch.toLowerCase()) + 1
        if (!t || f > 7) throw new Error(`FEN invalide : rangée ${8 - i}.`)
        const pc = ch === ch.toUpperCase() ? t : -t
        if (t === PAWN && (r === 0 || r === 7)) throw new Error('FEN invalide : pion sur la première ou la dernière rangée.')
        if (t === KING) { kings[pc > 0 ? 0 : 1]++; p.kings[pc > 0 ? 0 : 1] = r * 16 + f }
        p.put(r * 16 + f, pc)
        f++
      }
      if (f !== 8) throw new Error(`FEN invalide : rangée ${8 - i}.`)
    }
    if (kings[0] !== 1 || kings[1] !== 1) throw new Error('FEN invalide : il faut exactement un roi de chaque couleur.')
    const s = parts[1] || 'w'
    if (s !== 'w' && s !== 'b') throw new Error('FEN invalide : trait « w » ou « b » attendu.')
    p.side = s === 'w' ? 0 : 1
    let cr = 0
    const c = parts[2] || '-'
    if (c !== '-' && !/^[KQkq]+$/.test(c)) throw new Error('FEN invalide : droits de roque.')
    if (c.includes('K') && p.b[4] === KING && p.b[7] === ROOK) cr |= 1
    if (c.includes('Q') && p.b[4] === KING && p.b[0] === ROOK) cr |= 2
    if (c.includes('k') && p.b[116] === -KING && p.b[119] === -ROOK) cr |= 4
    if (c.includes('q') && p.b[116] === -KING && p.b[112] === -ROOK) cr |= 8
    p.castle = cr
    const e = parts[3] || '-'
    if (e !== '-') {
      const sq = parseSq(e)
      if (sq < 0) throw new Error('FEN invalide : case en passant.')
      const sgn = p.side ? -1 : 1
      if ((sq >> 4) === (p.side ? 2 : 5) && p.b[sq] === 0 && p.b[sq - 16 * sgn] === -sgn && p.b[sq + 16 * sgn] === 0) p.ep = p.epIfCapturable(sq, sq - 16 * sgn)
    }
    p.half = Math.max(0, parseInt(parts[4], 10) || 0)
    p.full = Math.max(1, parseInt(parts[5], 10) || 1)
    if (p.attacked(p.kings[p.side ^ 1], p.side)) throw new Error('FEN invalide : le camp qui n’a pas le trait est en échec.')
    p.rehash()
    return p
  }

  rehash() {
    let lo = 0, hi = 0
    for (const s of SQUARES) if (this.b[s]) { lo ^= Z.pLo[zIdx(this.b[s]) + s]; hi ^= Z.pHi[zIdx(this.b[s]) + s] }
    lo ^= Z.cLo[this.castle]; hi ^= Z.cHi[this.castle]
    if (this.ep >= 0) { lo ^= Z.eLo[this.ep & 7]; hi ^= Z.eHi[this.ep & 7] }
    if (this.side) { lo ^= Z.sLo; hi ^= Z.sHi }
    this.lo = lo; this.hi = hi
    this.hLo[this.ply] = lo; this.hHi[this.ply] = hi
  }

  epIfCapturable(epSq, pawnSq) {
    const opp = this.b[pawnSq] > 0 ? -PAWN : PAWN
    return (!((pawnSq - 1) & 0x88) && this.b[pawnSq - 1] === opp) || (!((pawnSq + 1) & 0x88) && this.b[pawnSq + 1] === opp) ? epSq : -1
  }

  clone() { return Position.fromFen(this.fen()) }

  fen() {
    let out = ''
    for (let r = 7; r >= 0; r--) {
      let empty = 0
      for (let f = 0; f < 8; f++) {
        const p = this.b[r * 16 + f]
        if (!p) { empty++; continue }
        if (empty) { out += empty; empty = 0 }
        const ch = ' pnbrqk'[Math.abs(p)]
        out += p > 0 ? ch.toUpperCase() : ch
      }
      if (empty) out += empty
      if (r) out += '/'
    }
    const c = (this.castle & 1 ? 'K' : '') + (this.castle & 2 ? 'Q' : '') + (this.castle & 4 ? 'k' : '') + (this.castle & 8 ? 'q' : '')
    return `${out} ${this.side ? 'b' : 'w'} ${c || '-'} ${this.ep >= 0 ? sqName(this.ep) : '-'} ${this.half} ${this.full}`
  }

  put(sq, p) {
    this.b[sq] = p
    const s = p < 0 ? 1 : 0, t = s ? -p : p, i = s ? ((sq >> 4) << 3) | (sq & 7) : ((7 - (sq >> 4)) << 3) | (sq & 7)
    this.mg[s] += MG[t][i]; this.eg[s] += EG[t][i]; this.phase += PHASE[t]
    this.lo ^= Z.pLo[zIdx(p) + sq]; this.hi ^= Z.pHi[zIdx(p) + sq]
  }

  remove(sq) {
    const p = this.b[sq]
    this.b[sq] = 0
    const s = p < 0 ? 1 : 0, t = s ? -p : p, i = s ? ((sq >> 4) << 3) | (sq & 7) : ((7 - (sq >> 4)) << 3) | (sq & 7)
    this.mg[s] -= MG[t][i]; this.eg[s] -= EG[t][i]; this.phase -= PHASE[t]
    this.lo ^= Z.pLo[zIdx(p) + sq]; this.hi ^= Z.pHi[zIdx(p) + sq]
  }

  attacked(sq, by) {
    const b = this.b, sgn = by ? -1 : 1
    if (by === 0) {
      if ((!((sq - 15) & 0x88) && b[sq - 15] === PAWN) || (!((sq - 17) & 0x88) && b[sq - 17] === PAWN)) return true
    } else if ((!((sq + 15) & 0x88) && b[sq + 15] === -PAWN) || (!((sq + 17) & 0x88) && b[sq + 17] === -PAWN)) return true
    const n = KNIGHT * sgn, k = KING * sgn, bb = BISHOP * sgn, rr = ROOK * sgn, qq = QUEEN * sgn
    for (let i = 0; i < 8; i++) {
      const t = sq + N_STEPS[i]
      if (!(t & 0x88) && b[t] === n) return true
      const u = sq + K_STEPS[i]
      if (!(u & 0x88) && b[u] === k) return true
    }
    for (let i = 0; i < 4; i++) {
      const d = DIAG[i]
      let t = sq + d
      while (!(t & 0x88)) { const p = b[t]; if (p) { if (p === bb || p === qq) return true; break } t += d }
      const e = ORTH[i]
      t = sq + e
      while (!(t & 0x88)) { const p = b[t]; if (p) { if (p === rr || p === qq) return true; break } t += e }
    }
    return false
  }

  inCheck() { return this.attacked(this.kings[this.side], this.side ^ 1) }

  // Pseudo-legal moves appended to out from index n; returns the new length. capsOnly: captures + queen promotions.
  gen(out, n, capsOnly = false) {
    const b = this.b, us = this.side, sgn = us ? -1 : 1, them = us ^ 1
    for (let i = 0; i < 64; i++) {
      const sq = SQUARES[i], p = b[sq] * sgn
      if (p <= 0) continue
      if (p === PAWN) {
        const fwd = sq + 16 * sgn, promoRank = us ? 0 : 7, last = (fwd >> 4) === promoRank
        if (b[fwd] === 0) {
          if (last) {
            out[n++] = mk(sq, fwd, QUEEN)
            if (!capsOnly) { out[n++] = mk(sq, fwd, ROOK); out[n++] = mk(sq, fwd, BISHOP); out[n++] = mk(sq, fwd, KNIGHT) }
          } else if (!capsOnly) {
            out[n++] = mk(sq, fwd)
            if ((sq >> 4) === (us ? 6 : 1) && b[fwd + 16 * sgn] === 0) out[n++] = mk(sq, fwd + 16 * sgn, 0, F_DOUBLE)
          }
        }
        for (let k = 0; k < 2; k++) {
          const t = fwd + (k ? 1 : -1)
          if (t & 0x88) continue
          if (b[t] * sgn < 0) {
            if (last) {
              out[n++] = mk(sq, t, QUEEN)
              if (!capsOnly) { out[n++] = mk(sq, t, ROOK); out[n++] = mk(sq, t, BISHOP); out[n++] = mk(sq, t, KNIGHT) }
            } else out[n++] = mk(sq, t)
          } else if (t === this.ep) out[n++] = mk(sq, t, 0, F_EP)
        }
      } else if (p === KNIGHT || p === KING) {
        const steps = p === KNIGHT ? N_STEPS : K_STEPS
        for (let k = 0; k < 8; k++) {
          const t = sq + steps[k]
          if (t & 0x88) continue
          const q = b[t] * sgn
          if (q < 0 || (q === 0 && !capsOnly)) out[n++] = mk(sq, t)
        }
        if (p === KING && !capsOnly && this.castle) {
          const base = us ? 112 : 0
          if (sq === base + 4) {
            if ((this.castle & (us ? 4 : 1)) && !b[base + 5] && !b[base + 6] &&
              !this.attacked(base + 4, them) && !this.attacked(base + 5, them) && !this.attacked(base + 6, them)) out[n++] = mk(sq, base + 6, 0, F_CASTLE)
            if ((this.castle & (us ? 8 : 2)) && !b[base + 3] && !b[base + 2] && !b[base + 1] &&
              !this.attacked(base + 4, them) && !this.attacked(base + 3, them) && !this.attacked(base + 2, them)) out[n++] = mk(sq, base + 2, 0, F_CASTLE)
          }
        }
      } else {
        const dirs = p === BISHOP ? DIAG : p === ROOK ? ORTH : K_STEPS
        for (let k = 0; k < dirs.length; k++) {
          const d = dirs[k]
          let t = sq + d
          while (!(t & 0x88)) {
            const q = b[t] * sgn
            if (q > 0) break
            if (q < 0) { out[n++] = mk(sq, t); break }
            if (!capsOnly) out[n++] = mk(sq, t)
            t += d
          }
        }
      }
    }
    return n
  }

  // Makes the move; returns false (and leaves the position unchanged) if it leaves the mover's king in check.
  make(m) {
    const from = m & 127, to = (m >> 7) & 127, promo = (m >> 14) & 7, flag = m >> 17
    const b = this.b, us = this.side, sgn = us ? -1 : 1, ply = this.ply
    const p = b[from]
    this.uMove[ply] = m; this.uCastle[ply] = this.castle; this.uEp[ply] = this.ep; this.uHalf[ply] = this.half
    let cap = 0
    if (flag === F_EP) { cap = b[to - 16 * sgn]; this.remove(to - 16 * sgn) } else if (b[to]) { cap = b[to]; this.remove(to) }
    this.uCap[ply] = cap
    this.remove(from)
    this.put(to, promo ? promo * sgn : p)
    if (flag === F_CASTLE) {
      const rf = (to & 7) === 6 ? to + 1 : to - 2, rt = (to & 7) === 6 ? to - 1 : to + 1
      this.remove(rf); this.put(rt, ROOK * sgn)
    }
    if (p * sgn === KING) this.kings[us] = to
    const cr = this.castle & CR_MASK[from] & CR_MASK[to]
    if (cr !== this.castle) { this.lo ^= Z.cLo[this.castle] ^ Z.cLo[cr]; this.hi ^= Z.cHi[this.castle] ^ Z.cHi[cr]; this.castle = cr }
    if (this.ep >= 0) { this.lo ^= Z.eLo[this.ep & 7]; this.hi ^= Z.eHi[this.ep & 7] }
    this.ep = flag === F_DOUBLE ? this.epIfCapturable(from + 16 * sgn, to) : -1
    if (this.ep >= 0) { this.lo ^= Z.eLo[this.ep & 7]; this.hi ^= Z.eHi[this.ep & 7] }
    this.half = cap || p * sgn === PAWN ? 0 : this.half + 1
    if (us) this.full++
    this.side = us ^ 1
    this.lo ^= Z.sLo; this.hi ^= Z.sHi
    this.ply = ply + 1
    this.hLo[ply + 1] = this.lo; this.hHi[ply + 1] = this.hi
    if (this.attacked(this.kings[us], us ^ 1)) { this.unmake(); return false }
    return true
  }

  unmake() {
    const ply = --this.ply
    const m = this.uMove[ply], from = m & 127, to = (m >> 7) & 127, promo = (m >> 14) & 7, flag = m >> 17
    const us = this.side ^ 1, sgn = us ? -1 : 1
    this.side = us
    const p = promo ? PAWN * sgn : this.b[to]
    this.remove(to)
    this.put(from, p)
    const cap = this.uCap[ply]
    if (flag === F_EP) this.put(to - 16 * sgn, cap)
    else if (cap) this.put(to, cap)
    if (flag === F_CASTLE) {
      const rf = (to & 7) === 6 ? to + 1 : to - 2, rt = (to & 7) === 6 ? to - 1 : to + 1
      this.remove(rt); this.put(rf, ROOK * sgn)
    }
    if (p * sgn === KING) this.kings[us] = from
    this.castle = this.uCastle[ply]; this.ep = this.uEp[ply]; this.half = this.uHalf[ply]
    if (us) this.full--
    this.lo = this.hLo[ply]; this.hi = this.hHi[ply]
  }

  makeNull() {
    const ply = this.ply
    this.uMove[ply] = 0; this.uEp[ply] = this.ep; this.uHalf[ply] = this.half
    if (this.ep >= 0) { this.lo ^= Z.eLo[this.ep & 7]; this.hi ^= Z.eHi[this.ep & 7] }
    this.ep = -1
    this.half = 0
    this.side ^= 1
    this.lo ^= Z.sLo; this.hi ^= Z.sHi
    this.ply = ply + 1
    this.hLo[ply + 1] = this.lo; this.hHi[ply + 1] = this.hi
  }

  unmakeNull() {
    const ply = --this.ply
    this.side ^= 1
    this.ep = this.uEp[ply]; this.half = this.uHalf[ply]
    this.lo = this.hLo[ply]; this.hi = this.hHi[ply]
  }

  legalMoves() {
    const n = this.gen(this.buf, 0), out = []
    for (let i = 0; i < n; i++) { const m = this.buf[i]; if (this.make(m)) { this.unmake(); out.push(m) } }
    return out
  }

  repetitions() {
    let n = 1
    for (let i = this.ply - 2; i >= 0 && i >= this.ply - this.half; i -= 2) if (this.hLo[i] === this.lo && this.hHi[i] === this.hi) n++
    return n
  }

  insufficient() {
    let minors = 0, knights = 0, bishopColors = 0
    for (const s of SQUARES) {
      const t = Math.abs(this.b[s])
      if (t === PAWN || t === ROOK || t === QUEEN) return false
      if (t === KNIGHT) { minors++; knights++ }
      if (t === BISHOP) { minors++; bishopColors |= 1 << (((s >> 4) + (s & 7)) & 1) }
    }
    return minors <= 1 || (!knights && bishopColors !== 3)
  }

  // Static evaluation from the side to move's point of view (centipawns).
  evaluate() {
    const b = this.b
    const ph = Math.min(24, this.phase)
    let score = ((this.mg[0] - this.mg[1]) * ph + (this.eg[0] - this.eg[1]) * (24 - ph)) / 24
    const wMin = [9, 9, 9, 9, 9, 9, 9, 9, 9, 9], bMax = [-1, -1, -1, -1, -1, -1, -1, -1, -1, -1]
    const wCnt = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0], bCnt = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    let wb = 0, bbish = 0, wPawns = 0, bPawns = 0
    const npm = [0, 0]
    for (let i = 0; i < 64; i++) {
      const s = SQUARES[i], p = b[s]
      if (p === PAWN) { const f = (s & 7) + 1, r = s >> 4; wCnt[f]++; wPawns++; if (r < wMin[f]) wMin[f] = r }
      else if (p === -PAWN) { const f = (s & 7) + 1, r = s >> 4; bCnt[f]++; bPawns++; if (r > bMax[f]) bMax[f] = r }
      else if (p) {
        if (p === BISHOP) wb++
        else if (p === -BISHOP) bbish++
        if (p > 0) npm[0] += VALUE[p]; else npm[1] += VALUE[-p]
      }
    }
    let pm = 0, pe = 0
    for (let i = 0; i < 64; i++) {
      const s = SQUARES[i], p = b[s]
      if (p !== PAWN && p !== -PAWN) continue
      const f = (s & 7) + 1, r = s >> 4
      if (p === PAWN) {
        if (bMax[f - 1] <= r && bMax[f] <= r && bMax[f + 1] <= r) { pm += PASSED_MG[r]; pe += PASSED_EG[r] }
        if (!wCnt[f - 1] && !wCnt[f + 1]) { pm -= 10; pe -= 12 }
      } else {
        const rr = 7 - r
        if (wMin[f - 1] >= r && wMin[f] >= r && wMin[f + 1] >= r) { pm -= PASSED_MG[rr]; pe -= PASSED_EG[rr] }
        if (!bCnt[f - 1] && !bCnt[f + 1]) { pm += 10; pe += 12 }
      }
    }
    for (let f = 1; f <= 8; f++) {
      if (wCnt[f] > 1) { pm -= 10 * (wCnt[f] - 1); pe -= 20 * (wCnt[f] - 1) }
      if (bCnt[f] > 1) { pm += 10 * (bCnt[f] - 1); pe += 20 * (bCnt[f] - 1) }
    }
    score += (pm * ph + pe * (24 - ph)) / 24
    if (wb >= 2) score += 30
    if (bbish >= 2) score -= 30
    const lead = score > 0 ? 0 : 1, leadPawns = lead ? bPawns : wPawns, loserPawns = lead ? wPawns : bPawns
    if (!leadPawns && npm[lead] <= 340) score /= 8
    else if (Math.abs(score) > 300 && !loserPawns && ph <= 10) {
      const lk = this.kings[lead ^ 1], wk = this.kings[lead]
      const cmd = Math.max(3 - (lk & 7), (lk & 7) - 4) + Math.max(3 - (lk >> 4), (lk >> 4) - 4)
      const dist = Math.abs((lk & 7) - (wk & 7)) + Math.abs((lk >> 4) - (wk >> 4))
      const bonus = cmd * 12 + (14 - dist) * 5
      score += lead ? -bonus : bonus
    }
    return (this.side ? -score : score) + 10
  }
}

const PIECE_LETTER = ' PNBRQK'

// SAN for a legal move m (legal = list of legal moves in this position).
export function san(pos, m, legal = pos.legalMoves()) {
  const from = mFrom(m), to = mTo(m), promo = mPromo(m), flag = mFlag(m), t = Math.abs(pos.b[from])
  let s
  if (flag === F_CASTLE) s = (to & 7) === 6 ? 'O-O' : 'O-O-O'
  else {
    const cap = pos.b[to] !== 0 || flag === F_EP
    if (t === PAWN) {
      s = (cap ? 'abcdefgh'[from & 7] + 'x' : '') + sqName(to)
      if (promo) s += '=' + PIECE_LETTER[promo]
    } else {
      s = PIECE_LETTER[t]
      const others = legal.filter((x) => x !== m && mTo(x) === to && Math.abs(pos.b[mFrom(x)]) === t)
      if (others.length) {
        const sameFile = others.some((x) => (mFrom(x) & 7) === (from & 7)), sameRank = others.some((x) => (mFrom(x) >> 4) === (from >> 4))
        if (!sameFile) s += 'abcdefgh'[from & 7]
        else if (!sameRank) s += (from >> 4) + 1
        else s += sqName(from)
      }
      s += (cap ? 'x' : '') + sqName(to)
    }
  }
  if (pos.make(m)) {
    if (pos.inCheck()) s += pos.legalMoves().length ? '+' : '#'
    pos.unmake()
  }
  return s
}

// Finds the legal move matching a SAN (tolerant: optional x, =, +, #, annotations, 0-0) or UCI token.
export function parseMove(pos, token, legal = pos.legalMoves()) {
  let tk = String(token).trim().replace(/[!?+#]+$/g, '').replace(/^0-0-0$/, 'O-O-O').replace(/^0-0$/, 'O-O')
  if (/^[a-h][1-8][a-h][1-8][qrbnQRBN]?$/.test(tk)) {
    const u = tk.toLowerCase()
    const m = legal.find((x) => toUci(x) === u)
    if (m != null) return m
  }
  if (tk === 'O-O' || tk === 'O-O-O') {
    return legal.find((x) => mFlag(x) === F_CASTLE && ((mTo(x) & 7) === 6) === (tk === 'O-O')) ?? null
  }
  const mm = /^([NBRQK])?([a-h])?([1-8])?[x:]?([a-h][1-8])(?:=?([NBRQnbrq]))?$/.exec(tk)
  if (!mm) return null
  const t = mm[1] ? PIECE_LETTER.indexOf(mm[1]) : PAWN
  const to = parseSq(mm[4])
  const promo = mm[5] ? PIECE_LETTER.indexOf(mm[5].toUpperCase()) : 0
  const c = legal.filter((x) => mTo(x) === to && Math.abs(pos.b[mFrom(x)]) === t && mPromo(x) === promo &&
    (!mm[2] || 'abcdefgh'[mFrom(x) & 7] === mm[2]) && (!mm[3] || String((mFrom(x) >> 4) + 1) === mm[3]))
  return c.length === 1 ? c[0] : null
}

export function perft(pos, depth) {
  if (depth === 0) return 1
  const buf = new Int32Array(256)
  const n = pos.gen(buf, 0)
  let total = 0
  for (let i = 0; i < n; i++) {
    if (!pos.make(buf[i])) continue
    total += depth === 1 ? 1 : perft(pos, depth - 1)
    pos.unmake()
  }
  return total
}

// High-level game record used by the UI: start FEN, UCI history, SAN history, captured pieces, result.
export class Game {
  constructor(startFen = null) {
    this.startFen = startFen || START_FEN
    this.custom = !!startFen && startFen !== START_FEN
    this.pos = Position.fromFen(this.startFen)
    this.startSide = this.pos.side
    this.startFull = this.pos.full
    this.ucis = []
    this.sans = []
    this.captured = [[], []]
    this._legal = null
    this._legalU = null
  }

  get side() { return this.pos.side }
  legal() {
    if (!this._legal) { this._legal = this.pos.legalMoves(); this._legalU = this._legal.map(toUci) }
    return this._legalU
  }
  legalInts() { this.legal(); return this._legal }

  play(uci) {
    const legalU = this.legal()
    const i = legalU.indexOf(uci)
    if (i < 0) throw new Error(`Coup illégal : ${uci}`)
    const m = this._legal[i], pos = this.pos
    const from = mFrom(m), to = mTo(m), flag = mFlag(m), us = pos.side, sgn = us ? -1 : 1
    const piece = pos.b[from]
    const capSq = flag === F_EP ? to - 16 * sgn : pos.b[to] ? to : -1
    const captured = capSq >= 0 ? pos.b[capSq] : 0
    const s = san(pos, m, this._legal)
    pos.make(m)
    this._legal = null
    this.ucis.push(uci)
    this.sans.push(s)
    if (captured) this.captured[us].push(Math.abs(captured))
    const anim = { from, to, piece, captured, capSq, promo: mPromo(m), san: s, check: pos.inCheck() }
    if (flag === F_CASTLE) anim.rook = (to & 7) === 6 ? [to + 1, to - 1] : [to - 2, to + 1]
    return anim
  }

  result() {
    const pos = this.pos
    if (!this.legalInts().length) {
      return pos.inCheck() ? { winner: pos.side ^ 1, reason: 'mate' } : { winner: -1, reason: 'stalemate' }
    }
    if (pos.half >= 100) return { winner: -1, reason: 'fifty' }
    if (pos.repetitions() >= 3) return { winner: -1, reason: 'repetition' }
    if (pos.insufficient()) return { winner: -1, reason: 'material' }
    return null
  }

  material() {
    let w = 0, bl = 0
    for (const s of SQUARES) { const p = this.pos.b[s]; if (p > 0) w += [0, 1, 3, 3, 5, 9, 0][p]; else if (p < 0) bl += [0, 1, 3, 3, 5, 9, 0][-p] }
    return w - bl
  }

  static replay(startFen, ucis) {
    const g = new Game(startFen)
    for (const u of ucis) g.play(u)
    return g
  }

  pgn(headers = {}) {
    const res = this.result()
    const r = !res ? '*' : res.winner === 0 ? '1-0' : res.winner === 1 ? '0-1' : '1/2-1/2'
    const h = { Event: 'Partie amicale', Site: 'LedroideDesktop', Date: '????.??.??', Round: '-', White: '?', Black: '?', ...headers, Result: r }
    if (this.custom) { h.SetUp = '1'; h.FEN = this.startFen }
    const tags = Object.entries(h).map(([k, v]) => `[${k} "${String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"]`).join('\n')
    const parts = []
    let n = this.startFull, side = this.startSide
    this.sans.forEach((s, i) => {
      if (side === 0) parts.push(`${n}.`)
      else if (i === 0) parts.push(`${n}...`)
      parts.push(s)
      if (side === 1) n++
      side ^= 1
    })
    parts.push(r)
    const lines = []
    let line = ''
    for (const p of parts) {
      if (line && line.length + 1 + p.length > 80) { lines.push(line); line = p } else line = line ? `${line} ${p}` : p
    }
    if (line) lines.push(line)
    return `${tags}\n\n${lines.join('\n')}\n`
  }
}

// Parses PGN (first game) → { start: FEN|null, ucis, headers }. Throws an Error with a French message.
export function parsePgn(text) {
  const src = String(text || '')
  const headers = {}
  for (const m of src.matchAll(/\[\s*(\w+)\s+"((?:[^"\\]|\\.)*)"\s*\]/g)) headers[m[1]] = m[2].replace(/\\(.)/g, '$1')
  let body = src.replace(/\[\s*\w+\s+"(?:[^"\\]|\\.)*"\s*\]/g, ' ').replace(/\{[^}]*\}/g, ' ').replace(/;[^\n]*/g, ' ')
  let prev
  do { prev = body; body = body.replace(/\([^()]*\)/g, ' ') } while (body !== prev)
  body = body.replace(/\$\d+/g, ' ').replace(/\d+\s*\.(\s*\.\.)?/g, ' ').replace(/\.\.+/g, ' ')
  const start = headers.FEN ? headers.FEN : null
  const g = new Game(start)
  for (const tok of body.split(/\s+/).filter(Boolean)) {
    if (/^(1-0|0-1|1\/2-1\/2|½-½|\*)$/.test(tok)) break
    const m = parseMove(g.pos, tok, g.legalInts())
    if (m == null) throw new Error(`PGN : coup « ${tok} » illégal ou ambigu (coup n° ${g.ucis.length + 1}).`)
    g.play(toUci(m))
  }
  return { start: g.custom ? g.startFen : null, ucis: g.ucis, headers }
}
