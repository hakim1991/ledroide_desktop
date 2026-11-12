export const FW = 520, FH = 600
const TOP = 36
export const GC = 13, GR = 14
export const BW = FW / GC, BH = 18, BY0 = 72
const R = 6
const PY = FH - 42, PH = 12
const PW = 84, PW_WIDE = 132
const STEP = 1000 / 120
const MAX_SPEED = 600
const MAX_BALLS = 8
const LIFE_EVERY = 5000
const KEY_SPEED = 620
const FX_MS = { wide: 15000, slow: 10000 }

const CAPS = {
  wide: { label: 'L', color: '#4ade80', name: 'Raquette large' },
  multi: { label: 'M', color: '#60a5fa', name: 'Multi-balle' },
  slow: { label: 'S', color: '#fbbf24', name: 'Balle lente' },
  life: { label: '+', color: '#f472b6', name: 'Vie' },
}
const CAP_BAG = ['wide', 'wide', 'wide', 'multi', 'multi', 'multi', 'slow', 'slow', 'slow', 'life']

const BRICK = {
  1: ['#2dd4bf', '#0f766e'],
  2: ['#818cf8', '#4338ca'],
  3: ['#f472b6', '#be185d'],
  9: ['#a1a1aa', '#52525b'],
}

export const LEVELS = [
  { name: 'Échauffement', rows: [
    '.............',
    '.............',
    '1111111111111',
    '1111111111111',
    '1111111111111',
    '1111111111111',
  ] },
  { name: 'Pyramide', rows: [
    '......2......',
    '.....212.....',
    '....21112....',
    '...2111112...',
    '..211111112..',
    '.21111111112.',
    '2111111111112',
  ] },
  { name: 'Damier', rows: [
    '2.2.2.2.2.2.2',
    '.1.1.1.1.1.1.',
    '2.2.2.2.2.2.2',
    '.1.1.1.1.1.1.',
    '2.2.2.2.2.2.2',
    '.1.1.1.1.1.1.',
    '2.2.2.2.2.2.2',
  ] },
  { name: 'Rayures', rows: [
    '3333333333333',
    '.............',
    '2222222222222',
    '.............',
    '1111111111111',
    '.............',
    '#.#.#.#.#.#.#',
  ] },
  { name: 'Cœur', rows: [
    '..222...222..',
    '.23332.23332.',
    '2333332333332',
    '2333333333332',
    '.23333333332.',
    '..233333332..',
    '...2333332...',
    '....23332....',
    '.....232.....',
    '......2......',
  ] },
  { name: 'Remparts', rows: [
    '3333333333333',
    '2...........2',
    '2.111111111.2',
    '2.1.......1.2',
    '2.1.33333.1.2',
    '2.111111111.2',
    '2...........2',
    '##...###...##',
  ] },
  { name: 'Diamant', rows: [
    '......3......',
    '.....323.....',
    '....32123....',
    '...3211123...',
    '..321111123..',
    '...3211123...',
    '....32123....',
    '.....323.....',
    '......3......',
  ] },
  { name: 'Cellules', rows: [
    '3333333333333',
    '#...#...#...#',
    '#.2.#.2.#.2.#',
    '#.2.#.2.#.2.#',
    '#...#...#...#',
    '1111111111111',
    '1111111111111',
  ] },
  { name: 'Bastion', rows: [
    '#3#3#3#3#3#3#',
    '3333333333333',
    '2222222222222',
    '##.#######.##',
    '1111111111111',
    '1212121212121',
  ] },
  { name: 'Citadelle', rows: [
    '2333333333332',
    '3###.###.###3',
    '3222222222223',
    '3211121211123',
    '3211111111123',
    '3222212122223',
    '.............',
    '#...#...#...#',
  ] },
]

export function parseRows(rows) {
  const kind = [], hp = []
  for (let r = 0; r < GR; r++) {
    const line = rows[r] || ''
    kind.push([]); hp.push([])
    for (let c = 0; c < GC; c++) {
      const ch = line[c] || '.'
      const k = ch === '#' ? 9 : '123'.includes(ch) ? +ch : 0
      kind[r].push(k); hp[r].push(k === 9 ? -1 : k)
    }
  }
  return { kind, hp }
}

export const countLeft = (hp) => hp.reduce((n, row) => n + row.filter((v) => v > 0).length, 0)
const clamp = (v, a, b) => Math.max(a, Math.min(b, v))

function fixAngle(b) {
  const MIN = 0.22
  if (Math.abs(b.dy) < MIN) {
    b.dy = (b.dy < 0 ? -1 : 1) * MIN
    b.dx = (b.dx < 0 ? -1 : 1) * Math.sqrt(1 - MIN * MIN)
  }
  const n = Math.hypot(b.dx, b.dy) || 1
  b.dx /= n; b.dy /= n
}

// Moves one ball by `dist` px in sub-steps of <= 3 px; stops at the first brick contact so a brick is never hit twice per call.
export function moveBall(b, dist, hp, paddle) {
  const out = { lost: false, hits: [], wall: false, paddle: false }
  const n = Math.max(1, Math.ceil(dist / 3))
  const d = dist / n
  for (let i = 0; i < n; i++) {
    const ox = b.x, oy = b.y
    b.x += b.dx * d; b.y += b.dy * d
    if (b.x < R) { b.x = R; b.dx = Math.abs(b.dx); out.wall = true }
    if (b.x > FW - R) { b.x = FW - R; b.dx = -Math.abs(b.dx); out.wall = true }
    if (b.y < TOP + R) { b.y = TOP + R; b.dy = Math.abs(b.dy); out.wall = true; fixAngle(b) }
    if (b.dy > 0 && b.y + R >= PY && oy + R <= PY + PH * 0.6 && b.y - R <= PY + PH &&
        b.x >= paddle.x - paddle.w / 2 - R && b.x <= paddle.x + paddle.w / 2 + R) {
      const hit = clamp((b.x - paddle.x) / (paddle.w / 2), -1, 1)
      const ang = hit * (62 * Math.PI / 180)
      b.dx = Math.sin(ang); b.dy = -Math.cos(ang)
      b.y = PY - R
      out.paddle = true
    }
    const c0 = Math.floor((b.x - R) / BW), c1 = Math.floor((b.x + R) / BW)
    const r0 = Math.floor((b.y - R - BY0) / BH), r1 = Math.floor((b.y + R - BY0) / BH)
    let flipX = false, flipY = false
    for (let r = Math.max(0, r0); r <= Math.min(GR - 1, r1); r++) {
      for (let c = Math.max(0, c0); c <= Math.min(GC - 1, c1); c++) {
        if (!hp[r][c]) continue
        const bx = c * BW, by = BY0 + r * BH
        const cx = clamp(b.x, bx, bx + BW), cy = clamp(b.y, by, by + BH)
        let nx = b.x - cx, ny = b.y - cy
        const dd = nx * nx + ny * ny
        if (dd >= R * R) continue
        if (dd === 0) { nx = ox - (bx + BW / 2); ny = (oy - (by + BH / 2)) * (BW / BH) }
        const dist0 = Math.sqrt(dd)
        if (Math.abs(nx) > Math.abs(ny)) {
          if (b.dx * nx < 0) flipX = true
          if (dd > 0) b.x = cx + (nx / dist0) * R
        } else {
          if (b.dy * ny < 0) flipY = true
          if (dd > 0) b.y = cy + (ny / dist0) * R
        }
        if (out.hits.length < 3) out.hits.push([r, c])
      }
    }
    if (flipX) b.dx = -b.dx
    if (flipY) b.dy = -b.dy
    if (flipX || flipY) fixAngle(b)
    if (out.hits.length) break
    if (b.y - R > FH) { out.lost = true; break }
  }
  return out
}

const CSS = `
.g-breakout { flex: 1; min-height: 0; display: grid; place-items: center; padding: 8px; }
.g-breakout canvas { display: block; border-radius: 10px; box-shadow: 0 10px 40px rgba(0,0,0,.45); touch-action: none; }
.g-breakout canvas.play { cursor: none; }
.g-breakout-ed { flex: 1; min-height: 0; display: flex; gap: 12px; padding: 10px; }
.g-breakout-ed .ed-area { flex: 1; min-width: 0; min-height: 0; display: grid; place-items: center; }
.g-breakout-ed .ed-area canvas { display: block; border-radius: 10px; cursor: crosshair; touch-action: none; box-shadow: 0 8px 30px rgba(0,0,0,.4); }
.g-breakout-ed .ed-side { width: 230px; flex-shrink: 0; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; }
.g-breakout-ed .ed-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px; display: flex; flex-direction: column; gap: 7px; }
.g-breakout-ed .ed-box h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); font-weight: 600; }
.g-breakout-ed .brushes { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; }
.g-breakout-ed .brushes button { padding: 4px 0; display: flex; flex-direction: column; align-items: center; gap: 2px; font-size: 11px; }
.g-breakout-ed .sw { width: 26px; height: 11px; border-radius: 3px; border: 1px solid rgba(255,255,255,.15); }
.g-breakout-ed .row { display: flex; gap: 6px; flex-wrap: wrap; }
.g-breakout-ed .row > * { flex: 1; }
.g-breakout-ed input, .g-breakout-ed select { width: 100%; }
.g-breakout-ed .hint { color: var(--muted); font-size: 12px; line-height: 1.4; }
.g-breakout-help ul { margin: 4px 0 8px 18px; } .g-breakout-help li { margin: 2px 0; }
.g-breakout-help .cap { display: inline-block; width: 18px; text-align: center; border-radius: 5px; color: #111; font-weight: 700; font-size: 11px; margin-right: 4px; }
`

function validSave(st) {
  return st && Array.isArray(st.hp) && st.hp.length === GR && Array.isArray(st.balls) && typeof st.score === 'number' && st.source !== 'test'
}

export default {
  async mount(root, ctx) {
    const { h } = ctx
    const prefs = Object.assign({ pointerLock: false, custom: [] }, await ctx.prefs.get().catch(() => ({})))
    if (!Array.isArray(prefs.custom)) prefs.custom = []
    const savePrefs = () => ctx.prefs.set(prefs)

    const canvas = h('canvas')
    const g = canvas.getContext('2d')
    const gameWrap = h('div', { class: 'g-breakout' }, canvas)
    root.append(h('style', null, CSS))

    let scene = 'game'
    let s = null
    let phase = 'idle'
    let cd = 0, acc = 0, clearT = 0, dieT = 0
    let k = 1, W = 0, H = 0
    let closeOv = null, overAt = 0
    let saveTimer = 0
    let best = 0
    let keyMode = false
    let particles = [], trails = new Map(), flashes = []
    let banner = null

    const pills = { lvl: h('span', { class: 'pill' }), lives: h('span', { class: 'pill' }), score: h('span', { class: 'pill' }), best: h('span', { class: 'pill', title: 'Record (campagne)' }) }
    const updateStatus = () => {
      if (scene !== 'game') return
      pills.lvl.textContent = s ? (s.source === 'campaign' ? `Niveau ${s.level + 1}/${LEVELS.length}` : s.source === 'test' ? 'Test' : s.name) : `Niveau 1/${LEVELS.length}`
      pills.lives.textContent = `❤ ${s ? s.lives : 3}`
      pills.score.textContent = `Score ${ctx.format.int(s ? s.score : 0)}`
      pills.best.textContent = `★ ${ctx.format.int(Math.max(best, s && s.source === 'campaign' ? s.score : 0))}`
    }
    ctx.scores.list('campaign', { limit: 1 }).then((r) => { best = r[0]?.score || 0; updateStatus() }).catch(() => {})

    // ── Toolbar ──
    const sourceSel = h('select', { title: 'Niveaux', onchange: () => {
      const v = sourceSel.value
      askNew(v === 'campaign' ? { source: 'campaign' } : { source: 'custom', id: v })
    } })
    const fillSourceSel = (value) => {
      sourceSel.replaceChildren(h('option', { value: 'campaign' }, 'Campagne (10 niveaux)'),
        ...prefs.custom.map((l) => h('option', { value: l.id }, `Perso : ${l.name}`)))
      sourceSel.value = value && [...sourceSel.options].some((o) => o.value === value) ? value : 'campaign'
    }
    const lockBtn = h('button', { class: `icon${prefs.pointerLock ? ' on' : ''}`, title: 'Capturer la souris pendant le jeu (clic sur le plateau, Échap pour libérer)', onclick: () => {
      prefs.pointerLock = !prefs.pointerLock
      lockBtn.classList.toggle('on', prefs.pointerLock)
      if (!prefs.pointerLock && document.pointerLockElement) document.exitPointerLock()
      savePrefs()
      ctx.toast(prefs.pointerLock ? 'Capture de la souris activée (cliquez sur le plateau)' : 'Capture de la souris désactivée')
    } }, '🖱️')
    function gameToolbar() {
      const testing = s && s.source === 'test'
      fillSourceSel(s ? (s.source === 'campaign' ? 'campaign' : s.customId) : 'campaign')
      ctx.setToolbar([
        testing ? h('button', { onclick: () => openEditor() }, '← Éditeur') : null,
        h('button', { class: 'primary', onclick: () => askNew(testing ? { source: 'test', rows: s.rows, name: s.name } : sourceSel.value === 'campaign' ? { source: 'campaign' } : { source: 'custom', id: sourceSel.value }) }, testing ? 'Recommencer' : 'Nouvelle partie'),
        testing ? null : sourceSel,
        testing ? null : h('button', { title: 'Éditeur de niveaux', onclick: () => openEditor() }, '✏️ Éditeur'),
        lockBtn,
        h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => withPause(() => showScores()) }, '🏆'),
        h('button', { class: 'icon', title: 'Aide', onclick: () => withPause(() => showHelp()) }, '❔'),
      ].filter(Boolean))
      ctx.setStatus(pills.lvl, pills.lives, pills.score, pills.best)
      updateStatus()
    }

    const showScores = (highlightId = null) => ctx.showScores('campaign', { highlightId, title: 'Meilleurs scores — Casse-briques',
      fmt: (v, m) => `${ctx.format.int(v)}${m?.level ? ` · niv. ${m.level}` : ''}` })
    const showHelp = () => ctx.dialog({
      title: 'Casse-briques — aide', wide: true,
      body: h('div', { class: 'g-breakout-help' },
        h('p', null, 'Renvoyez la balle avec la raquette pour briser toutes les briques. L’angle dépend de l’endroit où la balle touche la raquette : au centre elle repart droite, sur les bords elle part en biais.'),
        h('ul', null,
          h('li', null, 'Briques turquoise : 1 coup · violettes : 2 coups · roses : 3 coups · grises : indestructibles.'),
          h('li', null, `3 vies au départ, une vie supplémentaire tous les ${ctx.format.int(LIFE_EVERY)} points. La balle accélère un peu à chaque renvoi.`)),
        h('p', null, h('b', null, 'Bonus'), ' (attrapez les capsules qui tombent) :'),
        h('ul', null, Object.values(CAPS).map((c) => h('li', null, h('span', { class: 'cap', style: { background: c.color } }, c.label), c.name,
          c.label === 'L' ? ' (15 s)' : c.label === 'S' ? ' (10 s)' : c.label === 'M' ? ' : chaque balle se divise en trois' : ''))),
        h('p', null, h('b', null, 'Commandes'), ' : souris (déplacer), clic pour lancer · ', h('span', { class: 'kbd' }, '←'), h('span', { class: 'kbd' }, '→'),
          ' ou ', h('span', { class: 'kbd' }, 'Q'), h('span', { class: 'kbd' }, 'D'), ' déplacer, ', h('span', { class: 'kbd' }, 'Espace'), ' ou ', h('span', { class: 'kbd' }, '↑'),
          ' lancer, ', h('span', { class: 'kbd' }, 'P'), ' / ', h('span', { class: 'kbd' }, 'Échap'), ' pause. Manette : croix/stick, A lancer, Start pause. ',
          'Le bouton 🖱️ capture la souris pendant le jeu (Échap pour la libérer).'),
        h('p', { style: { marginTop: '8px' } }, h('b', null, 'Éditeur'), ' : peignez les briques sur la grille (clic droit = gomme), testez, enregistrez, puis jouez vos niveaux depuis la liste « Perso ». Les scores ne sont enregistrés que pour la campagne.')),
    })

    async function withPause(fn) {
      const wasRunning = phase === 'play' || phase === 'countdown'
      if (wasRunning) { phase = 'paused'; releaseLock(); saveNow() }
      await fn()
      if (wasRunning) showPause()
    }

    // ── Layout ──
    function layout() {
      if (scene === 'editor') { edLayout(); return }
      const aw = Math.max(200, gameWrap.clientWidth - 16), ah = Math.max(200, gameWrap.clientHeight - 16)
      k = Math.min(aw / FW, ah / FH)
      W = Math.floor(FW * k); H = Math.floor(FH * k)
      const d = window.devicePixelRatio || 1
      canvas.style.width = `${W}px`; canvas.style.height = `${H}px`
      canvas.width = Math.round(W * d); canvas.height = Math.round(H * d)
      g.setTransform(d * k, 0, 0, d * k, 0, 0)
      draw()
    }
    const ro = new ResizeObserver(() => layout())
    ro.observe(root)
    ctx.onCleanup(() => ro.disconnect())

    // ── Save ──
    function saveNow() {
      clearTimeout(saveTimer)
      if (!s || s.source === 'test' || phase === 'over' || phase === 'idle' || phase === 'dying') return
      ctx.save.set(JSON.parse(JSON.stringify(s)))
    }
    const saveSoon = () => { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 500) }
    ctx.onCleanup(() => clearTimeout(saveTimer))

    const closeOverlay = () => { closeOv?.(); closeOv = null }
    const releaseLock = () => { if (document.pointerLockElement) document.exitPointerLock() }

    // ── Game setup ──
    function levelRows(src) {
      if (src.source === 'campaign') return { rows: LEVELS[src.level || 0].rows, name: LEVELS[src.level || 0].name }
      if (src.source === 'test') return { rows: src.rows, name: src.name || 'Test' }
      const l = prefs.custom.find((x) => x.id === (src.id ?? src.customId))
      return l ? { rows: l.rows, name: l.name } : null
    }

    function baseSpeed() { return 300 + (s.source === 'campaign' ? s.level * 12 : 40) }

    function resetBalls() {
      s.balls = [{ x: s.px, y: PY - R, dx: 0, dy: -1, stuck: true, off: 14 }]
      s.caps = []
      s.fx = { wide: 0, slow: 0 }
      s.speed = baseSpeed()
      trails.clear()
    }

    function loadLevel() {
      const lv = levelRows(s)
      const { kind, hp } = parseRows(lv.rows)
      s.kind = kind; s.hp = hp; s.name = lv.name
      s.px = FW / 2; s.pw = PW
      resetBalls()
      particles = []; flashes = []
      banner = { text: s.source === 'campaign' ? `Niveau ${s.level + 1} — ${lv.name}` : lv.name, t: 2600 }
    }

    function newGame(src) {
      const lv = levelRows({ ...src, level: 0 })
      if (!lv) { ctx.toast('Niveau introuvable', 'err'); return }
      closeOverlay()
      s = { source: src.source, customId: src.id || null, rows: src.source === 'test' ? src.rows : null, name: lv.name,
        level: 0, score: 0, lives: 3, nextLife: LIFE_EVERY, time: 0 }
      loadLevel()
      gameToolbar()
      startCountdown()
      saveNow()
    }

    async function askNew(src) {
      const running = s && (phase === 'play' || phase === 'countdown' || phase === 'paused') && s.source !== 'test'
      if (running && s.score > 0) {
        phase = 'paused'; releaseLock(); saveNow(); closeOverlay()
        const ok = await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Nouvelle partie', danger: true })
        if (!ok) { fillSourceSel(s.source === 'campaign' ? 'campaign' : s.customId); showPause(); return }
      }
      newGame(src)
    }

    function startCountdown() {
      closeOverlay()
      phase = 'countdown'; cd = 3000; acc = 0
      ctx.sfx('tick')
    }

    function showPause(text = 'Partie en pause.') {
      phase = 'paused'
      releaseLock()
      const testing = s.source === 'test'
      closeOv = ctx.overlay({
        title: 'Pause',
        text: `${text}\n${s.source === 'campaign' ? `Niveau ${s.level + 1} · ` : ''}Score ${ctx.format.int(s.score)} · ${s.lives} vie${s.lives > 1 ? 's' : ''}`,
        buttons: [
          { label: 'Reprendre', primary: true, onClick: startCountdown },
          testing ? { label: '← Éditeur', onClick: () => openEditor() } : { label: 'Nouvelle partie', onClick: () => askNew(s.source === 'campaign' ? { source: 'campaign' } : { source: 'custom', id: s.customId }) },
        ],
      })
    }

    function pauseGame() {
      if (scene === 'game' && (phase === 'play' || phase === 'countdown')) { saveNow(); showPause() }
    }

    // ── Gameplay ──
    function launch() {
      let any = false
      for (const b of s.balls) {
        if (!b.stuck) continue
        const ang = clamp(b.off / (s.pw / 2), -1, 1) * (35 * Math.PI / 180)
        b.dx = Math.sin(ang); b.dy = -Math.cos(ang); b.stuck = false
        any = true
      }
      if (any) ctx.sfx('bounce')
    }

    function burst(x, y, color, n = 10) {
      for (let i = 0; i < n; i++) {
        const a = Math.random() * Math.PI * 2, v = 40 + Math.random() * 160
        particles.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 40, life: 500 + Math.random() * 300, color, sz: 2 + Math.random() * 3 })
      }
    }

    function addScore(n) {
      s.score += n
      if (s.score >= s.nextLife) {
        s.nextLife += LIFE_EVERY
        if (s.lives < 9) { s.lives++; ctx.sfx('win'); banner = { text: '+1 vie', t: 1200, small: true } }
      }
    }

    function hitBrick(r, c) {
      const kd = s.kind[r][c]
      const bx = c * BW + BW / 2, by = BY0 + r * BH + BH / 2
      flashes.push({ r, c, t: 140 })
      if (kd === 9) { ctx.sfx('bounce'); return }
      s.hp[r][c]--
      addScore(10)
      if (s.hp[r][c] <= 0) {
        s.hp[r][c] = 0
        addScore(20 * kd)
        ctx.sfx('pop')
        burst(bx, by, BRICK[kd][0])
        if (Math.random() < 0.14) s.caps.push({ x: bx, y: by, type: CAP_BAG[Math.floor(Math.random() * CAP_BAG.length)] })
        saveSoon()
      } else ctx.sfx('hit')
    }

    function applyCap(type) {
      ctx.sfx('merge')
      banner = { text: CAPS[type].name, t: 1100, small: true }
      if (type === 'wide') s.fx.wide = FX_MS.wide
      else if (type === 'slow') s.fx.slow = FX_MS.slow
      else if (type === 'life') { if (s.lives < 9) s.lives++ }
      else if (type === 'multi') {
        const extra = []
        for (const b of s.balls) {
          if (b.stuck) continue
          for (const a of [-0.45, 0.45]) {
            if (s.balls.length + extra.length >= MAX_BALLS) break
            const cs = Math.cos(a), sn = Math.sin(a)
            const nb = { x: b.x, y: b.y, dx: b.dx * cs - b.dy * sn, dy: b.dx * sn + b.dy * cs, stuck: false, off: 0 }
            fixAngle(nb)
            extra.push(nb)
          }
        }
        if (!extra.length && s.balls.some((b) => b.stuck)) {
          launch()
          return applyCap('multi')
        }
        s.balls.push(...extra)
      }
      updateStatus()
    }

    function update(dt) {
      for (const p of particles) { p.life -= dt; p.x += p.vx * dt / 1000; p.y += p.vy * dt / 1000; p.vy += 400 * dt / 1000 }
      particles = particles.filter((p) => p.life > 0)
      for (const f of flashes) f.t -= dt
      flashes = flashes.filter((f) => f.t > 0)
      if (banner) { banner.t -= dt; if (banner.t <= 0) banner = null }
      if (scene !== 'game' || !s) return
      if (phase === 'countdown') {
        const before = Math.ceil(cd / 1000)
        cd -= dt
        movePaddle(dt)
        if (cd <= 0) { phase = 'play'; ctx.sfx('click') } else if (Math.ceil(cd / 1000) !== before) ctx.sfx('tick')
        return
      }
      if (phase === 'clear') {
        clearT -= dt
        if (clearT <= 0) nextLevel()
        return
      }
      if (phase === 'dying') {
        dieT -= dt
        if (dieT <= 0) gameOver(false)
        return
      }
      if (phase !== 'play') return
      s.time += dt
      for (const key of ['wide', 'slow']) if (s.fx[key] > 0) s.fx[key] = Math.max(0, s.fx[key] - dt)
      const targetW = s.fx.wide > 0 ? PW_WIDE : PW
      s.pw += (targetW - s.pw) * Math.min(1, dt / 90)
      movePaddle(dt)

      const sp = s.speed * (s.fx.slow > 0 ? 0.62 : 1)
      const paddle = { x: s.px, w: s.pw }
      for (const b of s.balls) {
        if (b.stuck) { b.x = clamp(s.px + b.off, R, FW - R); b.y = PY - R; continue }
        const res = moveBall(b, sp * dt / 1000, s.hp, paddle)
        if (res.wall) ctx.sfx('bounce')
        if (res.paddle) { s.speed = Math.min(MAX_SPEED, s.speed + 4); ctx.sfx('bounce') }
        for (const [r, c] of res.hits) hitBrick(r, c)
        if (res.lost) b.lost = true
        const tr = trails.get(b) || []
        tr.push([b.x, b.y]); if (tr.length > 7) tr.shift()
        trails.set(b, tr)
      }
      if (s.balls.some((b) => b.lost)) s.balls = s.balls.filter((b) => !b.lost)

      for (const cp of s.caps) {
        cp.y += 130 * dt / 1000
        if (cp.y + 8 >= PY && cp.y - 8 <= PY + PH && Math.abs(cp.x - s.px) <= s.pw / 2 + 14) { cp.got = true; applyCap(cp.type) }
        else if (cp.y > FH + 20) cp.got = true
      }
      s.caps = s.caps.filter((cp) => !cp.got)

      if (!s.balls.length) {
        s.lives--
        ctx.sfx(s.lives > 0 ? 'error' : 'lose')
        if (s.lives <= 0) { phase = 'dying'; dieT = 700; clearTimeout(saveTimer); updateStatus(); return }
        banner = { text: `Balle perdue — ${s.lives} vie${s.lives > 1 ? 's' : ''}`, t: 1400, small: true }
        resetBalls()
        saveNow()
      }
      if (countLeft(s.hp) === 0) {
        phase = 'clear'; clearT = 1600
        addScore(s.lives * 100)
        banner = { text: 'Niveau terminé !', t: 1600 }
        ctx.sfx('win')
      }
      updateStatus()
    }

    function movePaddle(dt) {
      const l = ctx.isHeld('left'), r = ctx.isHeld('right')
      if (l !== r) { keyMode = true; s.px += (r ? 1 : -1) * KEY_SPEED * dt / 1000 }
      s.px = clamp(s.px, s.pw / 2, FW - s.pw / 2)
      for (const b of s.balls) if (b.stuck) { b.x = clamp(s.px + b.off, R, FW - R); b.y = PY - R }
    }

    function nextLevel() {
      if (s.source !== 'campaign' || s.level + 1 >= LEVELS.length) { gameOver(true); return }
      s.level++
      loadLevel()
      updateStatus()
      startCountdown()
      saveNow()
    }

    async function gameOver(won) {
      phase = 'over'
      overAt = performance.now()
      releaseLock()
      clearTimeout(saveTimer)
      const src = s.source
      if (src !== 'test') ctx.save.clear()
      const score = s.score, lvl = s.level + 1
      let r = null
      if (src === 'campaign') {
        try { r = await ctx.scores.submit('campaign', score, { meta: { level: lvl, won } }) } catch (e) { ctx.toast(e.message, 'err') }
        const st = await ctx.stats.update({ inc: { played: 1, won: won ? 1 : 0 }, max: { best_score: score, best_level: lvl } })
        ctx.stats.update({ set: { hub_best: `${ctx.format.int(st.best_score ?? score)} (niv. ${st.best_level ?? lvl})` } })
        if (score > best) best = score
      } else if (src === 'custom') {
        ctx.stats.update({ inc: { custom_played: 1, custom_won: won ? 1 : 0 } })
      }
      updateStatus()
      if (phase !== 'over' || scene !== 'game') return
      const title = won ? (src === 'campaign' ? 'Campagne terminée !' : 'Niveau réussi !') : 'Perdu !'
      const lines = [`Score ${ctx.format.int(score)}${src === 'campaign' ? ` · niveau ${lvl}/${LEVELS.length}` : ''}`]
      if (r?.record && score > 0) lines.push('Nouveau record !')
      else if (r?.rank) lines.push(`Rang ${r.rank} sur ${r.total}`)
      if (won) ctx.sfx('win')
      const replay = src === 'test' ? { source: 'test', rows: s.rows, name: s.name } : src === 'campaign' ? { source: 'campaign' } : { source: 'custom', id: s.customId }
      const show = () => {
        closeOv = ctx.overlay({
          title, text: lines.join('\n'),
          buttons: [
            { label: 'Rejouer', primary: true, onClick: () => newGame(replay) },
            src === 'test' ? { label: '← Éditeur', onClick: () => openEditor() } : null,
            src === 'campaign' ? { label: '🏆 Scores', onClick: () => showScores(r?.id).then(() => { if (phase === 'over') show() }) } : null,
          ].filter(Boolean),
        })
      }
      show()
    }

    // ── Drawing ──
    function rr(x, y, w, hh, rad) {
      g.beginPath()
      if (g.roundRect) g.roundRect(x, y, w, hh, rad); else g.rect(x, y, w, hh)
    }

    function drawField(gc, kind, hp, opts = {}) {
      const bg = gc.createLinearGradient(0, 0, 0, FH)
      bg.addColorStop(0, '#16132a'); bg.addColorStop(1, '#0b0a14')
      gc.fillStyle = bg
      gc.fillRect(0, 0, FW, FH)
      gc.strokeStyle = 'rgba(255,255,255,0.035)'
      gc.lineWidth = 1
      if (!opts.grid) {
        for (let x = 0; x <= FW; x += 26) { gc.beginPath(); gc.moveTo(x, TOP); gc.lineTo(x, FH); gc.stroke() }
        for (let y = TOP; y <= FH; y += 26) { gc.beginPath(); gc.moveTo(0, y); gc.lineTo(FW, y); gc.stroke() }
      }
      gc.fillStyle = 'rgba(0,0,0,0.35)'
      gc.fillRect(0, 0, FW, TOP)
      gc.strokeStyle = 'rgba(167,139,250,0.35)'
      gc.beginPath(); gc.moveTo(0, TOP + 0.5); gc.lineTo(FW, TOP + 0.5); gc.stroke()
      if (opts.grid) {
        gc.strokeStyle = 'rgba(255,255,255,0.08)'
        for (let c = 0; c <= GC; c++) { gc.beginPath(); gc.moveTo(c * BW, BY0); gc.lineTo(c * BW, BY0 + GR * BH); gc.stroke() }
        for (let r = 0; r <= GR; r++) { gc.beginPath(); gc.moveTo(0, BY0 + r * BH); gc.lineTo(FW, BY0 + r * BH); gc.stroke() }
      }
      for (let r = 0; r < GR; r++) for (let c = 0; c < GC; c++) {
        const kd = kind[r][c], v = hp[r][c]
        if (!kd || !v) continue
        const x = c * BW + 1.5, y = BY0 + r * BH + 1.5, w = BW - 3, hh = BH - 3
        const [c1, c2] = BRICK[kd]
        const gr = gc.createLinearGradient(0, y, 0, y + hh)
        gr.addColorStop(0, c1); gr.addColorStop(1, c2)
        gc.fillStyle = gr
        gc.beginPath(); gc.roundRect ? gc.roundRect(x, y, w, hh, 3) : gc.rect(x, y, w, hh); gc.fill()
        gc.fillStyle = 'rgba(255,255,255,0.25)'
        gc.fillRect(x + 3, y + 2, w - 6, 2)
        if (kd === 9) {
          gc.fillStyle = 'rgba(0,0,0,0.35)'
          for (const px of [x + 4, x + w - 4]) { gc.beginPath(); gc.arc(px, y + hh / 2, 1.6, 0, Math.PI * 2); gc.fill() }
        } else if (v < kd) {
          gc.strokeStyle = 'rgba(10,10,20,0.6)'; gc.lineWidth = 1.2
          gc.beginPath()
          gc.moveTo(x + w * 0.3, y + 1); gc.lineTo(x + w * 0.42, y + hh * 0.5); gc.lineTo(x + w * 0.34, y + hh - 1)
          if (kd - v >= 2) { gc.moveTo(x + w * 0.7, y + 1); gc.lineTo(x + w * 0.6, y + hh * 0.55); gc.lineTo(x + w * 0.72, y + hh - 1) }
          gc.stroke()
        }
      }
    }

    function draw() {
      if (scene !== 'game' || !W) return
      g.save()
      const kind = s ? s.kind : parseRows(LEVELS[0].rows).kind
      const hp = s ? s.hp : parseRows(LEVELS[0].rows).hp
      drawField(g, kind, hp)
      for (const f of flashes) {
        g.fillStyle = `rgba(255,255,255,${0.6 * f.t / 140})`
        g.fillRect(f.c * BW + 1.5, BY0 + f.r * BH + 1.5, BW - 3, BH - 3)
      }
      for (const p of particles) {
        g.globalAlpha = Math.max(0, Math.min(1, p.life / 400))
        g.fillStyle = p.color
        g.fillRect(p.x - p.sz / 2, p.y - p.sz / 2, p.sz, p.sz)
      }
      g.globalAlpha = 1
      if (s) {
        for (const cp of s.caps) {
          const c = CAPS[cp.type]
          g.fillStyle = c.color
          rr(cp.x - 15, cp.y - 8, 30, 16, 8); g.fill()
          g.fillStyle = 'rgba(255,255,255,0.35)'
          rr(cp.x - 11, cp.y - 6, 22, 4, 2); g.fill()
          g.fillStyle = '#111827'
          g.font = '700 11px system-ui, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle'
          g.fillText(c.label, cp.x, cp.y + 1)
        }
        const px = s.px - s.pw / 2
        const pg = g.createLinearGradient(0, PY, 0, PY + PH)
        pg.addColorStop(0, s.fx.wide > 0 ? '#86efac' : '#e0e7ff'); pg.addColorStop(1, s.fx.wide > 0 ? '#15803d' : '#6366f1')
        g.fillStyle = pg
        rr(px, PY, s.pw, PH, PH / 2); g.fill()
        g.fillStyle = 'rgba(255,255,255,0.5)'
        rr(px + 8, PY + 2, s.pw - 16, 2.5, 1.5); g.fill()
        const slow = s.fx.slow > 0
        for (const b of s.balls) {
          const tr = trails.get(b)
          if (tr && !b.stuck) tr.forEach(([x, y], i) => {
            g.fillStyle = slow ? `rgba(251,191,36,${0.05 + i * 0.03})` : `rgba(199,210,254,${0.05 + i * 0.03})`
            g.beginPath(); g.arc(x, y, R * (0.4 + i * 0.08), 0, Math.PI * 2); g.fill()
          })
          const bg = g.createRadialGradient(b.x - 2, b.y - 2, 1, b.x, b.y, R)
          bg.addColorStop(0, '#ffffff'); bg.addColorStop(1, slow ? '#f59e0b' : '#a5b4fc')
          g.fillStyle = bg
          g.beginPath(); g.arc(b.x, b.y, R, 0, Math.PI * 2); g.fill()
        }
        if (phase === 'play' && s.balls.some((b) => b.stuck) && !banner) {
          g.fillStyle = 'rgba(226,232,240,0.7)'; g.font = '13px system-ui, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle'
          g.fillText('Clic, Espace ou ↑ pour lancer', FW / 2, PY - 60)
        }
        g.textBaseline = 'middle'; g.font = '600 14px system-ui, sans-serif'
        g.textAlign = 'left'; g.fillStyle = '#e4e4e7'
        g.fillText(ctx.format.int(s.score), 12, TOP / 2)
        g.textAlign = 'center'; g.fillStyle = '#a1a1aa'
        g.fillText(s.source === 'campaign' ? `${s.level + 1}. ${s.name}` : s.name, FW / 2, TOP / 2)
        g.textAlign = 'right'; g.fillStyle = '#f472b6'
        g.fillText('❤'.repeat(Math.min(s.lives, 6)) + (s.lives > 6 ? `+${s.lives - 6}` : ''), FW - 12, TOP / 2)
        let fxX = 12
        for (const [key, col] of [['wide', CAPS.wide.color], ['slow', CAPS.slow.color]]) {
          if (s.fx[key] <= 0) continue
          g.fillStyle = 'rgba(255,255,255,0.12)'; g.fillRect(fxX, FH - 12, 60, 4)
          g.fillStyle = col; g.fillRect(fxX, FH - 12, 60 * s.fx[key] / FX_MS[key], 4)
          fxX += 70
        }
      }
      if (phase === 'countdown') {
        g.fillStyle = 'rgba(8,6,16,0.45)'
        g.fillRect(0, TOP, FW, FH - TOP)
        const n = Math.ceil(cd / 1000), f = (cd % 1000) / 1000
        g.textAlign = 'center'; g.textBaseline = 'middle'
        g.font = `800 ${Math.round(70 + f * 40)}px system-ui, sans-serif`
        g.globalAlpha = 0.35 + 0.65 * f
        g.fillStyle = '#eef2ff'
        g.fillText(String(n), FW / 2, FH * 0.45)
        g.globalAlpha = 1
      }
      if (banner) {
        g.globalAlpha = Math.min(1, banner.t / 300)
        g.textAlign = 'center'; g.textBaseline = 'middle'
        g.font = banner.small ? '700 18px system-ui, sans-serif' : '800 26px system-ui, sans-serif'
        g.strokeStyle = 'rgba(0,0,0,0.6)'; g.lineWidth = 4
        g.strokeText(banner.text, FW / 2, banner.small ? FH * 0.62 : FH * 0.6)
        g.fillStyle = banner.small ? '#fde68a' : '#e0e7ff'
        g.fillText(banner.text, FW / 2, banner.small ? FH * 0.62 : FH * 0.6)
        g.globalAlpha = 1
      }
      if (phase === 'dying') {
        g.fillStyle = `rgba(239,68,68,${0.25 * dieT / 700})`
        g.fillRect(0, 0, FW, FH)
      }
      g.restore()
    }

    ctx.loop((dt) => {
      acc += dt
      while (acc >= STEP) { update(STEP); acc -= STEP }
      draw()
    })

    // ── Input ──
    ctx.onAction((action, ev) => {
      if (scene !== 'game') return
      if (action === 'start') {
        if (phase === 'play' || phase === 'countdown') pauseGame()
        else if (phase === 'paused') startCountdown()
        return
      }
      if (ev.repeat) return
      if (action === 'left' || action === 'right') { keyMode = true; return }
      if (action === 'a' || action === 'up') {
        if (phase === 'play') launch()
        else if (phase === 'paused' && action === 'a') startCountdown()
        else if (phase === 'over' && action === 'a' && performance.now() - overAt > 800) newGame(s.source === 'test' ? { source: 'test', rows: s.rows, name: s.name } : s.source === 'campaign' ? { source: 'campaign' } : { source: 'custom', id: s.customId })
        else if (phase === 'idle' && action === 'a') newGame({ source: 'campaign' })
      }
    })

    const toField = (e) => {
      const rect = canvas.getBoundingClientRect()
      return [(e.clientX - rect.left) / k, (e.clientY - rect.top) / k]
    }
    canvas.addEventListener('pointermove', (e) => {
      if (!s || scene !== 'game' || !(phase === 'play' || phase === 'countdown')) return
      if (document.pointerLockElement === canvas) s.px += e.movementX / k
      else {
        if (keyMode && Math.abs(e.movementX) < 2) return
        keyMode = false
        s.px = toField(e)[0]
      }
      s.px = clamp(s.px, s.pw / 2, FW - s.pw / 2)
    })
    canvas.addEventListener('pointerdown', (e) => {
      if (scene !== 'game') return
      if (phase === 'idle') { newGame({ source: 'campaign' }); return }
      if (phase !== 'play') return
      if (prefs.pointerLock && document.pointerLockElement !== canvas) {
        try { const p = canvas.requestPointerLock(); if (p?.catch) p.catch(() => {}) } catch { /* unsupported */ }
      }
      if (e.button === 0) launch()
    })
    canvas.addEventListener('contextmenu', (e) => e.preventDefault())
    const onLockChange = () => { canvas.classList.toggle('play', document.pointerLockElement === canvas) }
    document.addEventListener('pointerlockchange', onLockChange)
    ctx.onCleanup(() => { document.removeEventListener('pointerlockchange', onLockChange); releaseLock() })

    // ── Editor ──
    const BRUSHES = [['.', 'Gomme', null], ['1', '1 coup', BRICK[1][0]], ['2', '2 coups', BRICK[2][0]], ['3', '3 coups', BRICK[3][0]], ['#', 'Acier', BRICK[9][0]]]
    const ed = { grid: [], name: 'Mon niveau', id: null, brush: '1', dirty: false }
    const emptyGrid = () => Array.from({ length: GR }, () => Array(GC).fill('.'))
    const gridFrom = (rows) => Array.from({ length: GR }, (_, r) => Array.from({ length: GC }, (_, c) => {
      const ch = (rows[r] || '')[c] || '.'
      return '123#'.includes(ch) ? ch : '.'
    }))
    const gridRows = () => {
      const rows = ed.grid.map((r) => r.join(''))
      while (rows.length && /^\.+$/.test(rows[rows.length - 1])) rows.pop()
      return rows
    }
    ed.grid = emptyGrid()

    const edCanvas = h('canvas')
    const eg = edCanvas.getContext('2d')
    let ek = 1
    const nameIn = h('input', { maxlength: 30, placeholder: 'Nom du niveau', oninput: () => { ed.name = nameIn.value; ed.dirty = true; edDraw() } })
    const brushBox = h('div', { class: 'brushes' }, BRUSHES.map(([v, label, col]) => h('button', { 'data-b': v, title: label, onclick: () => { ed.brush = v; syncBrush() } },
      h('span', { class: 'sw', style: { background: col || 'repeating-linear-gradient(45deg,#3f3f46 0 3px,#18181b 3px 6px)' } }), label.split(' ')[0])))
    const syncBrush = () => { for (const b of brushBox.children) b.classList.toggle('on', b.dataset.b === ed.brush) }
    const savedSel = h('select')
    const builtinSel = h('select', null, h('option', { value: '' }, 'Partir d’un niveau intégré…'), LEVELS.map((l, i) => h('option', { value: i }, `${i + 1}. ${l.name}`)))
    builtinSel.onchange = async () => {
      const i = builtinSel.value
      builtinSel.value = ''
      if (i === '') return
      if (ed.dirty && !(await ctx.confirm('Remplacer la grille actuelle ?'))) return
      ed.grid = gridFrom(LEVELS[+i].rows); ed.name = `${LEVELS[+i].name} (copie)`; ed.id = null; ed.dirty = true
      nameIn.value = ed.name; edDraw(); syncSaved()
    }
    const fillSaved = () => {
      savedSel.replaceChildren(h('option', { value: '' }, prefs.custom.length ? '— niveaux enregistrés —' : 'Aucun niveau enregistré'),
        prefs.custom.map((l) => h('option', { value: l.id }, l.name)))
      savedSel.value = ed.id || ''
    }
    const edStatus = () => ctx.setStatus(h('span', { class: 'pill' }, `${prefs.custom.length} niveau${prefs.custom.length > 1 ? 'x' : ''} perso`))
    const syncSaved = () => { fillSaved() }
    savedSel.onchange = async () => {
      const l = prefs.custom.find((x) => x.id === savedSel.value)
      if (!l) return
      if (ed.dirty && !(await ctx.confirm('Abandonner les modifications non enregistrées ?'))) { savedSel.value = ed.id || ''; return }
      ed.grid = gridFrom(l.rows); ed.name = l.name; ed.id = l.id; ed.dirty = false
      nameIn.value = ed.name; edDraw()
    }

    const validLevel = () => {
      const rows = gridRows()
      if (!rows.some((r) => /[123]/.test(r))) { ctx.toast('Ajoutez au moins une brique destructible.', 'err'); return null }
      return rows
    }
    async function edSave() {
      const rows = validLevel()
      if (!rows) return null
      const name = (nameIn.value.trim() || 'Sans titre').slice(0, 30)
      if (ed.id && prefs.custom.some((l) => l.id === ed.id)) {
        const l = prefs.custom.find((x) => x.id === ed.id)
        l.name = name; l.rows = rows
      } else {
        if (prefs.custom.length >= 60) { ctx.toast('60 niveaux maximum.', 'err'); return null }
        ed.id = `c${Date.now().toString(36)}`
        prefs.custom.push({ id: ed.id, name, rows })
      }
      ed.name = name; ed.dirty = false
      await savePrefs()
      fillSaved(); edStatus()
      ctx.toast(`Niveau « ${name} » enregistré`, 'ok')
      return ed.id
    }
    async function edDelete() {
      if (!ed.id) { ctx.toast('Ce niveau n’est pas enregistré.'); return }
      const l = prefs.custom.find((x) => x.id === ed.id)
      if (!l || !(await ctx.confirm(`Supprimer le niveau « ${l.name} » ?`, { ok: 'Supprimer', danger: true }))) return
      prefs.custom = prefs.custom.filter((x) => x.id !== ed.id)
      ed.id = null; ed.dirty = true
      await savePrefs()
      fillSaved(); edStatus()
    }
    const edTest = () => {
      const rows = validLevel()
      if (!rows) return
      closeEditor()
      newGame({ source: 'test', rows, name: `Test : ${nameIn.value.trim() || 'Sans titre'}` })
    }
    async function edPlay() {
      const id = await edSave()
      if (!id) return
      closeEditor()
      await askNew({ source: 'custom', id })
    }
    const mirror = () => {
      for (const row of ed.grid) for (let c = 0; c < Math.floor(GC / 2); c++) row[GC - 1 - c] = row[c]
      ed.dirty = true; edDraw()
    }
    const clearAll = async () => {
      if (!(await ctx.confirm('Effacer toute la grille ?'))) return
      ed.grid = emptyGrid(); ed.dirty = true; edDraw()
    }
    const newLevel = async () => {
      if (ed.dirty && !(await ctx.confirm('Abandonner les modifications non enregistrées ?'))) return
      ed.grid = emptyGrid(); ed.id = null; ed.name = 'Mon niveau'; ed.dirty = false
      nameIn.value = ed.name; fillSaved(); edDraw()
    }

    const edArea = h('div', { class: 'ed-area' }, edCanvas)
    const edRoot = h('div', { class: 'g-breakout-ed' }, edArea,
      h('div', { class: 'ed-side' },
        h('div', { class: 'ed-box' }, h('h3', null, 'Pinceau'), brushBox,
          h('div', { class: 'hint' }, 'Clic gauche : peindre (glisser pour plusieurs cases) · clic droit : effacer.')),
        h('div', { class: 'ed-box' }, h('h3', null, 'Niveau'), nameIn,
          h('div', { class: 'row' }, h('button', { onclick: newLevel }, 'Nouveau'), h('button', { class: 'danger', onclick: edDelete }, 'Supprimer')),
          h('div', { class: 'row' }, h('button', { onclick: mirror, title: 'Recopie la moitié gauche à droite' }, 'Symétrie'), h('button', { onclick: clearAll }, 'Effacer')),
          savedSel, builtinSel),
        h('div', { class: 'ed-box' },
          h('button', { onclick: edTest }, '▶ Tester'),
          h('button', { onclick: () => edSave() }, '💾 Enregistrer'),
          h('button', { class: 'primary', onclick: edPlay }, 'Enregistrer et jouer'))))

    function edLayout() {
      const aw = Math.max(160, edArea.clientWidth - 4), ah = Math.max(160, edArea.clientHeight - 4)
      ek = Math.min(aw / FW, ah / (BY0 + GR * BH + 70))
      const ew = Math.floor(FW * ek), eh = Math.floor((BY0 + GR * BH + 70) * ek)
      const d = window.devicePixelRatio || 1
      edCanvas.style.width = `${ew}px`; edCanvas.style.height = `${eh}px`
      edCanvas.width = Math.round(ew * d); edCanvas.height = Math.round(eh * d)
      eg.setTransform(d * ek, 0, 0, d * ek, 0, 0)
      edDraw()
    }
    function edDraw() {
      if (scene !== 'editor') return
      const { kind, hp } = parseRows(ed.grid.map((r) => r.join('')))
      drawField(eg, kind, hp, { grid: true })
      eg.fillStyle = 'rgba(224,231,255,0.25)'
      eg.beginPath(); eg.roundRect ? eg.roundRect(FW / 2 - PW / 2, BY0 + GR * BH + 40, PW, PH, 6) : eg.rect(FW / 2 - PW / 2, BY0 + GR * BH + 40, PW, PH); eg.fill()
      eg.fillStyle = '#a1a1aa'; eg.font = '600 14px system-ui, sans-serif'; eg.textAlign = 'center'; eg.textBaseline = 'middle'
      eg.fillText(`Éditeur — ${nameIn.value.trim() || 'Sans titre'}${ed.dirty ? ' •' : ''}`, FW / 2, TOP / 2)
      if (edHover) {
        eg.strokeStyle = 'rgba(255,255,255,0.7)'; eg.lineWidth = 1.5
        eg.strokeRect(edHover[1] * BW + 1, BY0 + edHover[0] * BH + 1, BW - 2, BH - 2)
      }
    }
    let painting = null, edHover = null
    const edCell = (e) => {
      const rect = edCanvas.getBoundingClientRect()
      const x = (e.clientX - rect.left) / ek, y = (e.clientY - rect.top) / ek
      const c = Math.floor(x / BW), r = Math.floor((y - BY0) / BH)
      return r >= 0 && r < GR && c >= 0 && c < GC ? [r, c] : null
    }
    const paint = (cell) => {
      if (!cell || !painting) return
      const [r, c] = cell
      if (ed.grid[r][c] !== painting) { ed.grid[r][c] = painting; ed.dirty = true; ctx.sfx('tick') }
    }
    edCanvas.addEventListener('pointerdown', (e) => {
      e.preventDefault()
      painting = e.button === 2 ? '.' : ed.brush
      try { edCanvas.setPointerCapture(e.pointerId) } catch { /* synthetic */ }
      paint(edCell(e)); edDraw()
    })
    edCanvas.addEventListener('pointermove', (e) => { edHover = edCell(e); paint(edHover); edDraw() })
    const endPaint = () => { painting = null }
    edCanvas.addEventListener('pointerup', endPaint)
    edCanvas.addEventListener('pointercancel', endPaint)
    edCanvas.addEventListener('pointerleave', () => { edHover = null; edDraw() })
    edCanvas.addEventListener('contextmenu', (e) => e.preventDefault())

    function openEditor() {
      if (scene === 'game' && s && s.source !== 'test' && (phase === 'play' || phase === 'countdown' || phase === 'paused')) saveNow()
      closeOverlay()
      releaseLock()
      if (s && s.source === 'test') s = null
      phase = 'idle'
      scene = 'editor'
      gameWrap.remove()
      root.append(edRoot)
      nameIn.value = ed.name
      fillSaved(); syncBrush()
      ctx.setToolbar(
        h('button', { onclick: () => leaveEditor() }, '← Retour au jeu'),
        h('button', { onclick: edTest }, '▶ Tester'),
        h('button', { class: 'icon', title: 'Aide', onclick: showHelp }, '❔'))
      edStatus()
      requestAnimationFrame(edLayout)
    }
    function closeEditor() {
      if (scene !== 'editor') return
      edRoot.remove()
      root.append(gameWrap)
      scene = 'game'
      requestAnimationFrame(layout)
    }
    async function leaveEditor() {
      if (ed.dirty && gridRows().length && !(await ctx.confirm('Quitter l’éditeur sans enregistrer ?', { ok: 'Quitter' }))) return
      closeEditor()
      await resumeOrIdle()
    }

    async function resumeOrIdle(first = false) {
      const saved = await ctx.save.get().catch(() => null)
      if (validSave(saved)) {
        s = saved
        trails.clear()
        gameToolbar()
        layout()
        showPause(first ? 'Partie précédente retrouvée.' : 'Partie en cours.')
      } else {
        s = null
        phase = 'idle'
        gameToolbar()
        layout()
        closeOv = ctx.overlay({
          title: 'Casse-briques',
          text: `${LEVELS.length} niveaux à briser, ou vos propres niveaux créés dans l’éditeur.`,
          buttons: [
            { label: 'Jouer la campagne', primary: true, onClick: () => newGame({ source: 'campaign' }) },
            { label: '✏️ Éditeur', onClick: () => openEditor() },
          ],
        })
      }
    }

    root.append(gameWrap)
    await resumeOrIdle(true)

    return {
      pause() { pauseGame() },
      destroy() { saveNow() },
    }
  },
}
