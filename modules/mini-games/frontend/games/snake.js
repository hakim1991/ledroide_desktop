const COLS = 24, ROWS = 18
const START_Y = 9
const STEP = 1000 / 120
const DIRS = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] }
const OPP = { up: 'down', down: 'up', left: 'right', right: 'left' }
const MODES = [['walls', 'Murs'], ['open', 'Ouvert'], ['obstacles', 'Obstacles']]
const MODE_LABEL = Object.fromEntries(MODES)
const LEVEL_FOOD = 12
const GOLD_MS = 7000

const CSS = `
.g-snake { flex: 1; min-height: 0; display: grid; place-items: center; padding: 10px; }
.g-snake canvas { display: block; border-radius: 10px; box-shadow: 0 10px 40px rgba(0,0,0,.45); touch-action: none; }
.g-snake-help kbd, .g-snake-help .kbd { margin: 0 1px; }
.g-snake-help ul { margin: 6px 0 0 18px; }
.g-snake-help li { margin: 2px 0; }
`

const cellKey = (x, y) => y * COLS + x

function floodOk(blocked) {
  const free = COLS * ROWS - blocked.size
  const seen = new Set([cellKey(2, START_Y)])
  const stack = [[2, START_Y]]
  while (stack.length) {
    const [x, y] = stack.pop()
    for (const [dx, dy] of Object.values(DIRS)) {
      const nx = x + dx, ny = y + dy
      if (nx < 0 || ny < 0 || nx >= COLS || ny >= ROWS) continue
      const k = cellKey(nx, ny)
      if (blocked.has(k) || seen.has(k)) continue
      seen.add(k)
      stack.push([nx, ny])
    }
  }
  return seen.size === free
}

export function genObstacles(level, rnd = Math.random) {
  const blocked = new Set()
  const put = (x, y) => {
    if (x < 0 || y < 0 || x >= COLS || y >= ROWS) return
    if (Math.abs(y - START_Y) <= 1) return
    blocked.add(cellKey(x, y))
  }
  for (let attempt = 0; attempt < 120; attempt++) {
    blocked.clear()
    const n = Math.min(2 + level, 9)
    const quad = level % 2 === 0
    for (let i = 0; i < n; i++) {
      const horiz = rnd() < 0.5
      const len = 2 + Math.floor(rnd() * Math.min(2 + level, 6))
      const x = 1 + Math.floor(rnd() * (COLS / 2 - 1)), y = 1 + Math.floor(rnd() * (quad ? ROWS / 2 - 1 : ROWS - 2))
      for (let k = 0; k < len; k++) {
        const cx = horiz ? x + k : x, cy = horiz ? y : y + k
        if (cx >= COLS / 2) break
        put(cx, cy); put(COLS - 1 - cx, cy)
        if (quad) { put(cx, ROWS - 1 - cy); put(COLS - 1 - cx, ROWS - 1 - cy) }
      }
    }
    if (blocked.size >= 6 && floodOk(blocked)) return [...blocked].map((k) => [k % COLS, Math.floor(k / COLS)])
  }
  return []
}

export function stepInterval(len, level = 1) {
  return Math.max(48, 135 - (len - 4) * 2.2 - (level - 1) * 4)
}

function newState(mode, level = 1, score = 0) {
  const s = {
    mode, level, score, eaten: 0, levelEaten: 0,
    snake: [[6, START_Y], [5, START_Y], [4, START_Y], [3, START_Y]],
    dir: 'right', queue: [], grow: 0,
    obstacles: mode === 'obstacles' ? genObstacles(level) : [],
    food: null, gold: null, goldCooldown: 4, time: 0,
  }
  s.food = freeCell(s)
  return s
}

function freeCell(s, rnd = Math.random) {
  const used = new Set()
  for (const [x, y] of s.snake) used.add(cellKey(x, y))
  for (const [x, y] of s.obstacles) used.add(cellKey(x, y))
  if (s.food) used.add(cellKey(s.food[0], s.food[1]))
  if (s.gold) used.add(cellKey(s.gold.x, s.gold.y))
  const [hx, hy] = s.snake[0]
  const free = []
  for (let y = 0; y < ROWS; y++) for (let x = 0; x < COLS; x++) {
    if (used.has(cellKey(x, y))) continue
    if (Math.abs(x - hx) + Math.abs(y - hy) < 3) continue
    free.push([x, y])
  }
  if (!free.length) {
    for (let y = 0; y < ROWS; y++) for (let x = 0; x < COLS; x++) if (!used.has(cellKey(x, y))) free.push([x, y])
  }
  return free.length ? free[Math.floor(rnd() * free.length)] : null
}

// Returns 'dead' | 'eat' | 'gold' | 'level' | 'full' | null
export function stepSnake(s, rnd = Math.random) {
  if (s.queue.length) s.dir = s.queue.shift()
  const [dx, dy] = DIRS[s.dir]
  let [x, y] = s.snake[0]
  x += dx; y += dy
  if (x < 0 || y < 0 || x >= COLS || y >= ROWS) {
    if (s.mode !== 'open') return 'dead'
    x = (x + COLS) % COLS; y = (y + ROWS) % ROWS
  }
  if (s.obstacles.some(([ox, oy]) => ox === x && oy === y)) return 'dead'
  const eating = s.food && s.food[0] === x && s.food[1] === y
  const body = s.grow > 0 || eating ? s.snake : s.snake.slice(0, -1)
  if (body.some(([bx, by]) => bx === x && by === y)) return 'dead'
  s.snake.unshift([x, y])
  if (eating) s.grow += 1
  if (s.grow > 0) s.grow--
  else s.snake.pop()
  let ev = null
  if (eating) {
    s.eaten++; s.levelEaten++
    s.score += 10 + (s.mode === 'obstacles' ? (s.level - 1) * 5 : 0)
    s.food = null
    if (s.mode === 'obstacles' && s.levelEaten >= LEVEL_FOOD) return 'level'
    s.food = freeCell(s, rnd)
    if (!s.food) return 'full'
    s.goldCooldown--
    if (!s.gold && s.goldCooldown <= 0 && rnd() < 0.35) {
      const c = freeCell(s, rnd)
      if (c) { s.gold = { x: c[0], y: c[1], t: GOLD_MS }; s.goldCooldown = 4 }
    }
    ev = 'eat'
  }
  if (s.gold && s.gold.x === x && s.gold.y === y) {
    s.score += 50 + (s.mode === 'obstacles' ? (s.level - 1) * 10 : 0)
    s.gold = null
    ev = 'gold'
  }
  return ev
}

export function queueDir(s, d) {
  const last = s.queue.length ? s.queue[s.queue.length - 1] : s.dir
  if (d === last || d === OPP[last] || s.queue.length >= 2) return false
  s.queue.push(d)
  return true
}

function validSave(st) {
  return st && Array.isArray(st.snake) && st.snake.length >= 2 && DIRS[st.dir] && MODE_LABEL[st.mode] && Array.isArray(st.obstacles)
}

export default {
  async mount(root, ctx) {
    const { h } = ctx
    const prefs = Object.assign({ mode: 'walls' }, await ctx.prefs.get().catch(() => ({})))
    if (!MODE_LABEL[prefs.mode]) prefs.mode = 'walls'

    const canvas = h('canvas')
    const g = canvas.getContext('2d')
    const wrap = h('div', { class: 'g-snake' }, canvas)
    root.append(h('style', null, CSS), wrap)

    let s = null
    let phase = 'idle'
    let cd = 0, acc = 0, stepAcc = 0, deathT = 0, flashT = 0, banner = null
    let closeOv = null
    let overAt = 0
    let saveTimer = 0
    let cs = 20, W = 0, H = 0
    let best = 0
    const particles = []

    const pills = { score: h('span', { class: 'pill' }), len: h('span', { class: 'pill' }), lvl: h('span', { class: 'pill' }), best: h('span', { class: 'pill', title: 'Record du mode' }) }
    ctx.setStatus(pills.score, pills.len, pills.lvl, pills.best)
    const updateStatus = () => {
      pills.score.textContent = `Score ${ctx.format.int(s ? s.score : 0)}`
      pills.len.textContent = `Longueur ${s ? s.snake.length : 4}`
      pills.lvl.textContent = `Niveau ${s ? s.level : 1}`
      pills.lvl.classList.toggle('hidden', (s ? s.mode : prefs.mode) !== 'obstacles')
      pills.best.textContent = `★ ${ctx.format.int(Math.max(best, s ? s.score : 0))}`
    }
    const loadBest = async (mode) => {
      const rows = await ctx.scores.list(mode, { limit: 1 }).catch(() => [])
      best = rows[0]?.score || 0
      updateStatus()
    }

    const modeSeg = h('div', { class: 'seg' }, MODES.map(([v, label]) =>
      h('button', { 'data-mode': v, onclick: () => chooseMode(v), title: `Mode ${label}` }, label)))
    const syncModeSeg = () => { for (const b of modeSeg.children) b.classList.toggle('on', b.dataset.mode === (s ? s.mode : prefs.mode)) }
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => askNewGame(prefs.mode) }, 'Nouvelle partie'),
      modeSeg,
      h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => withPause(() => showScores()) }, '🏆'),
      h('button', { class: 'icon', title: 'Aide', onclick: () => withPause(() => showHelp()) }, '❔'),
    )

    const showScores = (highlightId = null) => ctx.showScores(s ? s.mode : prefs.mode, { highlightId, variants: MODES, title: 'Meilleurs scores — Serpent' })
    const showHelp = () => ctx.dialog({
      title: 'Serpent — aide',
      body: h('div', { class: 'g-snake-help' },
        h('p', null, 'Dirigez le serpent vers les fruits : chaque fruit le rallonge et l’accélère. Ne heurtez ni votre queue, ni un obstacle.'),
        h('ul', null,
          h('li', null, h('b', null, 'Murs'), ' : les bords sont mortels.'),
          h('li', null, h('b', null, 'Ouvert'), ' : on ressort de l’autre côté.'),
          h('li', null, h('b', null, 'Obstacles'), ` : bords mortels et blocs de pierre ; niveau suivant tous les ${LEVEL_FOOD} fruits (points bonus par niveau).`),
          h('li', null, 'Fruit doré : apparaît de temps en temps, +50 points, disparaît au bout de quelques secondes.')),
        h('p', { style: { marginTop: '10px' } }, 'Touches : ',
          h('span', { class: 'kbd' }, '←↑→↓'), ' / ', h('span', { class: 'kbd' }, 'ZQSD'), ' / ', h('span', { class: 'kbd' }, 'WASD'),
          ' diriger (deux virages rapides sont mémorisés) · ', h('span', { class: 'kbd' }, 'P'), ' ou ', h('span', { class: 'kbd' }, 'Échap'),
          ' pause · ', h('span', { class: 'kbd' }, 'Espace'), ' reprendre / rejouer. Manette : croix ou stick, Start = pause. Souris / tactile : glisser dans la direction voulue.')),
    })

    async function withPause(fn) {
      const wasRunning = phase === 'play' || phase === 'countdown'
      if (wasRunning) { phase = 'paused'; saveNow() }
      await fn()
      if (wasRunning) showPause()
    }

    function layout() {
      const aw = Math.max(200, wrap.clientWidth - 20), ah = Math.max(160, wrap.clientHeight - 20)
      cs = Math.max(8, Math.floor(Math.min(aw / COLS, ah / ROWS)))
      W = cs * COLS; H = cs * ROWS
      const d = window.devicePixelRatio || 1
      canvas.style.width = `${W}px`; canvas.style.height = `${H}px`
      canvas.width = Math.round(W * d); canvas.height = Math.round(H * d)
      g.setTransform(d, 0, 0, d, 0, 0)
      draw()
    }
    const ro = new ResizeObserver(layout)
    ro.observe(wrap)
    ctx.onCleanup(() => ro.disconnect())

    const serialize = () => JSON.parse(JSON.stringify(s))
    function saveNow() {
      clearTimeout(saveTimer)
      if (s && phase !== 'over' && phase !== 'dying' && phase !== 'idle') ctx.save.set(serialize())
    }
    const saveSoon = () => { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 500) }
    ctx.onCleanup(() => clearTimeout(saveTimer))

    function closeOverlay() { closeOv?.(); closeOv = null }

    function startCountdown() {
      closeOverlay()
      phase = 'countdown'; cd = 3000; acc = 0
      ctx.sfx('tick')
    }

    function showPause(text = 'Partie en pause.') {
      phase = 'paused'
      closeOv = ctx.overlay({
        title: 'Pause',
        text: `${text}\nScore ${ctx.format.int(s.score)} · longueur ${s.snake.length}${s.mode === 'obstacles' ? ` · niveau ${s.level}` : ''} (${MODE_LABEL[s.mode]})`,
        buttons: [
          { label: 'Reprendre', primary: true, onClick: startCountdown },
          { label: 'Nouvelle partie', onClick: () => askNewGame(s.mode) },
        ],
      })
    }

    function pauseGame() {
      if (phase === 'play' || phase === 'countdown') { saveNow(); showPause() }
    }

    function newGame(mode) {
      closeOverlay()
      s = newState(mode)
      particles.length = 0
      banner = null
      prefs.mode = mode
      syncModeSeg(); updateStatus(); loadBest(mode)
      startCountdown()
      saveNow()
    }

    async function askNewGame(mode) {
      const running = s && (phase === 'play' || phase === 'countdown' || phase === 'paused')
      if (running && s.score > 0) {
        phase = 'paused'
        saveNow()
        closeOverlay()
        const ok = await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Nouvelle partie', danger: true })
        if (!ok) { showPause(); return }
      }
      newGame(mode)
    }

    function chooseMode(mode) {
      if (s && mode === s.mode && phase !== 'over' && phase !== 'idle') return
      if (!s || phase === 'over' || phase === 'idle') {
        prefs.mode = mode
        ctx.prefs.set(prefs)
        newGame(mode)
        return
      }
      prefs.mode = mode
      ctx.prefs.set(prefs)
      askNewGame(mode).then(syncModeSeg)
    }

    function burst(x, y, color, n = 10) {
      for (let i = 0; i < n; i++) {
        const a = Math.random() * Math.PI * 2, v = 0.04 + Math.random() * 0.12
        particles.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 450 + Math.random() * 250, max: 700, color })
      }
    }

    async function gameOver(full = false) {
      phase = 'over'
      overAt = performance.now()
      clearTimeout(saveTimer)
      ctx.save.clear()
      ctx.sfx(full ? 'win' : 'lose')
      const score = s.score, mode = s.mode, len = s.snake.length
      let r = null
      try {
        r = await ctx.scores.submit(mode, score, { meta: { length: len, level: s.level } })
      } catch (e) { ctx.toast(e.message, 'err') }
      const st = await ctx.stats.update({ inc: { played: 1, eaten: s.eaten }, max: { [`best_${mode}`]: score, best_length: len } })
      const top = Math.max(st.best_walls || 0, st.best_open || 0, st.best_obstacles || 0)
      ctx.stats.update({ set: { hub_best: ctx.format.int(top) } })
      if (score > best) best = score
      updateStatus()
      if (phase !== 'over') return
      const lines = [`Score ${ctx.format.int(score)} · longueur ${len}${mode === 'obstacles' ? ` · niveau ${s.level}` : ''}`]
      if (r?.record && score > 0) lines.push('Nouveau record !')
      else if (r?.rank) lines.push(`Rang ${r.rank} sur ${r.total}`)
      const showOver = () => {
        closeOv = ctx.overlay({
          title: full ? 'Plateau rempli !' : 'Perdu !',
          text: lines.join('\n'),
          buttons: [
            { label: 'Rejouer', primary: true, onClick: () => newGame(mode) },
            { label: '🏆 Scores', onClick: () => showScores(r?.id).then(() => { if (phase === 'over') showOver() }) },
          ],
        })
      }
      showOver()
    }

    function update(dt) {
      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i]
        p.life -= dt; p.x += p.vx * dt; p.y += p.vy * dt
        if (p.life <= 0) particles.splice(i, 1)
      }
      if (flashT > 0) flashT -= dt
      if (banner) { banner.t -= dt; if (banner.t <= 0) banner = null }
      if (phase === 'countdown') {
        const before = Math.ceil(cd / 1000)
        cd -= dt
        if (cd <= 0) { phase = 'play'; stepAcc = 0; ctx.sfx('click') }
        else if (Math.ceil(cd / 1000) !== before) ctx.sfx('tick')
        return
      }
      if (phase === 'dying') {
        deathT -= dt
        if (deathT <= 0) gameOver()
        return
      }
      if (phase !== 'play') return
      s.time += dt
      if (s.gold) {
        s.gold.t -= dt
        if (s.gold.t <= 0) s.gold = null
      }
      stepAcc += dt
      const interval = stepInterval(s.snake.length, s.level)
      while (stepAcc >= interval && phase === 'play') {
        stepAcc -= interval
        const ev = stepSnake(s)
        const [hx, hy] = s.snake[0]
        if (ev === 'dead') {
          ctx.sfx('hit')
          phase = 'dying'; deathT = 750; flashT = 400
          break
        } else if (ev === 'eat') {
          ctx.sfx('eat'); burst(hx + 0.5, hy + 0.5, '#fb7185'); saveSoon()
        } else if (ev === 'gold') {
          ctx.sfx('pop'); burst(hx + 0.5, hy + 0.5, '#fde047', 18)
          banner = { text: '+50', t: 900, x: hx, y: hy }
          saveSoon()
        } else if (ev === 'level') {
          ctx.sfx('win')
          const lvl = s.level + 1
          const keep = { score: s.score, eaten: s.eaten, time: s.time }
          s = newState('obstacles', lvl, keep.score)
          s.eaten = keep.eaten; s.time = keep.time
          banner = { text: `Niveau ${lvl}`, t: 1600, big: true }
          startCountdown()
          saveNow()
        } else if (ev === 'full') {
          gameOver(true)
        }
        updateStatus()
      }
    }

    function roundRect(x, y, w, hh, r) {
      g.beginPath()
      g.roundRect ? g.roundRect(x, y, w, hh, r) : g.rect(x, y, w, hh)
    }

    function draw() {
      if (!W) return
      g.save()
      if (flashT > 0) g.translate((Math.random() - 0.5) * 6 * (flashT / 400), (Math.random() - 0.5) * 6 * (flashT / 400))
      const bg = g.createLinearGradient(0, 0, 0, H)
      bg.addColorStop(0, '#13201c'); bg.addColorStop(1, '#0d1614')
      g.fillStyle = bg
      g.fillRect(0, 0, W, H)
      g.fillStyle = 'rgba(255,255,255,0.035)'
      for (let y = 0; y < ROWS; y++) for (let x = 0; x < COLS; x++) if ((x + y) % 2 === 0) g.fillRect(x * cs, y * cs, cs, cs)
      const mode = s ? s.mode : prefs.mode
      g.lineWidth = 3
      if (mode === 'open') {
        g.setLineDash([cs * 0.4, cs * 0.4])
        g.strokeStyle = 'rgba(94,234,212,0.35)'
      } else {
        g.setLineDash([])
        g.strokeStyle = 'rgba(248,113,113,0.55)'
      }
      g.strokeRect(1.5, 1.5, W - 3, H - 3)
      g.setLineDash([])

      if (s) {
        for (const [x, y] of s.obstacles) {
          const px = x * cs, py = y * cs
          g.fillStyle = '#4b4560'
          g.fillRect(px + 1, py + 1, cs - 2, cs - 2)
          g.fillStyle = '#6b6385'
          g.fillRect(px + 1, py + 1, cs - 2, Math.max(2, cs * 0.18))
          g.fillStyle = '#332f43'
          g.fillRect(px + 1, py + cs - 1 - Math.max(2, cs * 0.18), cs - 2, Math.max(2, cs * 0.18))
        }
        const t = performance.now()
        if (s.food) {
          const [fx, fy] = s.food
          const cx = (fx + 0.5) * cs, cy = (fy + 0.5) * cs, r = cs * (0.34 + 0.03 * Math.sin(t / 180))
          g.fillStyle = '#f43f5e'
          g.beginPath(); g.arc(cx, cy + cs * 0.04, r, 0, Math.PI * 2); g.fill()
          g.fillStyle = 'rgba(255,255,255,0.45)'
          g.beginPath(); g.arc(cx - r * 0.35, cy - r * 0.25, r * 0.28, 0, Math.PI * 2); g.fill()
          g.fillStyle = '#4ade80'
          g.beginPath(); g.ellipse(cx + r * 0.35, cy - r * 0.95, r * 0.42, r * 0.2, -0.6, 0, Math.PI * 2); g.fill()
        }
        if (s.gold && (s.gold.t > 2000 || Math.floor(t / 120) % 2 === 0)) {
          const cx = (s.gold.x + 0.5) * cs, cy = (s.gold.y + 0.5) * cs
          const glow = g.createRadialGradient(cx, cy, 0, cx, cy, cs)
          glow.addColorStop(0, 'rgba(253,224,71,0.55)'); glow.addColorStop(1, 'rgba(253,224,71,0)')
          g.fillStyle = glow
          g.fillRect(cx - cs, cy - cs, cs * 2, cs * 2)
          g.fillStyle = '#facc15'
          g.beginPath()
          for (let i = 0; i < 10; i++) {
            const a = -Math.PI / 2 + i * Math.PI / 5 + t / 900, rr = i % 2 ? cs * 0.17 : cs * 0.4
            g.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr)
          }
          g.closePath(); g.fill()
          g.strokeStyle = 'rgba(254,240,138,0.9)'; g.lineWidth = 2
          g.beginPath(); g.arc(cx, cy, cs * 0.52, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * (s.gold.t / GOLD_MS)); g.stroke()
        }
        const n = s.snake.length
        const dead = phase === 'dying' || phase === 'over'
        const color = (i) => dead ? `hsl(0, 20%, ${45 - (i / n) * 15}%)` : `hsl(${158 + (i / n) * 40}, 70%, ${56 - (i / n) * 18}%)`
        const pad = cs * 0.1
        for (let i = n - 1; i >= 0; i--) {
          const [x, y] = s.snake[i]
          g.fillStyle = color(i)
          if (i > 0) {
            const [px, py] = s.snake[i - 1]
            if (Math.abs(px - x) + Math.abs(py - y) === 1) {
              const minx = Math.min(px, x), miny = Math.min(py, y)
              g.fillRect(minx * cs + pad, miny * cs + pad, (Math.abs(px - x) + 1) * cs - pad * 2, (Math.abs(py - y) + 1) * cs - pad * 2)
            }
          }
          roundRect(x * cs + pad, y * cs + pad, cs - pad * 2, cs - pad * 2, cs * 0.28)
          g.fill()
        }
        const [hx, hy] = s.snake[0]
        const [dx, dy] = DIRS[s.dir]
        const cx = (hx + 0.5) * cs, cy = (hy + 0.5) * cs
        const ex = -dy, ey = dx
        for (const side of [-1, 1]) {
          const ox = cx + dx * cs * 0.14 + ex * side * cs * 0.18, oy = cy + dy * cs * 0.14 + ey * side * cs * 0.18
          g.fillStyle = '#f8fafc'
          g.beginPath(); g.arc(ox, oy, cs * 0.12, 0, Math.PI * 2); g.fill()
          g.fillStyle = '#0f172a'
          if (dead) {
            g.strokeStyle = '#0f172a'; g.lineWidth = 1.5
            g.beginPath(); g.moveTo(ox - cs * 0.07, oy - cs * 0.07); g.lineTo(ox + cs * 0.07, oy + cs * 0.07)
            g.moveTo(ox + cs * 0.07, oy - cs * 0.07); g.lineTo(ox - cs * 0.07, oy + cs * 0.07); g.stroke()
          } else {
            g.beginPath(); g.arc(ox + dx * cs * 0.04, oy + dy * cs * 0.04, cs * 0.06, 0, Math.PI * 2); g.fill()
          }
        }
        for (const p of particles) {
          g.globalAlpha = Math.max(0, p.life / p.max)
          g.fillStyle = p.color
          g.fillRect(p.x * cs - 2, p.y * cs - 2, 4, 4)
        }
        g.globalAlpha = 1
      }
      if (flashT > 0) {
        g.fillStyle = `rgba(239,68,68,${0.35 * flashT / 400})`
        g.fillRect(0, 0, W, H)
      }
      if (banner) {
        g.textAlign = 'center'; g.textBaseline = 'middle'
        g.globalAlpha = Math.min(1, banner.t / 300)
        if (banner.big) {
          g.font = `700 ${Math.round(cs * 1.6)}px system-ui, sans-serif`
          g.fillStyle = '#fde68a'
          g.fillText(banner.text, W / 2, H * 0.3)
        } else {
          g.font = `700 ${Math.round(cs * 0.8)}px system-ui, sans-serif`
          g.fillStyle = '#fde047'
          g.fillText(banner.text, (banner.x + 0.5) * cs, (banner.y - 0.3) * cs - (900 - banner.t) * 0.02)
        }
        g.globalAlpha = 1
      }
      if (phase === 'countdown') {
        g.fillStyle = 'rgba(5,10,9,0.45)'
        g.fillRect(0, 0, W, H)
        const n = Math.ceil(cd / 1000), f = (cd % 1000) / 1000
        g.textAlign = 'center'; g.textBaseline = 'middle'
        g.font = `800 ${Math.round(cs * (3 + f * 1.5))}px system-ui, sans-serif`
        g.globalAlpha = 0.35 + 0.65 * f
        g.fillStyle = '#ecfdf5'
        g.fillText(String(n), W / 2, H / 2)
        g.globalAlpha = 1
      } else if (phase === 'idle') {
        g.fillStyle = 'rgba(5,10,9,0.4)'
        g.fillRect(0, 0, W, H)
      }
      g.restore()
    }

    ctx.loop((dt) => {
      acc += dt
      while (acc >= STEP) { update(STEP); acc -= STEP }
      draw()
    })

    function turn(d) {
      if (!s || phase === 'over' || phase === 'dying' || phase === 'idle') return
      if (phase === 'paused') return
      if (queueDir(s, d)) ctx.sfx('move')
    }

    ctx.onAction((action, ev) => {
      if (action === 'start') {
        if (phase === 'play' || phase === 'countdown') pauseGame()
        else if (phase === 'paused') startCountdown()
        return
      }
      if (DIRS[action]) { if (!ev.repeat) turn(action); return }
      if (action === 'a' && !ev.repeat) {
        if (phase === 'paused') startCountdown()
        else if (phase === 'over' && overSince()) newGame(s.mode)
        else if (phase === 'idle') newGame(prefs.mode)
      }
    })
    const overSince = () => performance.now() - overAt > 600

    let swipe = null
    canvas.addEventListener('pointerdown', (e) => { swipe = { x: e.clientX, y: e.clientY, done: false } })
    canvas.addEventListener('pointermove', (e) => {
      if (!swipe || swipe.done) return
      const dx = e.clientX - swipe.x, dy = e.clientY - swipe.y
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 18) return
      turn(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'))
      swipe = { x: e.clientX, y: e.clientY, done: false }
    })
    const endSwipe = () => { swipe = null }
    canvas.addEventListener('pointerup', endSwipe)
    canvas.addEventListener('pointercancel', endSwipe)
    canvas.addEventListener('click', () => {
      if (phase === 'idle') newGame(prefs.mode)
    })

    const saved = await ctx.save.get().catch(() => null)
    if (validSave(saved)) {
      s = saved
      s.queue = []
      prefs.mode = s.mode
      syncModeSeg(); loadBest(s.mode)
      layout()
      showPause('Partie précédente retrouvée.')
    } else {
      syncModeSeg(); loadBest(prefs.mode)
      layout()
      phase = 'idle'
      closeOv = ctx.overlay({
        title: 'Serpent',
        text: `Mode : ${MODE_LABEL[prefs.mode]}\nMangez les fruits sans vous mordre la queue.`,
        buttons: [{ label: 'Jouer', primary: true, onClick: () => newGame(prefs.mode) }, { label: 'Aide', onClick: () => showHelp().then(() => { if (phase === 'idle') closeOv = ctx.overlay({ title: 'Serpent', text: `Mode : ${MODE_LABEL[prefs.mode]}`, buttons: [{ label: 'Jouer', primary: true, onClick: () => newGame(prefs.mode) }] }) }) }],
      })
    }
    updateStatus()

    return {
      pause() { pauseGame() },
      destroy() { saveNow() },
    }
  },
}
