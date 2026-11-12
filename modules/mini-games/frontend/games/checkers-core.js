// Draughts rules shared by the UI, the AI worker and the tests.
// Board: playable squares numbered 0..N-1 (official number - 1), row 0 at the top (black side).
// Pieces: 1 white man, 2 white king, -1 black man, -2 black king. Side to move p: 0 = white, 1 = black.

export const VARIANTS = [['10', 'Internationales 10×10'], ['8', '8×8']]
export const QUIET_LIMIT = 50

const GEOM = {}
export function geom(n) {
  if (GEOM[n]) return GEOM[n]
  const half = n / 2, N = n * half
  const row = new Int8Array(N), col = new Int8Array(N)
  const nb = new Int16Array(N * 4).fill(-1)
  const rays = []
  const DR = [-1, -1, 1, 1], DC = [-1, 1, -1, 1]
  const at = (r, c) => (r < 0 || r >= n || c < 0 || c >= n || ((r + c) & 1) === 0 ? -1 : r * half + (c >> 1))
  for (let i = 0; i < N; i++) {
    const r = (i / half) | 0
    row[i] = r
    col[i] = 2 * (i % half) + (r % 2 === 0 ? 1 : 0)
  }
  for (let i = 0; i < N; i++) {
    for (let d = 0; d < 4; d++) {
      nb[i * 4 + d] = at(row[i] + DR[d], col[i] + DC[d])
      const ray = []
      for (let k = 1; ; k++) {
        const s = at(row[i] + DR[d] * k, col[i] + DC[d] * k)
        if (s < 0) break
        ray.push(s)
      }
      rays.push(ray)
    }
  }
  return (GEOM[n] = { n, half, N, row, col, nb, rays, at })
}

export function initialBoard(g) {
  const b = new Int8Array(g.N)
  const rows = g.n / 2 - 1
  for (let i = 0; i < g.N; i++) {
    if (g.row[i] < rows) b[i] = -1
    else if (g.row[i] >= g.n - rows) b[i] = 1
  }
  return b
}

// Legal moves of side p: [{ from, to, path, caps }]. Captures are compulsory and the sequence taking the most
// pieces must be played; jumped pieces stay on the board until the end of the sequence (they block and cannot
// be jumped twice). Sequences with the same start, end and captured set are the same move.
export function genMoves(g, b, p) {
  const sgn = p ? -1 : 1
  const out = []
  let best = 0
  const capped = new Uint8Array(g.N)
  const path = [], caps = []
  const { nb, rays } = g
  const dfs = (s, king) => {
    let ext = false
    for (let d = 0; d < 4; d++) {
      if (king) {
        const ray = rays[s * 4 + d]
        let k = 0
        while (k < ray.length && b[ray[k]] === 0) k++
        if (k >= ray.length) continue
        const t = ray[k]
        if (b[t] * sgn >= 0 || capped[t]) continue
        k++
        if (k >= ray.length || b[ray[k]] !== 0) continue
        capped[t] = 1; caps.push(t)
        for (; k < ray.length && b[ray[k]] === 0; k++) {
          path.push(ray[k]); dfs(ray[k], true); path.pop()
          ext = true
        }
        caps.pop(); capped[t] = 0
      } else {
        const t = nb[s * 4 + d]
        if (t < 0 || b[t] * sgn >= 0 || capped[t]) continue
        const l = nb[t * 4 + d]
        if (l < 0 || b[l] !== 0) continue
        capped[t] = 1; caps.push(t); path.push(l)
        dfs(l, false)
        path.pop(); caps.pop(); capped[t] = 0
        ext = true
      }
    }
    if (!ext && caps.length && caps.length >= best) {
      if (caps.length > best) { best = caps.length; out.length = 0 }
      out.push({ from: path[0], to: s, path: path.slice(), caps: caps.slice() })
    }
  }
  for (let s = 0; s < g.N; s++) {
    const v = b[s]
    if (v * sgn <= 0) continue
    b[s] = 0
    path.length = 0; path.push(s)
    dfs(s, v === 2 || v === -2)
    b[s] = v
  }
  if (out.length) {
    if (best < 2) return out
    const seen = new Set()
    return out.filter((m) => {
      const k = `${m.from}:${m.to}:${[...m.caps].sort((x, y) => x - y).join(',')}`
      if (seen.has(k)) return false
      seen.add(k)
      return true
    })
  }
  for (let s = 0; s < g.N; s++) {
    const v = b[s]
    if (v * sgn <= 0) continue
    if (v === 1 || v === -1) {
      for (let d = p ? 2 : 0, e = d + 2; d < e; d++) {
        const t = nb[s * 4 + d]
        if (t >= 0 && b[t] === 0) out.push({ from: s, to: t, path: [s, t], caps: [] })
      }
    } else {
      for (let d = 0; d < 4; d++) {
        for (const t of rays[s * 4 + d]) {
          if (b[t] !== 0) break
          out.push({ from: s, to: t, path: [s, t], caps: [] })
        }
      }
    }
  }
  return out
}

export const promoRow = (g, p) => (p ? g.n - 1 : 0)

export const moveKey = (m) => (m.caps.length ? m.path.map((s) => s + 1).join('x') : `${m.from + 1}-${m.to + 1}`)

// Official short notation; the full path is given only when it is needed to tell two moves apart.
export function notation(m, legal) {
  if (!m.caps.length) return `${m.from + 1}-${m.to + 1}`
  const amb = legal && legal.some((o) => o !== m && o.from === m.from && o.to === m.to)
  return amb ? moveKey(m) : `${m.from + 1}x${m.to + 1}`
}

const posKey = (pos) => `${pos.p}${pos.b.join(',')}`

export function newPosition(variant) {
  const n = variant === '8' ? 8 : 10
  const g = geom(n)
  const pos = { n, g, b: initialBoard(g), p: 0, quiet: 0, reps: [], hist: [], agreed: false, offer: false, _legal: null }
  pos.reps.push(posKey(pos))
  return pos
}

export function legalMoves(pos) {
  if (!pos._legal) {
    const moves = genMoves(pos.g, pos.b, pos.p)
    pos._legal = { moves, keys: moves.map(moveKey) }
  }
  return pos._legal
}

export function findMove(pos, key) {
  const { moves, keys } = legalMoves(pos)
  const i = keys.indexOf(key)
  return i < 0 ? null : moves[i]
}

// Plays a legal move given by its key ('=' = draw agreed). Returns what the view needs to animate it.
export function applyKey(pos, key) {
  if (key === '=') {
    pos.agreed = true
    pos.hist.push('½-½')
    pos._legal = null
    return { draw: true }
  }
  const m = findMove(pos, key)
  if (!m) throw new Error(`coup invalide : ${key}`)
  const { g, b } = pos
  const piece = b[m.from]
  const capTypes = m.caps.map((s) => b[s])
  pos.hist.push(notation(m, pos._legal.moves))
  b[m.from] = 0
  for (const s of m.caps) b[s] = 0
  let promo = false
  if ((piece === 1 || piece === -1) && g.row[m.to] === promoRow(g, pos.p)) { b[m.to] = piece * 2; promo = true } else b[m.to] = piece
  const reversible = !m.caps.length && (piece === 2 || piece === -2)
  pos.quiet = reversible ? pos.quiet + 1 : 0
  if (!reversible) pos.reps = []
  pos.p ^= 1
  pos._legal = null
  pos.reps.push(posKey(pos))
  return { from: m.from, to: m.to, path: m.path, caps: m.caps, capTypes, piece, promo, side: pos.p ^ 1, n: pos.n }
}

export function replayKeys(variant, keys) {
  const pos = newPosition(variant)
  for (const k of keys) applyKey(pos, k)
  return pos
}

export function counts(pos) {
  const c = [{ men: 0, kings: 0 }, { men: 0, kings: 0 }]
  for (const v of pos.b) {
    if (v === 1) c[0].men++
    else if (v === 2) c[0].kings++
    else if (v === -1) c[1].men++
    else if (v === -2) c[1].kings++
  }
  return c
}

export function result(pos) {
  if (pos.agreed) return { winner: -1, line: [], reason: 'Nulle par accord mutuel.' }
  if (!legalMoves(pos).moves.length) {
    const c = counts(pos)[pos.p]
    const side = pos.p ? 'Les noirs' : 'Les blancs'
    return { winner: pos.p ^ 1, line: [], reason: c.men + c.kings ? `${side} n’ont plus aucun coup possible.` : `${side} n’ont plus de pièces.` }
  }
  if (pos.quiet >= QUIET_LIMIT) return { winner: -1, line: [], reason: 'Nulle : 25 coups de dames de chaque côté sans prise ni mouvement de pion.' }
  const k = pos.reps[pos.reps.length - 1]
  if (pos.reps.filter((x) => x === k).length >= 3) return { winner: -1, line: [], reason: 'Nulle par triple répétition de la position.' }
  return null
}
