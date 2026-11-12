// Sliding puzzle logic: solvable shuffles, IDA* (Manhattan + linear conflict), weighted A*, greedy fallback.
// Board = array of n*n values, tile t (1..n²-1) belongs at index t-1, 0 = blank (belongs at the end).

export const isSolved = (tiles) => tiles.every((v, i) => v === (i === tiles.length - 1 ? 0 : i + 1))

export function isSolvable(tiles, n) {
  let inv = 0
  const a = tiles.filter((v) => v)
  for (let i = 0; i < a.length; i++) for (let j = i + 1; j < a.length; j++) if (a[i] > a[j]) inv++
  if (n % 2) return inv % 2 === 0
  const blankRow = Math.floor(tiles.indexOf(0) / n)
  return (inv + blankRow) % 2 === (n - 1) % 2
}

export function shuffleSolvable(n, rnd = Math.random) {
  const N = n * n
  for (;;) {
    const t = [...Array(N)].map((_, i) => (i + 1) % N)
    for (let i = N - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [t[i], t[j]] = [t[j], t[i]] }
    if (!isSolvable(t, n)) {
      const [a, b] = t.map((v, i) => (v ? i : -1)).filter((i) => i >= 0)
      ;[t[a], t[b]] = [t[b], t[a]]
    }
    if (!isSolved(t) && manhattan(t, n) >= N) return t
  }
}

export function manhattan(tiles, n) {
  let s = 0
  for (let i = 0; i < tiles.length; i++) {
    const v = tiles[i]
    if (v) s += Math.abs(Math.floor(i / n) - Math.floor((v - 1) / n)) + Math.abs((i % n) - ((v - 1) % n))
  }
  return s
}

// 2 × (tiles of the line whose goal is in the line − longest increasing run of their goal order).
function lineConflict(tiles, n, line, isRow) {
  const seq = []
  for (let k = 0; k < n; k++) {
    const v = tiles[isRow ? line * n + k : k * n + line]
    if (!v) continue
    const g = v - 1
    if (isRow ? Math.floor(g / n) === line : g % n === line) seq.push(isRow ? g % n : Math.floor(g / n))
  }
  if (seq.length < 2) return 0
  const lis = new Array(seq.length).fill(1)
  let best = 1
  for (let i = 1; i < seq.length; i++) {
    for (let j = 0; j < i; j++) if (seq[j] < seq[i] && lis[j] + 1 > lis[i]) lis[i] = lis[j] + 1
    if (lis[i] > best) best = lis[i]
  }
  return 2 * (seq.length - best)
}

export function heuristic(tiles, n) {
  let lc = 0
  for (let k = 0; k < n; k++) lc += lineConflict(tiles, n, k, true) + lineConflict(tiles, n, k, false)
  return manhattan(tiles, n) + lc
}

function neighbours(n) {
  return [...Array(n * n)].map((_, i) => {
    const r = Math.floor(i / n), c = i % n, out = []
    if (r > 0) out.push(i - n)
    if (r < n - 1) out.push(i + n)
    if (c > 0) out.push(i - 1)
    if (c < n - 1) out.push(i + 1)
    return out
  })
}

class Timeout extends Error {}

// Optimal solution as the list of tile values to slide, or null when the deadline (performance.now()) passes.
export function idaStar(start, n, deadline = Infinity) {
  const t = Uint8Array.from(start)
  if (isSolved(t)) return []
  const NB = neighbours(n)
  const rowLc = new Int32Array(n), colLc = new Int32Array(n)
  let md = manhattan(t, n), lc = 0
  for (let k = 0; k < n; k++) { rowLc[k] = lineConflict(t, n, k, true); colLc[k] = lineConflict(t, n, k, false); lc += rowLc[k] + colLc[k] }
  let blank = t.indexOf(0)
  const path = []
  let nodes = 0, bound = md + lc, nextBound
  const dist = (pos, v) => Math.abs(Math.floor(pos / n) - Math.floor((v - 1) / n)) + Math.abs((pos % n) - ((v - 1) % n))

  const search = (g, prevBlank) => {
    const f = g + md + lc
    if (f > bound) { if (f < nextBound) nextBound = f; return false }
    if (md === 0) return true
    if ((++nodes & 0x3fff) === 0 && performance.now() > deadline) throw new Timeout()
    const b = blank
    for (const p of NB[b]) {
      if (p === prevBlank) continue
      const v = t[p]
      const dmd = dist(b, v) - dist(p, v)
      t[b] = v; t[p] = 0; blank = p
      md += dmd
      let a1, a2, old1, old2, isRow
      if ((p - b) % n === 0 && Math.abs(p - b) >= n) { isRow = true; a1 = Math.floor(p / n); a2 = Math.floor(b / n) } else { isRow = false; a1 = p % n; a2 = b % n }
      const arr = isRow ? rowLc : colLc
      old1 = arr[a1]; old2 = arr[a2]
      const n1 = lineConflict(t, n, a1, isRow), n2 = lineConflict(t, n, a2, isRow)
      arr[a1] = n1; arr[a2] = n2
      lc += n1 + n2 - old1 - old2
      path.push(v)
      const found = search(g + 1, b)
      if (found) return true
      path.pop()
      arr[a1] = old1; arr[a2] = old2
      lc -= n1 + n2 - old1 - old2
      md -= dmd
      t[p] = v; t[b] = 0; blank = b
    }
    return false
  }
  try {
    for (;;) {
      nextBound = Infinity
      if (search(0, -1)) return path.slice()
      if (nextBound === Infinity) return null
      bound = nextBound
    }
  } catch (e) {
    if (e instanceof Timeout) return null
    throw e
  }
}

class Heap {
  constructor() { this.a = [] }
  get size() { return this.a.length }
  push(x) {
    const a = this.a
    a.push(x)
    let i = a.length - 1
    while (i > 0) { const p = (i - 1) >> 1; if (a[p].f <= a[i].f) break; [a[p], a[i]] = [a[i], a[p]]; i = p }
  }
  pop() {
    const a = this.a, top = a[0], last = a.pop()
    if (a.length) {
      a[0] = last
      let i = 0
      for (;;) {
        const l = 2 * i + 1, r = l + 1
        let m = i
        if (l < a.length && a[l].f < a[m].f) m = l
        if (r < a.length && a[r].f < a[m].f) m = r
        if (m === i) break
        ;[a[m], a[i]] = [a[i], a[m]]; i = m
      }
    }
    return top
  }
}

// Weighted A* (f = g + w·h): fast, non-optimal path. null if the deadline or node budget is exceeded.
export function weightedAStar(start, n, w = 3, deadline = Infinity, maxNodes = 300000) {
  if (isSolved(start)) return []
  const NB = neighbours(n)
  const key = (t) => String.fromCharCode(...t)
  const startKey = key(start)
  const parent = new Map([[startKey, null]])
  const heap = new Heap()
  heap.push({ f: w * heuristic(start, n), g: 0, t: Uint8Array.from(start), k: startKey, b: start.indexOf(0) })
  let nodes = 0
  while (heap.size) {
    const cur = heap.pop()
    if ((++nodes & 0x3ff) === 0 && performance.now() > deadline) return null
    if (nodes > maxNodes) return null
    for (const p of NB[cur.b]) {
      const t = cur.t.slice()
      const v = t[p]
      t[cur.b] = v; t[p] = 0
      const k = key(t)
      if (parent.has(k)) continue
      parent.set(k, [cur.k, v])
      const hv = heuristic(t, n)
      if (hv === 0) {
        const path = []
        for (let x = k; parent.get(x); x = parent.get(x)[0]) path.push(parent.get(x)[1])
        return path.reverse()
      }
      heap.push({ f: cur.g + 1 + w * hv, g: cur.g + 1, t, k, b: p })
    }
  }
  return null
}

// Short lookahead on the heuristic; used when nothing better finished in time.
export function greedyMove(start, n, avoid = 0) {
  const NB = neighbours(n)
  const t = Uint8Array.from(start)
  const look = (depth, blank, prev) => {
    if (depth === 0) return heuristic(t, n)
    let best = Infinity
    for (const p of NB[blank]) {
      if (p === prev) continue
      const v = t[p]
      t[blank] = v; t[p] = 0
      best = Math.min(best, isSolved(t) ? -1 : look(depth - 1, p, blank))
      t[p] = v; t[blank] = 0
    }
    return best
  }
  const blank = t.indexOf(0)
  let bestTile = 0, bestScore = Infinity
  for (const p of NB[blank]) {
    const v = t[p]
    t[blank] = v; t[p] = 0
    const s = (isSolved(t) ? -100 : look(n >= 5 ? 3 : 4, p, blank)) + (v === avoid ? 2 : 0) + Math.random() * 0.5
    t[p] = v; t[blank] = 0
    if (s < bestScore) { bestScore = s; bestTile = v }
  }
  return bestTile
}

export function canSlide(tiles, n, v) {
  const i = tiles.indexOf(v), b = tiles.indexOf(0)
  return Math.abs(i - b) === n || (Math.abs(i - b) === 1 && Math.floor(i / n) === Math.floor(b / n))
}
