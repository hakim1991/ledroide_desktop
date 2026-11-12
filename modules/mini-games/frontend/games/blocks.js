export const COLS = 10, ROWS = 22, HIDDEN = 2
const STEP = 1000 / 120
const DAS = 160, ARR = 40, SOFT_MS = 35
const LOCK_MS = 500, MAX_LOCK_RESETS = 15
const CLEAR_MS = 340
const LINE_POINTS = [0, 100, 300, 500, 800]
const LINE_NAMES = ['', 'Simple', 'Double', 'Triple', 'Quadruple !']

const SHAPES = [
  [[0, 0, 0, 0], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]],
  [[1, 1], [1, 1]],
  [[0, 1, 0], [1, 1, 1], [0, 0, 0]],
  [[0, 1, 1], [1, 1, 0], [0, 0, 0]],
  [[1, 1, 0], [0, 1, 1], [0, 0, 0]],
  [[1, 0, 0], [1, 1, 1], [0, 0, 0]],
  [[0, 0, 1], [1, 1, 1], [0, 0, 0]],
]
const COLORS = ['#fb7185', '#38bdf8', '#a3e635', '#f59e0b', '#c084fc', '#2dd4bf', '#facc15']

const rotateCW = (m) => m[0].map((_, x) => m.map((row) => row[x]).reverse())
// ROT[t][r] = list of [dx, dy] cells for piece type t in rotation r
export const ROT = SHAPES.map((m) => {
  const out = []
  let cur = m
  for (let r = 0; r < 4; r++) {
    const cells = []
    cur.forEach((row, y) => row.forEach((v, x) => { if (v) cells.push([x, y]) }))
    out.push(cells)
    cur = rotateCW(cur)
  }
  return out
})
const KICKS = [[0, 0], [-1, 0], [1, 0], [0, -1], [-1, -1], [1, -1], [-2, 0], [2, 0], [0, 1]]

export const emptyBoard = () => Array.from({ length: ROWS }, () => Array(COLS).fill(0))

export function cellsOf(p, dx = 0, dy = 0, r = p.r) {
  return ROT[p.t][r].map(([x, y]) => [p.x + x + dx, p.y + y + dy])
}

export function collides(board, p, dx = 0, dy = 0, r = p.r) {
  for (const [x, y] of cellsOf(p, dx, dy, r)) {
    if (x < 0 || x >= COLS || y >= ROWS) return true
    if (y >= 0 && board[y][x]) return true
  }
  return false
}

export function tryRotate(board, p, dir) {
  if (p.t === 1) return true
  const r = (p.r + dir + 4) % 4
  for (const [kx, ky] of KICKS) {
    if (!collides(board, p, kx, ky, r)) {
      p.x += kx; p.y += ky; p.r = r
      return true
    }
  }
  return false
}

export function spawnPiece(t) {
  const n = SHAPES[t].length
  return { t, r: 0, x: Math.floor((COLS - n) / 2), y: t === 0 ? 0 : 1 }
}

export function fullRows(board) {
  const rows = []
  for (let y = 0; y < ROWS; y++) if (board[y].every(Boolean)) rows.push(y)
  return rows
}

export function removeRows(board, rows) {
  const keep = board.filter((_, y) => !rows.includes(y))
  while (keep.length < ROWS) keep.unshift(Array(COLS).fill(0))
  return keep
}

export const gravityMs = (level) => Math.max(25, 800 * Math.pow(0.84, level - 1))

const CSS = `
.g-blocks { flex: 1; min-height: 0; display: grid; place-items: center; padding: 8px; }
.g-blocks canvas { display: block; touch-action: none; }
.g-blocks-help table { border-collapse: collapse; width: 100%; margin-top: 6px; }
.g-blocks-help td { padding: 3px 6px; border-bottom: 1px solid var(--border); vertical-align: top; }
.g-blocks-help td:first-child { white-space: nowrap; }
.g-blocks-help p { margin-bottom: 6px; }
`

function validSave(st) {
  return st && Array.isArray(st.board) && st.board.length === ROWS && Array.isArray(st.queue) && typeof st.score === 'number'
}

export default {
  async mount(root, ctx) {
    const { h } = ctx
    const prefs = Object.assign({ startLevel: 1 }, await ctx.prefs.get().catch(() => ({})))

    const canvas = h('canvas')
    const g = canvas.getContext('2d')
    const wrap = h('div', { class: 'g-blocks' }, canvas)
    root.append(h('style', null, CSS), wrap)

    let s = null
    let phase = 'idle'
    let cd = 0, acc = 0
    let fallAcc = 0, softAcc = 0
    let das = null
    let clearing = null
    let popups = []
    let dying = 0
    let closeOv = null, overAt = 0
    let saveTimer = 0
    let c = 24, W = 0, H = 0, wellX = 0, wellY = 0
    let best = 0

    const pills = { score: h('span', { class: 'pill' }), lines: h('span', { class: 'pill' }), level: h('span', { class: 'pill' }), best: h('span', { class: 'pill', title: 'Record' }) }
    ctx.setStatus(pills.score, pills.lines, pills.level, pills.best)
    const updateStatus = () => {
      pills.score.textContent = `Score ${ctx.format.int(s ? s.score : 0)}`
      pills.lines.textContent = `Lignes ${s ? s.lines : 0}`
      pills.level.textContent = `Niveau ${s ? s.level : prefs.startLevel}`
      pills.best.textContent = `★ ${ctx.format.int(Math.max(best, s ? s.score : 0))}`
    }
    ctx.scores.list('marathon', { limit: 1 }).then((r) => { best = r[0]?.score || 0; updateStatus() }).catch(() => {})

    const levelSel = h('select', { title: 'Niveau de départ', onchange: () => { prefs.startLevel = +levelSel.value; ctx.prefs.set(prefs); updateStatus() } },
      Array.from({ length: 15 }, (_, i) => h('option', { value: i + 1, selected: i + 1 === prefs.startLevel }, `Départ niv. ${i + 1}`)))
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => askNewGame() }, 'Nouvelle partie'),
      levelSel,
      h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => withPause(() => showScores()) }, '🏆'),
      h('button', { class: 'icon', title: 'Statistiques', onclick: () => withPause(() => showStats()) }, '📊'),
      h('button', { class: 'icon', title: 'Aide', onclick: () => withPause(() => showHelp()) }, '❔'),
    )

    const kbd = (t) => h('span', { class: 'kbd' }, t)
    const showScores = (highlightId = null) => ctx.showScores('marathon', { highlightId, title: 'Meilleurs scores — Blocs', fmt: (v, m) => `${ctx.format.int(v)}${m?.lines != null ? ` · ${m.lines} l.` : ''}` })
    const showHelp = () => ctx.dialog({
      title: 'Blocs — aide',
      wide: true,
      body: h('div', { class: 'g-blocks-help' },
        h('p', null, 'Des pièces de quatre cellules tombent dans le puits. Complétez des lignes horizontales pour les effacer. La partie s’arrête quand une nouvelle pièce ne peut plus apparaître.'),
        h('p', null, `Points : 1 ligne = 100, 2 = 300, 3 = 500, 4 = 800, multipliés par le niveau ; +1 par case en chute douce, +2 en chute instantanée. Niveau supérieur toutes les 10 lignes (la chute accélère).`),
        h('table', null, h('tbody', null,
          h('tr', null, h('td', null, h('b', null, 'Action')), h('td', null, h('b', null, 'Clavier')), h('td', null, h('b', null, 'Manette'))),
          h('tr', null, h('td', null, 'Déplacer'), h('td', null, kbd('←'), ' ', kbd('→'), ' (', kbd('Q'), kbd('D'), ' / ', kbd('A'), kbd('D'), ') — maintenir pour répéter'), h('td', null, 'Croix / stick gauche-droite')),
          h('tr', null, h('td', null, 'Chute douce'), h('td', null, kbd('↓'), ' / ', kbd('S')), h('td', null, 'Bas')),
          h('tr', null, h('td', null, 'Chute instantanée'), h('td', null, kbd('Espace'), ' / ', kbd('Entrée')), h('td', null, 'Haut')),
          h('tr', null, h('td', null, 'Rotation horaire'), h('td', null, kbd('↑'), ' / ', kbd('X'), ' (', kbd('Z'), ' en AZERTY, ', kbd('W'), ' en QWERTY)'), h('td', null, 'A')),
          h('tr', null, h('td', null, 'Rotation anti-horaire'), h('td', null, kbd('Ctrl'), ' / ', kbd('V'), ' / ', kbd('Retour arrière'), ' (', kbd('W'), ' en AZERTY, ', kbd('Z'), ' en QWERTY)'), h('td', null, 'B ou Y')),
          h('tr', null, h('td', null, 'Réserve'), h('td', null, kbd('C'), ' / ', kbd('Maj')), h('td', null, 'X, LB ou RB')),
          h('tr', null, h('td', null, 'Pause'), h('td', null, kbd('P'), ' / ', kbd('Échap')), h('td', null, 'Start')))),
        h('p', { style: { marginTop: '8px' } }, 'La réserve met la pièce courante de côté (une fois par pièce). La pièce fantôme montre où la pièce va se poser ; une pièce posée peut encore glisser ou tourner pendant un court délai.')),
    })
    async function showStats() {
      const st = await ctx.stats.get().catch(() => ({}))
      await ctx.dialog({
        title: 'Statistiques — Blocs',
        body: h('div', { class: 'form' },
          h('span', { class: 'muted' }, 'Parties'), h('span', null, ctx.format.int(st.played || 0)),
          h('span', { class: 'muted' }, 'Meilleur score'), h('span', null, ctx.format.int(st.best_score || 0)),
          h('span', { class: 'muted' }, 'Plus de lignes'), h('span', null, ctx.format.int(st.best_lines || 0)),
          h('span', { class: 'muted' }, 'Niveau max'), h('span', null, ctx.format.int(st.best_level || 0)),
          h('span', { class: 'muted' }, 'Lignes au total'), h('span', null, ctx.format.int(st.total_lines || 0)),
          h('span', { class: 'muted' }, 'Quadruples'), h('span', null, ctx.format.int(st.quads || 0))),
      })
    }

    async function withPause(fn) {
      const wasRunning = phase === 'play' || phase === 'countdown'
      if (wasRunning) { phase = 'paused'; saveNow() }
      await fn()
      if (wasRunning) showPause()
    }

    function layout() {
      const aw = Math.max(240, wrap.clientWidth - 16), ah = Math.max(200, wrap.clientHeight - 16)
      c = Math.max(10, Math.floor(Math.min(aw / 22, ah / 21)))
      W = Math.round(c * 22); H = Math.round(c * 21)
      wellX = Math.round(c * 6); wellY = Math.round(c * 0.5)
      const d = window.devicePixelRatio || 1
      canvas.style.width = `${W}px`; canvas.style.height = `${H}px`
      canvas.width = Math.round(W * d); canvas.height = Math.round(H * d)
      g.setTransform(d, 0, 0, d, 0, 0)
      draw()
    }
    const ro = new ResizeObserver(layout)
    ro.observe(wrap)
    ctx.onCleanup(() => ro.disconnect())

    function saveNow() {
      clearTimeout(saveTimer)
      if (!s || phase === 'over' || phase === 'dying' || phase === 'idle') return
      const st = JSON.parse(JSON.stringify(s))
      if (clearing) { st.board = removeRows(st.board, clearing.rows); st.cur = null }
      ctx.save.set(st)
    }
    const saveSoon = () => { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 500) }
    ctx.onCleanup(() => clearTimeout(saveTimer))

    const closeOverlay = () => { closeOv?.(); closeOv = null }

    function refillQueue() {
      while (s.queue.length < 7) s.queue.push(...ctx.shuffle([0, 1, 2, 3, 4, 5, 6]))
    }

    function spawn(t = null) {
      refillQueue()
      const type = t ?? s.queue.shift()
      refillQueue()
      s.cur = spawnPiece(type)
      s.lockT = 0; s.lockResets = 0; s.lowest = s.cur.y
      fallAcc = 0
      if (collides(s.board, s.cur)) {
        if (!collides(s.board, s.cur, 0, -1)) s.cur.y -= 1
        else { die(); return false }
      }
      return true
    }

    function newGame() {
      closeOverlay()
      s = { board: emptyBoard(), queue: [], cur: null, hold: null, canHold: true, score: 0, lines: 0,
        startLevel: prefs.startLevel, level: prefs.startLevel, time: 0, lockT: 0, lockResets: 0, lowest: 0, quads: 0 }
      clearing = null; popups = []; das = null
      spawn()
      updateStatus()
      startCountdown()
      saveNow()
    }

    async function askNewGame() {
      const running = s && (phase === 'play' || phase === 'countdown' || phase === 'paused')
      if (running && s.score > 0) {
        phase = 'paused'
        saveNow()
        closeOverlay()
        const ok = await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Nouvelle partie', danger: true })
        if (!ok) { showPause(); return }
      }
      newGame()
    }

    function startCountdown() {
      closeOverlay()
      phase = 'countdown'; cd = 3000; acc = 0
      ctx.sfx('tick')
    }

    function showPause(text = 'Partie en pause.') {
      phase = 'paused'
      closeOv = ctx.overlay({
        title: 'Pause',
        text: `${text}\nScore ${ctx.format.int(s.score)} · ${s.lines} lignes · niveau ${s.level}`,
        buttons: [
          { label: 'Reprendre', primary: true, onClick: startCountdown },
          { label: 'Nouvelle partie', onClick: () => askNewGame() },
        ],
      })
    }

    function pauseGame() {
      if (phase === 'play' || phase === 'countdown') { saveNow(); showPause() }
    }

    function onGround() { return s.cur && collides(s.board, s.cur, 0, 1) }

    function afterMove() {
      if (!s.cur) return
      if (s.cur.y > s.lowest) { s.lowest = s.cur.y; s.lockResets = 0 }
      if (onGround() && s.lockResets < MAX_LOCK_RESETS) { s.lockT = 0; s.lockResets++ }
    }

    function move(dx) {
      if (!s.cur || collides(s.board, s.cur, dx, 0)) return false
      s.cur.x += dx
      afterMove()
      ctx.sfx('move')
      return true
    }

    function rotate(dir) {
      if (!s.cur) return
      if (tryRotate(s.board, s.cur, dir)) { afterMove(); ctx.sfx('rotate') }
    }

    function softStep() {
      if (!s.cur || collides(s.board, s.cur, 0, 1)) return false
      s.cur.y++
      s.score++
      afterMove()
      return true
    }

    function hardDrop() {
      if (!s.cur) return
      let n = 0
      while (!collides(s.board, s.cur, 0, 1)) { s.cur.y++; n++ }
      s.score += n * 2
      ctx.sfx('drop')
      lock()
    }

    function doHold() {
      if (!s.cur || !s.canHold) { if (s.cur) ctx.sfx('error'); return }
      const t = s.cur.t
      s.canHold = false
      ctx.sfx('flag')
      if (s.hold == null) { s.hold = t; spawn() } else { const ht = s.hold; s.hold = t; spawn(ht) }
    }

    function lock() {
      const p = s.cur
      let allHidden = true
      for (const [x, y] of cellsOf(p)) {
        if (y >= 0) s.board[y][x] = p.t + 1
        if (y >= HIDDEN) allHidden = false
      }
      s.cur = null
      s.canHold = true
      if (allHidden) { die(); return }
      const rows = fullRows(s.board)
      if (rows.length) {
        clearing = { rows, t: 0 }
        const n = rows.length
        const pts = LINE_POINTS[n] * s.level
        s.score += pts
        s.lines += n
        if (n === 4) s.quads = (s.quads || 0) + 1
        const lvl = s.startLevel + Math.floor(s.lines / 10)
        popups.push({ text: `${LINE_NAMES[n]}  +${ctx.format.int(pts)}`, t: 1100, y: rows[0], big: n === 4 })
        if (lvl > s.level) {
          s.level = lvl
          popups.push({ text: `Niveau ${lvl}`, t: 1500, y: rows[0] - 3, big: true, level: true })
          setTimeout(() => ctx.sfx('win'), 150)
        }
        ctx.sfx(n === 4 ? 'boom' : 'line')
      } else {
        ctx.sfx('hit')
        spawn()
      }
      updateStatus()
      saveSoon()
    }

    function die() {
      s.cur = null
      phase = 'dying'
      dying = 0
      clearTimeout(saveTimer)
      ctx.sfx('lose')
    }

    async function gameOver() {
      phase = 'over'
      overAt = performance.now()
      ctx.save.clear()
      const { score, lines, level } = s
      let r = null
      try { r = await ctx.scores.submit('marathon', score, { meta: { lines, level, start: s.startLevel } }) } catch (e) { ctx.toast(e.message, 'err') }
      const st = await ctx.stats.update({ inc: { played: 1, total_lines: lines, quads: s.quads || 0 }, max: { best_score: score, best_lines: lines, best_level: level } })
      ctx.stats.update({ set: { hub_best: ctx.format.int(st.best_score ?? score) } })
      if (score > best) best = score
      updateStatus()
      if (phase !== 'over') return
      const text = [`Score ${ctx.format.int(score)} · ${lines} lignes · niveau ${level}`]
      if (r?.record && score > 0) text.push('Nouveau record !')
      else if (r?.rank) text.push(`Rang ${r.rank} sur ${r.total}`)
      const show = () => {
        closeOv = ctx.overlay({
          title: 'Partie terminée', text: text.join('\n'),
          buttons: [
            { label: 'Rejouer', primary: true, onClick: newGame },
            { label: '🏆 Scores', onClick: () => showScores(r?.id).then(() => { if (phase === 'over') show() }) },
          ],
        })
      }
      show()
    }

    function update(dt) {
      for (const p of popups) p.t -= dt
      popups = popups.filter((p) => p.t > 0)
      if (phase === 'countdown') {
        const before = Math.ceil(cd / 1000)
        cd -= dt
        if (cd <= 0) { phase = 'play'; fallAcc = 0; ctx.sfx('click') } else if (Math.ceil(cd / 1000) !== before) ctx.sfx('tick')
        return
      }
      if (phase === 'dying') {
        dying += dt
        if (dying >= 900) gameOver()
        return
      }
      if (phase !== 'play') return
      s.time += dt
      if (clearing) {
        clearing.t += dt
        if (clearing.t >= CLEAR_MS) {
          s.board = removeRows(s.board, clearing.rows)
          clearing = null
          spawn()
        }
        return
      }
      if (!s.cur) return

      if (das) {
        const name = das.dir < 0 ? 'left' : 'right'
        if (ctx.isHeld(name)) {
          das.t += dt
          if (das.t >= DAS) {
            das.arr += dt
            while (das.arr >= ARR) { das.arr -= ARR; if (!move(das.dir)) { das.arr = 0; break } }
          }
        } else {
          const other = das.dir < 0 ? 'right' : 'left'
          das = ctx.isHeld(other) ? { dir: -das.dir, t: 0, arr: 0 } : null
        }
      }

      const soft = ctx.isHeld('down')
      const grav = gravityMs(s.level)
      if (soft && grav > SOFT_MS) {
        softAcc += dt
        while (softAcc >= SOFT_MS) { softAcc -= SOFT_MS; if (!softStep()) { softAcc = 0; break } }
        fallAcc = 0
      } else {
        softAcc = 0
        fallAcc += dt
        while (fallAcc >= grav) {
          fallAcc -= grav
          if (collides(s.board, s.cur, 0, 1)) { fallAcc = 0; break }
          s.cur.y++
          afterMove()
        }
      }
      if (onGround()) {
        s.lockT += dt
        if (s.lockT >= LOCK_MS) lock()
      } else s.lockT = 0
    }

    function block(x, y, size, color, alpha = 1) {
      const r = Math.max(2, size * 0.18)
      g.globalAlpha = alpha
      const gr = g.createLinearGradient(x, y, x + size, y + size)
      gr.addColorStop(0, shade(color, 0.28)); gr.addColorStop(1, shade(color, -0.22))
      g.fillStyle = gr
      g.beginPath(); g.roundRect ? g.roundRect(x + 1, y + 1, size - 2, size - 2, r) : g.rect(x + 1, y + 1, size - 2, size - 2); g.fill()
      g.fillStyle = 'rgba(255,255,255,0.22)'
      g.beginPath(); g.roundRect ? g.roundRect(x + size * 0.22, y + size * 0.2, size * 0.56, size * 0.16, size * 0.08) : g.rect(x + size * 0.22, y + size * 0.2, size * 0.56, size * 0.16); g.fill()
      g.globalAlpha = 1
    }

    function miniPiece(t, cx, cy, size, dim = false) {
      const cells = ROT[t][0]
      const xs = cells.map(([x]) => x), ys = cells.map(([, y]) => y)
      const w = Math.max(...xs) - Math.min(...xs) + 1, hh = Math.max(...ys) - Math.min(...ys) + 1
      const ox = cx - (w * size) / 2 - Math.min(...xs) * size, oy = cy - (hh * size) / 2 - Math.min(...ys) * size
      for (const [x, y] of cells) block(ox + x * size, oy + y * size, size, dim ? '#52525b' : COLORS[t])
    }

    function panel(x, y, w, hh, title) {
      g.fillStyle = 'rgba(24,24,32,0.9)'
      g.strokeStyle = 'rgba(255,255,255,0.08)'
      g.lineWidth = 1
      g.beginPath(); g.roundRect ? g.roundRect(x, y, w, hh, c * 0.3) : g.rect(x, y, w, hh); g.fill(); g.stroke()
      g.fillStyle = '#8b8b98'
      g.font = `600 ${Math.round(c * 0.5)}px system-ui, sans-serif`
      g.textAlign = 'left'; g.textBaseline = 'top'
      g.fillText(title, x + c * 0.4, y + c * 0.3)
    }

    function draw() {
      if (!W) return
      g.clearRect(0, 0, W, H)
      g.save()
      const wx = wellX, wy = wellY, ww = c * COLS, wh = c * (ROWS - HIDDEN)
      const bg = g.createLinearGradient(0, wy, 0, wy + wh)
      bg.addColorStop(0, '#141428'); bg.addColorStop(1, '#0c0c18')
      g.fillStyle = bg
      g.fillRect(wx, wy, ww, wh)
      g.strokeStyle = 'rgba(255,255,255,0.04)'
      g.lineWidth = 1
      for (let x = 1; x < COLS; x++) { g.beginPath(); g.moveTo(wx + x * c + 0.5, wy); g.lineTo(wx + x * c + 0.5, wy + wh); g.stroke() }
      for (let y = 1; y < ROWS - HIDDEN; y++) { g.beginPath(); g.moveTo(wx, wy + y * c + 0.5); g.lineTo(wx + ww, wy + y * c + 0.5); g.stroke() }
      g.strokeStyle = 'rgba(129,140,248,0.45)'
      g.lineWidth = 2
      g.strokeRect(wx - 1, wy - 1, ww + 2, wh + 2)

      if (s) {
        g.save()
        g.beginPath(); g.rect(wx, wy, ww, wh); g.clip()
        const clr = clearing ? clearing.t / CLEAR_MS : 0
        const deadRows = phase === 'dying' ? Math.floor((dying / 800) * (ROWS - HIDDEN)) : phase === 'over' ? ROWS : 0
        for (let y = HIDDEN; y < ROWS; y++) {
          const isClr = clearing && clearing.rows.includes(y)
          const grey = ROWS - 1 - y < deadRows
          for (let x = 0; x < COLS; x++) {
            const v = s.board[y][x]
            if (!v) continue
            const px = wx + x * c, py = wy + (y - HIDDEN) * c
            if (isClr) {
              const k = 1 - clr
              const sz = c * k
              block(px + (c - sz) / 2, py + (c - sz) / 2, Math.max(1, sz), COLORS[v - 1])
            } else block(px, py, c, grey ? '#3f3f46' : COLORS[v - 1])
          }
          if (isClr) {
            g.fillStyle = `rgba(255,255,255,${0.7 * (1 - clr)})`
            g.fillRect(wx, wy + (y - HIDDEN) * c, ww, c)
          }
        }
        if (s.cur && (phase === 'play' || phase === 'countdown' || phase === 'paused')) {
          let gy = 0
          while (!collides(s.board, s.cur, 0, gy + 1)) gy++
          const col = COLORS[s.cur.t]
          if (gy > 0) {
            for (const [x, y] of cellsOf(s.cur, 0, gy)) {
              if (y < HIDDEN) continue
              const px = wx + x * c, py = wy + (y - HIDDEN) * c
              g.strokeStyle = col; g.globalAlpha = 0.55; g.lineWidth = 2
              g.strokeRect(px + 2.5, py + 2.5, c - 5, c - 5)
              g.fillStyle = col; g.globalAlpha = 0.1
              g.fillRect(px + 2, py + 2, c - 4, c - 4)
              g.globalAlpha = 1
            }
          }
          const lockFade = onGround() ? 1 - Math.min(0.35, (s.lockT / LOCK_MS) * 0.35) : 1
          for (const [x, y] of cellsOf(s.cur)) {
            if (y < HIDDEN) continue
            block(wx + x * c, wy + (y - HIDDEN) * c, c, col, lockFade)
          }
        }
        g.restore()
      }

      const pw = c * 4.8
      const lx = c * 0.6
      panel(lx, wy, pw, c * 4, 'RÉSERVE')
      if (s && s.hold != null) miniPiece(s.hold, lx + pw / 2, wy + c * 2.35, c * 0.85, !s.canHold)
      panel(lx, wy + c * 4.5, pw, c * 7.2, 'PARTIE')
      const rows = [
        ['Score', ctx.format.int(s ? s.score : 0)],
        ['Niveau', String(s ? s.level : prefs.startLevel)],
        ['Lignes', String(s ? s.lines : 0)],
        ['Temps', s ? ctx.format.time(s.time).replace(/\.\d$/, '') : '0:00'],
      ]
      rows.forEach(([k, v], i) => {
        const y = wy + c * (5.5 + i * 1.5)
        g.fillStyle = '#71717a'; g.font = `${Math.round(c * 0.45)}px system-ui, sans-serif`
        g.textAlign = 'left'; g.textBaseline = 'top'
        g.fillText(k, lx + c * 0.4, y)
        g.fillStyle = '#e4e4e7'; g.font = `700 ${Math.round(c * 0.62)}px system-ui, sans-serif`
        g.fillText(v, lx + c * 0.4, y + c * 0.5)
      })
      const rx = wx + c * COLS + c * 0.6
      panel(rx, wy, pw, c * 10, 'SUIVANTES')
      if (s) s.queue.slice(0, 3).forEach((t, i) => miniPiece(t, rx + pw / 2, wy + c * (2.4 + i * 2.75), c * (i === 0 ? 0.85 : 0.7)))

      for (const p of popups) {
        const a = Math.min(1, p.t / 300)
        g.globalAlpha = a
        g.textAlign = 'center'; g.textBaseline = 'middle'
        g.font = `800 ${Math.round(c * (p.big ? 0.95 : 0.7))}px system-ui, sans-serif`
        g.fillStyle = p.level ? '#a5b4fc' : p.big ? '#fde68a' : '#f4f4f5'
        g.strokeStyle = 'rgba(0,0,0,0.6)'; g.lineWidth = 3
        const y = wy + (Math.max(HIDDEN + 2, p.y) - HIDDEN) * c - (1100 - p.t) * c * 0.0015
        g.strokeText(p.text, wx + ww / 2, y)
        g.fillText(p.text, wx + ww / 2, y)
        g.globalAlpha = 1
      }

      if (phase === 'countdown') {
        g.fillStyle = 'rgba(8,8,16,0.55)'
        g.fillRect(wx, wy, ww, wh)
        const n = Math.ceil(cd / 1000), f = (cd % 1000) / 1000
        g.textAlign = 'center'; g.textBaseline = 'middle'
        g.font = `800 ${Math.round(c * (2.6 + f * 1.4))}px system-ui, sans-serif`
        g.globalAlpha = 0.35 + 0.65 * f
        g.fillStyle = '#eef2ff'
        g.fillText(String(n), wx + ww / 2, wy + wh / 2)
        g.globalAlpha = 1
      }
      g.restore()
    }

    ctx.loop((dt) => {
      acc += dt
      while (acc >= STEP) { update(STEP); acc -= STEP }
      draw()
    })

    const canAct = () => phase === 'play' && s && s.cur

    ctx.onAction((action, ev) => {
      if (action === 'start') {
        if (phase === 'play' || phase === 'countdown') pauseGame()
        else if (phase === 'paused') startCountdown()
        return
      }
      if (ev.repeat) return
      if (phase === 'paused' && action === 'a') { startCountdown(); return }
      if (phase === 'over' && action === 'a' && performance.now() - overAt > 800) { newGame(); return }
      if (phase === 'idle' && action === 'a') { newGame(); return }
      if (!canAct()) return
      const pad = ev.source === 'pad'
      switch (action) {
        case 'left': case 'right': {
          const d = action === 'left' ? -1 : 1
          move(d)
          das = { dir: d, t: 0, arr: 0 }
          break
        }
        case 'down': softStep(); softAcc = 0; break
        case 'up': if (pad) hardDrop(); else rotate(1); break
        case 'a': if (pad) rotate(1); else hardDrop(); break
        case 'b': if (pad || ev.code === 'Backspace') rotate(-1); else rotate(1); break
        case 'y': rotate(-1); break
        case 'x': case 'select': case 'l': case 'r': doHold(); break
      }
    })

    const onKey = (e) => {
      if (e.repeat || document.querySelector('.dlg-back')) return
      const t = e.target
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (e.code === 'ControlLeft' || e.code === 'ControlRight' || e.code === 'KeyZ') {
        if (!canAct()) return
        e.preventDefault()
        rotate(-1)
      }
    }
    document.addEventListener('keydown', onKey)
    ctx.onCleanup(() => document.removeEventListener('keydown', onKey))

    canvas.addEventListener('click', () => { if (phase === 'idle') newGame() })

    const saved = await ctx.save.get().catch(() => null)
    if (validSave(saved)) {
      s = saved
      s.quads ??= 0
      if (!s.cur) spawn()
      layout()
      updateStatus()
      showPause('Partie précédente retrouvée.')
    } else {
      layout()
      updateStatus()
      closeOv = ctx.overlay({
        title: 'Blocs',
        text: 'Complétez des lignes avec les pièces qui tombent.\nNiveau de départ réglable dans la barre.',
        buttons: [{ label: 'Jouer', primary: true, onClick: newGame }],
      })
    }

    return {
      pause() { pauseGame() },
      destroy() { saveNow() },
    }
  },
}

function shade(hex, k) {
  const n = parseInt(hex.slice(1), 16)
  let r = n >> 16, gg = (n >> 8) & 255, b = n & 255
  if (k > 0) { r += (255 - r) * k; gg += (255 - gg) * k; b += (255 - b) * k } else { r *= 1 + k; gg *= 1 + k; b *= 1 + k }
  return `rgb(${r | 0},${gg | 0},${b | 0})`
}
