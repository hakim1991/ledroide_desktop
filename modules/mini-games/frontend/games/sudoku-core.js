// Sudoku engine: bitmask solver (solution counting), logical grader, unique-solution generator.
// Pure module, shared by the game (fallback), the worker and node tests.

export const ROW = new Uint8Array(81)
export const COL = new Uint8Array(81)
export const BOX = new Uint8Array(81)
for (let i = 0; i < 81; i++) {
  ROW[i] = (i / 9) | 0
  COL[i] = i % 9
  BOX[i] = ((ROW[i] / 3) | 0) * 3 + ((COL[i] / 3) | 0)
}
export const UNITS = []
for (let k = 0; k < 9; k++) {
  UNITS.push([...Array(9)].map((_, j) => k * 9 + j))
  UNITS.push([...Array(9)].map((_, j) => j * 9 + k))
  UNITS.push([...Array(9)].map((_, j) => (((k / 3) | 0) * 3 + ((j / 3) | 0)) * 9 + (k % 3) * 3 + (j % 3)))
}
export const PEERS = [...Array(81)].map((_, i) => {
  const s = new Set()
  for (let j = 0; j < 81; j++) if (j !== i && (ROW[j] === ROW[i] || COL[j] === COL[i] || BOX[j] === BOX[i])) s.add(j)
  return [...s]
})
const POP = new Uint8Array(512)
for (let m = 1; m < 512; m++) POP[m] = POP[m >> 1] + (m & 1)
const BIT_DIGIT = new Uint8Array(257)
for (let d = 0; d < 9; d++) BIT_DIGIT[1 << d] = d + 1
const digitOf = (bit) => (bit === 256 ? 9 : BIT_DIGIT[bit])

// Returns { count (capped at limit), solution } — rnd randomises the search order (used to build full grids).
export function solve(grid, limit = 2, rnd = null) {
  const cells = Uint8Array.from(grid)
  const r = new Uint16Array(9), c = new Uint16Array(9), b = new Uint16Array(9)
  const empties = []
  for (let i = 0; i < 81; i++) {
    const v = cells[i]
    if (!v) { empties.push(i); continue }
    const bit = 1 << (v - 1)
    if ((r[ROW[i]] | c[COL[i]] | b[BOX[i]]) & bit) return { count: 0, solution: null }
    r[ROW[i]] |= bit; c[COL[i]] |= bit; b[BOX[i]] |= bit
  }
  let count = 0, solution = null
  const order = new Uint8Array(9)
  const rec = (k) => {
    if (k === empties.length) {
      count++
      if (!solution) solution = Array.from(cells)
      return count >= limit
    }
    let best = -1, bestMask = 0, bestCnt = 10
    for (let j = k; j < empties.length; j++) {
      const i = empties[j]
      const m = ~(r[ROW[i]] | c[COL[i]] | b[BOX[i]]) & 511
      const n = POP[m]
      if (n < bestCnt) {
        best = j; bestMask = m; bestCnt = n
        if (n <= 1) break
      }
    }
    if (bestCnt === 0) return false
    const tmp = empties[k]; empties[k] = empties[best]; empties[best] = tmp
    const i = empties[k], R = ROW[i], C = COL[i], B = BOX[i]
    let n = 0
    for (let m = bestMask; m; m &= m - 1) order[n++] = digitOf(m & -m)
    if (rnd) for (let x = n - 1; x > 0; x--) { const y = Math.floor(rnd() * (x + 1)); const t = order[x]; order[x] = order[y]; order[y] = t }
    const local = order.slice(0, n)
    for (const d of local) {
      const bit = 1 << (d - 1)
      r[R] |= bit; c[C] |= bit; b[B] |= bit; cells[i] = d
      if (rec(k + 1)) return true
      r[R] ^= bit; c[C] ^= bit; b[B] ^= bit
    }
    cells[i] = 0
    return false
  }
  rec(0)
  return { count, solution }
}

export const TECH = ['', 'Singletons', 'Candidats verrouillés', 'Paires / triplets', 'X-Wing', 'Au-delà (essais)']

// Human-style solver. Returns { solved, level } where level = hardest technique needed (TECH index);
// level 5 means the logic set got stuck.
export function grade(grid) {
  const val = Uint8Array.from(grid)
  const cand = new Uint16Array(81)
  for (let i = 0; i < 81; i++) if (!val[i]) {
    let m = 511
    for (const p of PEERS[i]) if (val[p]) m &= ~(1 << (val[p] - 1))
    cand[i] = m
  }
  let left = 0
  for (let i = 0; i < 81; i++) if (!val[i]) left++
  let level = 0
  const place = (i, d) => {
    val[i] = d; cand[i] = 0; left--
    const bit = 1 << (d - 1)
    for (const p of PEERS[i]) cand[p] &= ~bit
  }
  const singles = () => {
    let prog = false
    for (let i = 0; i < 81; i++) if (!val[i] && POP[cand[i]] === 1) { place(i, digitOf(cand[i])); prog = true }
    for (const u of UNITS) {
      for (let d = 0; d < 9; d++) {
        const bit = 1 << d
        let at = -1, n = 0, placed = false
        for (const i of u) {
          if (val[i] === d + 1) { placed = true; break }
          if (cand[i] & bit) { n++; at = i }
        }
        if (!placed && n === 1) { place(at, d + 1); prog = true }
      }
    }
    return prog
  }
  const locked = () => {
    let prog = false
    for (let d = 0; d < 9; d++) {
      const bit = 1 << d
      for (const u of UNITS) {
        const cs = u.filter((i) => cand[i] & bit)
        if (cs.length < 2 || cs.length > 3) continue
        const sameBox = cs.every((i) => BOX[i] === BOX[cs[0]])
        const sameRow = cs.every((i) => ROW[i] === ROW[cs[0]])
        const sameCol = cs.every((i) => COL[i] === COL[cs[0]])
        const targets = []
        if (sameBox) for (let j = 0; j < 81; j++) if (BOX[j] === BOX[cs[0]]) targets.push(j)
        if (sameRow) for (let j = 0; j < 9; j++) targets.push(ROW[cs[0]] * 9 + j)
        if (sameCol) for (let j = 0; j < 9; j++) targets.push(j * 9 + COL[cs[0]])
        for (const j of targets) if (!u.includes(j) && (cand[j] & bit)) { cand[j] &= ~bit; prog = true }
      }
    }
    return prog
  }
  const subsets = () => {
    let prog = false
    for (const u of UNITS) {
      const empty = u.filter((i) => !val[i])
      for (const size of [2, 3]) {
        if (empty.length <= size) continue
        const pick = (start, chosen, mask) => {
          if (chosen.length === size) {
            if (POP[mask] !== size) return
            for (const j of empty) if (!chosen.includes(j) && (cand[j] & mask)) { cand[j] &= ~mask; prog = true }
            return
          }
          for (let x = start; x < empty.length; x++) {
            const m = mask | cand[empty[x]]
            if (POP[m] <= size) pick(x + 1, [...chosen, empty[x]], m)
          }
        }
        pick(0, [], 0)
        const digits = []
        for (let d = 0; d < 9; d++) if (!u.some((i) => val[i] === d + 1)) digits.push(d)
        const where = (d) => empty.filter((i) => cand[i] & (1 << d))
        const pickD = (start, chosen, cells) => {
          if (chosen.length === size) {
            if (cells.size !== size) return
            const keep = chosen.reduce((m, d) => m | (1 << d), 0)
            for (const i of cells) if (cand[i] & ~keep) { cand[i] &= keep; prog = true }
            return
          }
          for (let x = start; x < digits.length; x++) {
            const s = new Set([...cells, ...where(digits[x])])
            if (s.size <= size) pickD(x + 1, [...chosen, digits[x]], s)
          }
        }
        pickD(0, [], new Set())
      }
    }
    return prog
  }
  const xwing = () => {
    let prog = false
    for (let d = 0; d < 9; d++) {
      const bit = 1 << d
      for (const byRow of [true, false]) {
        const lines = []
        for (let a = 0; a < 9; a++) {
          const pos = []
          for (let b = 0; b < 9; b++) if (cand[byRow ? a * 9 + b : b * 9 + a] & bit) pos.push(b)
          lines.push(pos)
        }
        for (let a1 = 0; a1 < 9; a1++) {
          if (lines[a1].length !== 2) continue
          for (let a2 = a1 + 1; a2 < 9; a2++) {
            if (lines[a2].length !== 2 || lines[a2][0] !== lines[a1][0] || lines[a2][1] !== lines[a1][1]) continue
            for (const b of lines[a1]) for (let a = 0; a < 9; a++) {
              if (a === a1 || a === a2) continue
              const i = byRow ? a * 9 + b : b * 9 + a
              if (cand[i] & bit) { cand[i] &= ~bit; prog = true }
            }
          }
        }
      }
    }
    return prog
  }
  const steps = [locked, subsets, xwing]
  while (left > 0) {
    for (let i = 0; i < 81; i++) if (!val[i] && !cand[i]) return { solved: false, level: 5 }
    if (singles()) { level = Math.max(level, 1); continue }
    let done = false
    for (let s = 0; s < steps.length; s++) {
      if (steps[s]()) { level = Math.max(level, s + 2); done = true; break }
    }
    if (!done) return { solved: false, level: 5 }
  }
  return { solved: true, level }
}

export const LEVELS = {
  facile: { target: 38, maxTech: 1, minTech: 0 },
  moyen: { target: 30, maxTech: 2, minTech: 0 },
  difficile: { target: 25, maxTech: 3, minTech: 2 },
  expert: { target: 17, maxTech: 5, minTech: 4 },
}

function carve(solution, cfg, rnd) {
  const puzzle = solution.slice()
  const pairs = []
  for (let i = 0; i <= 40; i++) pairs.push(i)
  for (let x = pairs.length - 1; x > 0; x--) { const y = Math.floor(rnd() * (x + 1)); [pairs[x], pairs[y]] = [pairs[y], pairs[x]] }
  let clues = 81
  for (const i of pairs) {
    const j = 80 - i
    const drop = i === j ? 1 : 2
    if (clues - drop < cfg.target) continue
    const a = puzzle[i], b = puzzle[j]
    puzzle[i] = 0; puzzle[j] = 0
    let ok = solve(puzzle, 2).count === 1
    if (ok && cfg.maxTech < 5) ok = grade(puzzle).level <= cfg.maxTech
    if (ok) clues -= drop
    else { puzzle[i] = a; puzzle[j] = b }
  }
  return { puzzle, clues }
}

// Builds a puzzle with a unique solution. Retries until the difficulty constraints are met or the
// time budget is spent, keeping the best candidate (hardest grade, then fewest clues).
export function generate(level = 'facile', rnd = Math.random, budgetMs = 800) {
  const cfg = LEVELS[level] || LEVELS.facile
  const t0 = Date.now()
  let best = null
  for (let attempt = 0; ; attempt++) {
    const solution = solve(new Array(81).fill(0), 1, rnd).solution
    const { puzzle, clues } = carve(solution, cfg, rnd)
    const g = grade(puzzle)
    const cand = { puzzle, solution, clues, level: g.level, attempts: attempt + 1 }
    if (!best || g.level > best.level || (g.level === best.level && clues < best.clues)) best = cand
    if (g.level >= cfg.minTech) return cand
    if (Date.now() - t0 > budgetMs) return best
  }
}
