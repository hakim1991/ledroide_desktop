import { idaStar, weightedAStar, greedyMove } from './slider-solver.js'

self.onmessage = ({ data }) => {
  const { id, tiles, n, budget = 3000, avoid = 0 } = data
  const t0 = performance.now()
  let res = null
  if (n <= 4) {
    const path = idaStar(tiles, n, t0 + (n <= 3 ? budget : budget * 0.8))
    if (path) res = { kind: 'optimal', path }
  }
  if (!res) {
    const left = Math.max(400, t0 + budget - performance.now())
    const path = weightedAStar(tiles, n, n <= 4 ? 2 : 3, performance.now() + left, 1500000)
    if (path) res = { kind: 'approx', path }
  }
  if (!res) res = { kind: 'greedy', path: [greedyMove(tiles, n, avoid)] }
  self.postMessage({ id, ...res, ms: Math.round(performance.now() - t0) })
}
