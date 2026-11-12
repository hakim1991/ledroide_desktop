import { shuffleSolvable, isSolved, idaStar, greedyMove, canSlide } from './slider-solver.js'

const SIZES = [[3, '3×3'], [4, '4×4'], [5, '5×5']]
const HINT_BUDGET = 3000

const CSS = `
.g-slider { flex: 1; display: flex; align-items: center; justify-content: center; gap: 22px; padding: 12px; min-height: 0; }
.g-slider.col { flex-direction: column; gap: 12px; }
.g-slider .board { position: relative; flex-shrink: 0; background: #101014; border-radius: 10px; box-shadow: 0 0 0 2px #2e2e36, 0 10px 34px rgba(0,0,0,.45); touch-action: none; }
.g-slider .tile { position: absolute; left: 0; top: 0; transition: transform .11s ease-out; cursor: pointer; }
.g-slider .tile.nomove { transition: none; }
.g-slider .face { position: absolute; inset: 3px; border-radius: 7px; display: grid; place-items: center; font-weight: 700; color: #fff;
  text-shadow: 0 1px 2px rgba(0,0,0,.45); box-shadow: inset 0 -3px 0 rgba(0,0,0,.25), inset 0 1px 0 rgba(255,255,255,.18); background-repeat: no-repeat; }
.g-slider .tile:hover .face { filter: brightness(1.12); }
.g-slider .tile.ok .face::after { content: ''; position: absolute; left: 50%; bottom: 7%; width: 18%; height: 4px; margin-left: -9%; border-radius: 2px; background: rgba(255,255,255,.55); }
.g-slider.img .tile.ok .face::after { display: none; }
.g-slider.img .face { border-radius: 4px; box-shadow: inset 0 0 0 1px rgba(255,255,255,.15), inset 0 -2px 0 rgba(0,0,0,.25); }
.g-slider .face .num { font-size: 11px; position: absolute; top: 3px; left: 5px; opacity: .85; background: rgba(0,0,0,.35); border-radius: 4px; padding: 0 4px; }
.g-slider .tile.hint { z-index: 2; }
.g-slider .tile.hint .face { animation: g-slider-hint 1s ease-in-out infinite; }
.g-slider .tile.hint .face::before { content: attr(data-arrow); position: absolute; right: 6%; bottom: 6%; font-size: max(12px, 45%); line-height: 1; padding: 3px 6px; border-radius: 6px; background: rgba(0,0,0,.6); color: #fde68a; text-shadow: none; }
.g-slider .tile.last { opacity: 0; transition: opacity .5s; pointer-events: none; }
.g-slider .board.solved .tile.last { opacity: 1; }
.g-slider .board.solved .tile .face { box-shadow: none; }
.g-slider .board.solved.img .tile .face { border-radius: 0; inset: -1px; }
.g-slider .board.veiled .tile { visibility: hidden; }
.g-slider .side { width: 180px; display: flex; flex-direction: column; gap: 10px; flex-shrink: 0; }
.g-slider.col .side { width: auto; flex-direction: row; align-items: center; flex-wrap: wrap; justify-content: center; }
.g-slider .preview { width: 180px; height: 180px; border-radius: 8px; background-size: cover; box-shadow: 0 0 0 1px var(--border); }
.g-slider.col .preview { width: 72px; height: 72px; }
.g-slider .mini { display: grid; gap: 2px; width: 120px; }
.g-slider.col .mini { width: 72px; }
.g-slider .mini span { aspect-ratio: 1; display: grid; place-items: center; font-size: 10px; border-radius: 3px; color: #fff; }
.g-slider .info { color: var(--muted); font-size: 12px; line-height: 1.45; min-height: 3em; }
.g-slider.col .info { max-width: 260px; min-height: 0; }
.g-slider .info b { color: var(--text); }
.g-slider .help-keys td { padding: 2px 8px 2px 0; vertical-align: top; }
@keyframes g-slider-hint { 50% { box-shadow: 0 0 0 3px #fde68a, 0 0 18px rgba(253,230,138,.55); } }
`

const tileHue = (v, n) => {
  const g = v - 1, r = Math.floor(g / n), c = g % n
  return `hsl(${(230 + r * (300 / n)) % 360} 55% ${44 + (c % 2) * 5}%)`
}

function makeImage(seed, seeded, px = 720) {
  const rnd = seeded(seed)
  const cv = document.createElement('canvas')
  cv.width = cv.height = px
  const g = cv.getContext('2d')
  const hue = Math.floor(rnd() * 360)
  if (rnd() < 0.5) {
    const sky = g.createLinearGradient(0, 0, 0, px)
    sky.addColorStop(0, `hsl(${hue} 70% 22%)`)
    sky.addColorStop(0.6, `hsl(${(hue + 40) % 360} 75% 55%)`)
    sky.addColorStop(1, `hsl(${(hue + 60) % 360} 85% 70%)`)
    g.fillStyle = sky; g.fillRect(0, 0, px, px)
    for (let k = 0; k < 60; k++) {
      g.fillStyle = `rgba(255,255,255,${0.3 + rnd() * 0.6})`
      g.beginPath(); g.arc(rnd() * px, rnd() * px * 0.45, 1 + rnd() * 2, 0, 7); g.fill()
    }
    const sx = px * (0.2 + rnd() * 0.6), sy = px * (0.3 + rnd() * 0.15)
    const sun = g.createRadialGradient(sx, sy, 0, sx, sy, px * 0.22)
    sun.addColorStop(0, `hsl(${(hue + 50) % 360} 100% 85%)`); sun.addColorStop(0.45, `hsl(${(hue + 30) % 360} 95% 65% / .9)`); sun.addColorStop(1, 'transparent')
    g.fillStyle = sun; g.fillRect(0, 0, px, px)
    for (let layer = 0; layer < 4; layer++) {
      const base = px * (0.5 + layer * 0.13)
      g.fillStyle = `hsl(${(hue + 200 + layer * 12) % 360} ${40 - layer * 5}% ${30 - layer * 6}%)`
      g.beginPath(); g.moveTo(0, px)
      let y = base - rnd() * px * 0.1
      for (let x = 0; x <= px; x += px / 12) { y = Math.min(px, Math.max(base - px * 0.22, y + (rnd() - 0.5) * px * 0.12)); g.lineTo(x, y) }
      g.lineTo(px, px); g.closePath(); g.fill()
    }
  } else {
    const bg = g.createLinearGradient(0, 0, px, px)
    bg.addColorStop(0, `hsl(${hue} 65% 30%)`); bg.addColorStop(1, `hsl(${(hue + 150) % 360} 65% 22%)`)
    g.fillStyle = bg; g.fillRect(0, 0, px, px)
    const cx = px * (0.3 + rnd() * 0.4), cy = px * (0.3 + rnd() * 0.4)
    for (let r = px * 0.9; r > 10; r -= px * 0.06) {
      g.strokeStyle = `hsl(${(hue + r / 3) % 360} 80% 65% / .35)`; g.lineWidth = px * 0.012
      g.beginPath(); g.arc(cx, cy, r, 0, 7); g.stroke()
    }
    for (let k = 0; k < 14; k++) {
      g.fillStyle = `hsl(${(hue + rnd() * 180) % 360} 80% ${45 + rnd() * 25}% / ${0.35 + rnd() * 0.4})`
      g.beginPath()
      const x = rnd() * px, y = rnd() * px, s = px * (0.06 + rnd() * 0.16), sides = 3 + Math.floor(rnd() * 4), rot = rnd() * 7
      for (let a = 0; a <= sides; a++) g.lineTo(x + s * Math.cos(rot + (a * 2 * Math.PI) / sides), y + s * Math.sin(rot + (a * 2 * Math.PI) / sides))
      g.fill()
    }
    g.strokeStyle = 'rgba(255,255,255,.18)'; g.lineWidth = 2
    for (let k = -px; k < px; k += px / 9) { g.beginPath(); g.moveTo(k, 0); g.lineTo(k + px, px); g.stroke() }
  }
  return cv.toDataURL('image/jpeg', 0.9)
}

export default {
  async mount(root, ctx) {
    const { h, format } = ctx
    let prefs = { size: 3, mode: 'num', labels: false }
    try { prefs = { ...prefs, ...((await ctx.prefs.get()) || {}) } } catch { /* defaults */ }
    if (![3, 4, 5].includes(prefs.size)) prefs.size = 3

    let st = null
    let won = false, paused = false, alive = true
    let lastTick = performance.now()
    let saveTimer = 0, closeOverlay = null
    let hintTile = 0, solving = false, reqId = 0
    let worker = null
    let image = '', imageSeed = null
    let S = 300
    let tileEls = new Map()

    const board = h('div', { class: 'board' })
    const preview = h('div', { class: 'preview' })
    const mini = h('div', { class: 'mini' })
    const info = h('div', { class: 'info' })
    const hintBtn = h('button', { onmousedown: (e) => e.preventDefault(), onclick: () => hint(), title: 'Indice (H)' }, '💡 Indice')
    const side = h('div', { class: 'side' }, preview, mini, hintBtn, info)
    const wrap = h('div', { class: 'g-slider' }, board, side)
    root.append(h('style', null, CSS), wrap)

    const movesPill = h('span', { class: 'pill', title: 'Coups' })
    const timePill = h('span', { class: 'pill', title: 'Temps' })
    const hintPill = h('span', { class: 'pill', title: 'Indices utilisés' })
    ctx.setStatus(hintPill, movesPill, timePill)

    const sizeSeg = h('div', { class: 'seg' }, SIZES.map(([n, label]) => h('button', { 'data-n': n, onclick: () => newGame(n) }, label)))
    const modeSeg = h('div', { class: 'seg' },
      h('button', { 'data-m': 'num', onclick: () => setMode('num') }, 'Nombres'),
      h('button', { 'data-m': 'img', onclick: () => setMode('img') }, 'Image'))
    const labelsBtn = h('button', { class: 'icon', title: 'Afficher les numéros sur l’image', onclick: () => { prefs.labels = !prefs.labels; ctx.prefs.set(prefs); build() } }, '#')
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => newGame(st?.n || prefs.size) }, 'Nouvelle partie'),
      sizeSeg, modeSeg, labelsBtn,
      h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => showScores() }, '🏆'),
      h('button', { class: 'icon', title: 'Statistiques', onclick: () => showStats() }, '📊'),
      h('button', { class: 'icon', title: 'Aide', onclick: () => showHelp() }, '❔'))

    const fit = () => {
      const W = root.clientWidth - 24, H = root.clientHeight - 24
      const rowS = Math.min(H, W - 180 - 22)
      const colS = Math.min(W, H - 110)
      const useCol = colS > rowS + 30
      wrap.classList.toggle('col', useCol)
      S = Math.max(200, Math.floor(useCol ? colS : rowS))
      board.style.width = board.style.height = `${S}px`
      layout(true)
    }
    const ro = new ResizeObserver(fit)
    ro.observe(root)

    function build() {
      if (!st) return
      const n = st.n
      if (prefs.mode === 'img' && imageSeed !== st.seed) { image = makeImage(st.seed, ctx.seeded); imageSeed = st.seed }
      wrap.classList.toggle('img', prefs.mode === 'img')
      board.classList.toggle('img', prefs.mode === 'img')
      board.replaceChildren()
      tileEls = new Map()
      for (let v = 1; v <= n * n; v++) {
        const face = h('div', { class: 'face' })
        const el = h('div', { class: `tile${v === n * n ? ' last' : ''}` }, face)
        el.style.width = el.style.height = `${100 / n}%`
        if (prefs.mode === 'num') {
          face.style.background = tileHue(v, n)
          face.append(v === n * n ? '' : String(v))
        } else {
          face.style.backgroundImage = `url(${image})`
          if (prefs.labels && v < n * n) face.append(h('span', { class: 'num' }, String(v)))
        }
        if (v < n * n) el.addEventListener('pointerdown', (e) => { e.preventDefault(); clickTile(v) })
        tileEls.set(v, el)
        board.append(el)
      }
      preview.style.display = prefs.mode === 'img' ? '' : 'none'
      preview.style.backgroundImage = prefs.mode === 'img' ? `url(${image})` : ''
      mini.style.display = prefs.mode === 'num' ? '' : 'none'
      mini.style.gridTemplateColumns = `repeat(${n}, 1fr)`
      mini.replaceChildren(...[...Array(n * n)].map((_, i) => h('span', { style: { background: i < n * n - 1 ? tileHue(i + 1, n) : 'transparent' } }, i < n * n - 1 ? String(i + 1) : '')))
      for (const b of sizeSeg.children) b.classList.toggle('on', +b.dataset.n === n)
      for (const b of modeSeg.children) b.classList.toggle('on', b.dataset.m === prefs.mode)
      labelsBtn.classList.toggle('on', !!prefs.labels)
      labelsBtn.style.display = prefs.mode === 'img' ? '' : 'none'
      layout(true)
    }

    function layout(instant = false) {
      if (!st) return
      const n = st.n, cell = S / n
      const posOf = new Map(st.tiles.map((v, i) => [v, i]))
      const blank = st.tiles.indexOf(0)
      for (const [v, el] of tileEls) {
        const i = v === n * n ? (won ? blank : n * n - 1) : posOf.get(v)
        const r = Math.floor(i / n), c = i % n
        if (instant) el.classList.add('nomove')
        el.style.transform = `translate(${c * 100}%, ${r * 100}%)`
        el.classList.toggle('ok', v < n * n && i === v - 1)
        el.classList.toggle('hint', v === hintTile)
        const face = el.firstChild
        if (prefs.mode === 'img') {
          const g = v - 1, gr = Math.floor(g / n), gc = g % n
          face.style.backgroundSize = `${S}px ${S}px`
          const pad = won ? 0 : 3
          face.style.backgroundPosition = `${-(gc * cell + pad)}px ${-(gr * cell + pad)}px`
        } else face.style.fontSize = `${Math.round(cell * 0.4)}px`
        if (v === hintTile) {
          const d = blank - i
          face.dataset.arrow = d === 1 ? '→' : d === -1 ? '←' : d > 0 ? '↓' : '↑'
        } else delete face.dataset.arrow
      }
      if (instant) { void board.offsetWidth; for (const el of tileEls.values()) el.classList.remove('nomove') }
      board.classList.toggle('solved', won)
      movesPill.textContent = `${st.moves} coup${st.moves > 1 ? 's' : ''}`
      hintPill.textContent = `💡 ${st.hints}`
      hintBtn.disabled = won || solving
      hintBtn.textContent = solving ? '⏳ Calcul…' : '💡 Indice'
      updateTime()
    }

    function updateTime() { if (st) timePill.textContent = `⏱ ${format.time(st.elapsed).replace(/\.\d$/, '')}` }
    function tick() {
      const now = performance.now()
      if (st && st.moves > 0 && !won && !paused) st.elapsed += now - lastTick
      lastTick = now
      updateTime()
    }
    const timer = setInterval(tick, 250)

    function persist() {
      clearTimeout(saveTimer)
      if (!st || won) return
      tick()
      return ctx.save.set({ v: 1, ...st })
    }
    const saveSoon = () => { clearTimeout(saveTimer); saveTimer = setTimeout(persist, 500) }

    function slide(v) {
      const i = st.tiles.indexOf(v), b = st.tiles.indexOf(0)
      st.tiles[b] = v; st.tiles[i] = 0
      st.lastMoved = v
      if (st.moves === 0) lastTick = performance.now()
      st.moves++
      if (st.hintPath?.length) {
        if (st.hintPath[0] === v) st.hintPath.shift()
        else { st.hintPath = null; st.hintKind = null }
      }
    }

    function afterMoves(count) {
      if (!count) return
      hintTile = 0
      ctx.sfx('move')
      if (isSolved(st.tiles)) { victory(); return }
      setInfo()
      layout()
      saveSoon()
    }

    function clickTile(v) {
      if (!st || won || paused) return
      const n = st.n, i = st.tiles.indexOf(v), b = st.tiles.indexOf(0)
      const ri = Math.floor(i / n), ci = i % n, rb = Math.floor(b / n), cb = b % n
      if (ri !== rb && ci !== cb) { ctx.sfx('error'); return }
      const step = ri === rb ? Math.sign(i - b) : Math.sign(i - b) * n
      let count = 0
      for (let p = b + step; ; p += step) {
        slide(st.tiles[p])
        count++
        if (p === i) break
      }
      afterMoves(count)
    }

    function arrow(dir) {
      if (!st || won || paused) return
      const n = st.n, b = st.tiles.indexOf(0), r = Math.floor(b / n), c = b % n
      const from = dir === 'up' ? (r < n - 1 ? b + n : -1) : dir === 'down' ? (r > 0 ? b - n : -1)
        : dir === 'left' ? (c < n - 1 ? b + 1 : -1) : (c > 0 ? b - 1 : -1)
      if (from < 0) { ctx.sfx('bounce'); return }
      slide(st.tiles[from])
      afterMoves(1)
    }

    function setInfo(extra = '') {
      if (!st) return
      const kind = st.hintKind
      const left = st.hintPath?.length
      let txt = extra
      if (!txt && left) {
        txt = kind === 'optimal' ? `Solution optimale trouvée : <b>${left}</b> coup${left > 1 ? 's' : ''} restant${left > 1 ? 's' : ''}.`
          : kind === 'approx' ? `Chemin trouvé (non optimal) : <b>${left}</b> coups restants.`
            : 'Suggestion heuristique (pas de solution complète calculée).'
      }
      if (!txt) txt = st.n <= 4 ? 'L’indice calcule une solution optimale (IDA*, 3 s max), sinon un chemin approché.' : 'En 5×5, l’indice suit un chemin approché (A* pondéré), pas forcément optimal.'
      info.innerHTML = txt
    }

    function getWorker() {
      if (!worker) {
        worker = new Worker(new URL('./slider-worker.js', import.meta.url), { type: 'module' })
      }
      return worker
    }
    function solveAsync(tiles, n) {
      return new Promise((resolve, reject) => {
        let w
        try { w = getWorker() } catch (e) { reject(e); return }
        const id = ++reqId
        const timeout = setTimeout(() => { cleanup(); worker?.terminate(); worker = null; reject(new Error('délai dépassé')) }, HINT_BUDGET + 3000)
        const onMsg = ({ data }) => { if (data.id === id) { cleanup(); resolve(data) } }
        const onErr = (e) => { cleanup(); worker?.terminate(); worker = null; e.preventDefault?.(); reject(new Error('worker')) }
        const cleanup = () => { clearTimeout(timeout); w.removeEventListener('message', onMsg); w.removeEventListener('error', onErr) }
        w.addEventListener('message', onMsg)
        w.addEventListener('error', onErr)
        w.postMessage({ id, tiles, n, budget: HINT_BUDGET, avoid: st.lastMoved || 0 })
      })
    }

    async function hint() {
      if (!st || won || paused || solving) return
      if (!st.hintPath?.length) {
        const tiles = st.tiles.slice(), n = st.n, game = st
        solving = true
        layout()
        let res
        try { res = await solveAsync(tiles, n) } catch {
          const path = n === 3 ? idaStar(tiles, 3) : null
          res = path ? { kind: 'optimal', path } : { kind: 'greedy', path: [greedyMove(tiles, n)] }
        }
        solving = false
        if (!alive || st !== game) return
        if (won || st.tiles.join() !== tiles.join()) { layout(); setInfo('La position a changé pendant le calcul : redemandez un indice.'); return }
        st.hintPath = res.path
        st.hintKind = res.kind
      }
      const v = st.hintPath[0]
      if (!canSlide(st.tiles, st.n, v)) { st.hintPath = null; st.hintKind = null; layout(); return }
      st.hints++
      hintTile = v
      ctx.sfx('pop')
      setInfo()
      layout()
      saveSoon()
    }

    async function victory() {
      tick()
      won = true
      hintTile = 0
      clearTimeout(saveTimer)
      layout()
      ctx.sfx('win')
      ctx.save.clear()
      const n = st.n, variant = `${n}x${n}`, moves = st.moves, time = Math.round(st.elapsed)
      setInfo('Puzzle terminé !')
      let res = null
      try {
        res = await ctx.scores.submit(variant, moves, { lowerBetter: true, meta: { time, hints: st.hints, mode: prefs.mode } })
      } catch (e) { ctx.toast(e.message, 'err') }
      try {
        const stats = await ctx.stats.update({
          inc: { played: 1, won: 1, [`won_${n}`]: 1, hints: st.hints },
          min: { [`best_moves_${n}`]: moves, [`best_time_${n}`]: time },
        })
        let top = 0
        for (const [k] of SIZES) if (stats[`best_moves_${k}`] != null) top = k
        if (top) await ctx.stats.update({ set: { hub_best: `${stats[`best_moves_${top}`]} coups (${top}×${top})` } })
      } catch { /* best effort */ }
      if (!alive) return
      const lines = [`${n}×${n} · ${moves} coups · ${format.time(time)}`]
      if (st.hints) lines.push(`${st.hints} indice${st.hints > 1 ? 's' : ''} utilisé${st.hints > 1 ? 's' : ''}`)
      if (res) lines.push(res.record ? '🏆 Nouveau record !' : `Rang ${res.rank} sur ${res.total}`)
      const show = () => {
        closeOverlay = ctx.overlay({
          title: 'Bravo !', text: lines.join('\n'),
          buttons: [
            { label: 'Rejouer', primary: true, onClick: () => newGame(n, true) },
            { label: 'Meilleurs scores', onClick: () => showScores(variant, res?.id).then(() => { if (won && alive) show() }) },
            { label: 'Admirer', onClick: () => {} },
          ],
        })
      }
      setTimeout(() => { if (alive && won) show() }, 600)
    }

    async function newGame(n, force = false) {
      if (!force && st && !won && st.moves > 0) {
        const ok = await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Abandonner', danger: true })
        if (!ok || !alive) return
        ctx.stats.update({ inc: { played: 1, [`abandon_${st.n}`]: 1 } })
      }
      prefs.size = n
      ctx.prefs.set(prefs)
      reqId++
      if (solving) { worker?.terminate(); worker = null; solving = false }
      closeOverlay?.()
      closeOverlay = null
      st = { n, tiles: shuffleSolvable(n), moves: 0, elapsed: 0, hints: 0, seed: Math.floor(Math.random() * 2 ** 31), hintPath: null, hintKind: null }
      won = false; paused = false; hintTile = 0
      board.classList.remove('veiled')
      build()
      setInfo()
      ctx.save.set({ v: 1, ...st })
    }

    function setMode(m) {
      if (prefs.mode === m) return
      prefs.mode = m
      ctx.prefs.set(prefs)
      build()
    }

    function setPaused(p) {
      if (!st || won || paused === p) return
      tick()
      paused = p
      if (p) {
        persist()
        if (st.moves > 0) {
          board.classList.add('veiled')
          closeOverlay = ctx.overlay({ title: 'Pause', text: 'Le chronomètre est arrêté.', buttons: [{ label: 'Reprendre', primary: true, onClick: () => setPaused(false) }] })
        } else paused = false
      } else {
        board.classList.remove('veiled')
        closeOverlay?.()
        closeOverlay = null
        lastTick = performance.now()
      }
    }

    async function showScores(variant = `${st?.n || prefs.size}x${st?.n || prefs.size}`, highlightId = null) {
      await ctx.showScores(variant, {
        lowerBetter: true, highlightId, variants: SIZES.map(([n, l]) => [`${n}x${n}`, l]),
        fmt: (s, m) => `${format.int(s)} coups${m?.time != null ? ` · ${format.time(m.time)}` : ''}${m?.hints ? ` · 💡${m.hints}` : ''}`,
      })
    }

    async function showStats() {
      let s = {}
      try { s = (await ctx.stats.get()) || {} } catch (e) { ctx.toast(e.message, 'err') }
      await ctx.dialog({
        title: 'Statistiques — Taquin',
        body: h('div', null,
          h('p', null, `Parties terminées ou abandonnées : ${s.played || 0} · puzzles résolus : ${s.won || 0} · indices : ${s.hints || 0}`),
          h('table', { class: 'help-keys', style: { marginTop: '10px', width: '100%' } },
            h('thead', null, h('tr', { class: 'muted' }, h('td', null, 'Taille'), h('td', null, 'Résolus'), h('td', null, 'Moins de coups'), h('td', null, 'Meilleur temps'), h('td', null, 'Abandons'))),
            h('tbody', null, SIZES.map(([n, l]) => h('tr', null, h('td', null, l), h('td', null, String(s[`won_${n}`] || 0)),
              h('td', null, s[`best_moves_${n}`] != null ? String(s[`best_moves_${n}`]) : '—'),
              h('td', null, s[`best_time_${n}`] != null ? format.time(s[`best_time_${n}`]) : '—'),
              h('td', null, String(s[`abandon_${n}`] || 0))))))),
      })
    }

    function showHelp() {
      const k = (t) => h('span', { class: 'kbd' }, t)
      ctx.dialog({
        title: 'Taquin — aide',
        body: h('div', null,
          h('p', null, 'Remettez les pièces dans l’ordre (1, 2, 3… de gauche à droite, de haut en bas, case vide en bas à droite) ou reconstituez l’image. Chaque mélange est garanti soluble.'),
          h('p', { style: { margin: '8px 0' } }, 'Cliquez sur une pièce alignée avec la case vide : toute la rangée entre elles glisse. Le score est le nombre de pièces déplacées (moins = mieux). Le chronomètre démarre au premier coup.'),
          h('p', { style: { margin: '8px 0' } }, 'Indice : en 3×3 et 4×4, un solveur IDA* cherche la solution optimale (3 s max) ; s’il n’aboutit pas, un chemin approché est proposé. En 5×5 l’indice suit un chemin approché, pas forcément le plus court.'),
          h('table', { class: 'help-keys' }, h('tbody', null,
            h('tr', null, h('td', null, k('Flèches')), h('td', null, 'Faire glisser la pièce voisine vers la case vide')),
            h('tr', null, h('td', null, k('H')), h('td', null, 'Indice')),
            h('tr', null, h('td', null, k('P'), ' ', k('Échap')), h('td', null, 'Pause')),
            h('tr', null, h('td', null, 'Manette'), h('td', null, 'Croix : glisser · Y : indice · Start : pause'))))),
      })
    }

    ctx.onAction((a) => {
      if (!st) return
      if (a === 'start') { setPaused(!paused); return }
      if (paused || won) return
      if (a === 'up' || a === 'down' || a === 'left' || a === 'right') arrow(a)
      else if (a === 'y') hint()
    })
    const onKey = (e) => {
      if (document.querySelector('.dlg-back') || e.ctrlKey || e.metaKey || e.altKey) return
      const t = e.target
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (e.code === 'KeyH') { e.preventDefault(); hint() }
    }
    document.addEventListener('keydown', onKey)

    let saved = null
    try { saved = await ctx.save.get() } catch (e) { ctx.toast(e.message, 'err') }
    if (!alive) return {}
    if (saved && saved.v === 1 && [3, 4, 5].includes(saved.n) && Array.isArray(saved.tiles) && saved.tiles.length === saved.n * saved.n) {
      st = { hints: 0, moves: 0, elapsed: 0, seed: 1, hintPath: null, hintKind: null, ...saved }
      delete st.v
      fit()
      build()
      setInfo()
      if (st.moves > 0) ctx.toast('Partie reprise')
    } else {
      fit()
      await newGame(prefs.size, true)
    }

    return {
      pause() { setPaused(true) },
      destroy() {
        alive = false
        if (st && !won) persist()
        clearInterval(timer)
        clearTimeout(saveTimer)
        ro.disconnect()
        worker?.terminate()
        worker = null
        document.removeEventListener('keydown', onKey)
      },
    }
  },
}
