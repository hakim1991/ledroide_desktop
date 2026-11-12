const LEVELS = {
  beginner: { label: 'Débutant', w: 9, h: 9, n: 10 },
  intermediate: { label: 'Intermédiaire', w: 16, h: 16, n: 40 },
  expert: { label: 'Expert', w: 30, h: 16, n: 99 },
  custom: { label: 'Personnalisé' },
}
const SCORED = ['beginner', 'intermediate', 'expert']

const CSS = `
.g-minesweeper { flex: 1; display: flex; align-items: center; justify-content: center; padding: 14px; min-height: 0; }
.g-minesweeper .board { display: grid; gap: 2px; padding: 8px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: 0 8px 30px rgba(0,0,0,.35); touch-action: none; }
.g-minesweeper .board.blur .c { color: transparent !important; filter: blur(3px); }
.g-minesweeper .c { width: var(--cs); height: var(--cs); border-radius: calc(var(--cs) * .16); display: grid; place-items: center;
  font-weight: 800; font-size: calc(var(--cs) * .56); line-height: 1; cursor: pointer; position: relative;
  background: linear-gradient(160deg, #34344a, #27273a); box-shadow: inset 0 -2px 0 rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.07); }
.g-minesweeper .c:hover:not(.o) { background: linear-gradient(160deg, #40405c, #2f2f46); }
.g-minesweeper .c.o { background: #16161b; box-shadow: inset 0 0 0 1px #202027; cursor: default; animation: ms-open .12s ease-out; }
.g-minesweeper .c.cur { outline: 2px solid var(--accent-h); outline-offset: 1px; z-index: 1; }
.g-minesweeper .c.press:not(.o) { background: #1d1d25; }
.g-minesweeper .c.boom { background: #7f1d1d !important; animation: ms-boom .4s; }
.g-minesweeper .c.bad::after { content: '✕'; position: absolute; color: #f87171; font-size: calc(var(--cs) * .8); }
.g-minesweeper .c.win-m { background: linear-gradient(160deg, #1f4d36, #173a29); }
.g-minesweeper .n1 { color: #60a5fa } .g-minesweeper .n2 { color: #4ade80 } .g-minesweeper .n3 { color: #f87171 }
.g-minesweeper .n4 { color: #c084fc } .g-minesweeper .n5 { color: #fb923c } .g-minesweeper .n6 { color: #22d3ee }
.g-minesweeper .n7 { color: #f472b6 } .g-minesweeper .n8 { color: #a1a1aa }
.g-minesweeper .ico { font-size: calc(var(--cs) * .58); }
@keyframes ms-open { from { transform: scale(.85); opacity: .4 } }
@keyframes ms-boom { 0%,100% { transform: none } 25% { transform: scale(1.25) } 50% { transform: rotate(8deg) } }
`

export default {
  async mount(root, ctx) {
    const { h, format } = ctx
    root.append(h('style', null, CSS))
    const wrap = h('div', { class: 'g-minesweeper' })
    const board = h('div', { class: 'board' })
    wrap.append(board)
    root.append(wrap)

    let prefs = { level: 'beginner', custom: { w: 20, h: 12, n: 40 }, marks: false, ...(await ctx.prefs.get().catch(() => ({}))) }
    let s = null
    let cells = []
    let timerStart = 0
    let paused = false
    let cursor = -1
    let saveTimer = 0
    let pressed = []

    const minesPill = h('span', { class: 'pill', title: 'Mines restantes' })
    const timePill = h('span', { class: 'pill', title: 'Temps' })
    const levelSel = h('select', { onchange: () => changeLevel(levelSel.value) },
      Object.entries(LEVELS).map(([k, v]) => h('option', { value: k }, v.label)))
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => newGame(), title: 'Nouvelle partie (F2)' }, '↻ Nouvelle partie'),
      levelSel,
      h('button', { class: 'icon', title: 'Meilleurs temps', onclick: () => showScores() }, '🏆'),
      h('button', { class: 'icon', title: 'Statistiques', onclick: () => showStats() }, '📊'),
      h('button', { class: 'icon', title: 'Aide', onclick: () => help() }, '❔'),
    )
    ctx.setStatus(minesPill, timePill)

    const dims = () => (prefs.level === 'custom' ? prefs.custom : LEVELS[prefs.level])
    const elapsed = () => (s.startedAt && !s.over ? s.elapsed + (paused ? 0 : performance.now() - timerStart) : s.elapsed)
    const neighbors = (i) => {
      const x = i % s.w, y = (i / s.w) | 0, out = []
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        if (!dx && !dy) continue
        const nx = x + dx, ny = y + dy
        if (nx >= 0 && ny >= 0 && nx < s.w && ny < s.h) out.push(ny * s.w + nx)
      }
      return out
    }

    function fresh() {
      const { w, h: hh, n } = dims()
      return { level: prefs.level, w, h: hh, n, mines: null, open: Array(w * hh).fill(0), mark: Array(w * hh).fill(0),
        elapsed: 0, startedAt: null, over: null, boom: -1 }
    }

    function placeMines(safe) {
      const total = s.w * s.h
      const excl = new Set([safe, ...neighbors(safe)])
      if (total - excl.size < s.n) excl.clear(), excl.add(safe)
      const pool = []
      for (let i = 0; i < total; i++) if (!excl.has(i)) pool.push(i)
      ctx.shuffle(pool)
      const mines = Array(total).fill(0)
      for (const i of pool.slice(0, s.n)) mines[i] = 1
      s.mines = mines
    }
    const count = (i) => neighbors(i).reduce((a, j) => a + s.mines[j], 0)

    function build() {
      const total = s.w * s.h
      board.style.gridTemplateColumns = `repeat(${s.w}, var(--cs))`
      cells = []
      const frag = document.createDocumentFragment()
      for (let i = 0; i < total; i++) {
        const c = h('div', { class: 'c', dataset: { i } })
        cells.push(c)
        frag.append(c)
      }
      board.replaceChildren(frag)
      levelSel.value = s.level
      fit()
      renderAll()
    }

    function renderCell(i) {
      const c = cells[i]
      let cls = 'c', txt = ''
      if (s.open[i]) {
        cls += ' o'
        if (s.mines?.[i]) { txt = '💣'; cls += ' ico' + (i === s.boom ? ' boom' : '') }
        else { const k = count(i); if (k) { txt = k; cls += ` n${k}` } }
      } else if (s.over && s.mines?.[i]) {
        if (s.over === 'won') { txt = '🚩'; cls += ' ico win-m' } else if (s.mark[i] === 1) { txt = '🚩'; cls += ' ico' } else { txt = '💣'; cls += ' ico o' }
      } else if (s.mark[i] === 1) { txt = '🚩'; cls += ' ico'; if (s.over === 'lost' && s.mines && !s.mines[i]) cls += ' bad' }
      else if (s.mark[i] === 2) { txt = '?' }
      if (i === cursor) cls += ' cur'
      c.className = cls
      c.textContent = txt
    }
    function renderAll() { for (let i = 0; i < cells.length; i++) renderCell(i); renderStatus() }

    function renderStatus() {
      const flags = s.mark.filter((m) => m === 1).length
      minesPill.textContent = `💣 ${s.n - flags}`
      timePill.textContent = `⏱ ${format.time(elapsed()).replace(/\.\d$/, '')}`
    }

    function fit() {
      const r = wrap.getBoundingClientRect()
      const cs = Math.max(16, Math.min(40, Math.floor(Math.min((r.width - 48) / s.w, (r.height - 48) / s.h) - 2)))
      board.style.setProperty('--cs', `${cs}px`)
    }
    const ro = new ResizeObserver(() => s && fit())
    ro.observe(wrap)
    ctx.onCleanup(() => ro.disconnect())

    function startClock() {
      if (s.startedAt) return
      s.startedAt = new Date().toISOString()
      timerStart = performance.now()
    }
    const tick = setInterval(() => { if (s && s.startedAt && !s.over && !paused) renderStatus() }, 250)
    ctx.onCleanup(() => clearInterval(tick))

    function persist(now = false) {
      clearTimeout(saveTimer)
      const run = () => {
        if (!s || s.over || !s.startedAt) return
        ctx.save.set({ ...s, elapsed: elapsed() })
      }
      if (now) run(); else saveTimer = setTimeout(run, 400)
    }

    function reveal(i) {
      if (s.over || s.open[i] || s.mark[i] === 1) return
      if (!s.mines) { placeMines(i); startClock() }
      if (s.mines[i]) return lose(i)
      const stack = [i]
      let n = 0
      while (stack.length) {
        const j = stack.pop()
        if (s.open[j] || s.mark[j] === 1) continue
        s.open[j] = 1
        n++
        renderCell(j)
        if (count(j) === 0) for (const k of neighbors(j)) if (!s.open[k] && !s.mines[k]) stack.push(k)
      }
      ctx.sfx(n > 1 ? 'pop' : 'click')
      checkWin()
    }

    function chord(i) {
      if (s.over || !s.open[i] || !s.mines) return
      const ns = neighbors(i)
      const flags = ns.filter((j) => s.mark[j] === 1).length
      if (flags !== count(i) || !flags) return
      for (const j of ns) {
        if (s.over) break
        if (!s.open[j] && s.mark[j] !== 1) {
          if (s.mines[j]) return lose(j)
          reveal(j)
        }
      }
    }

    function toggleMark(i) {
      if (s.over || s.open[i]) return
      s.mark[i] = s.mark[i] === 0 ? 1 : s.mark[i] === 1 && prefs.marks ? 2 : 0
      ctx.sfx('flag')
      renderCell(i)
      renderStatus()
      persist()
    }

    function act(i, kind) {
      if (paused || i < 0) return
      if (kind === 'reveal') { if (s.open[i]) chord(i); else reveal(i) }
      else if (kind === 'mark') { if (s.open[i]) chord(i); else toggleMark(i) }
      else if (kind === 'chord') chord(i)
      if (!s.over) persist()
    }

    function checkWin() {
      const total = s.w * s.h
      let open = 0
      for (let i = 0; i < total; i++) open += s.open[i]
      if (open === total - s.n) win()
    }

    async function win() {
      s.elapsed = elapsed()
      s.over = 'won'
      renderAll()
      ctx.sfx('win')
      ctx.save.clear()
      const t = s.elapsed
      const stats = await ctx.stats.get().catch(() => ({}))
      const streak = (stats.streak || 0) + 1
      let rank = null, scoreId = null
      if (SCORED.includes(s.level)) {
        try {
          const r = await ctx.scores.submit(s.level, Math.round(t), { lowerBetter: true, meta: { w: s.w, h: s.h, n: s.n } })
          rank = r.rank; scoreId = r.id
        } catch (e) { ctx.toast(e.message, 'err') }
      }
      const best = { ...(stats.best || {}) }
      if (SCORED.includes(s.level) && (!best[s.level] || t < best[s.level])) best[s.level] = Math.round(t)
      const hubLvl = ['expert', 'intermediate', 'beginner'].find((l) => best[l])
      await ctx.stats.update({
        inc: { played: 1, won: 1, [`won_${s.level}`]: 1, [`played_${s.level}`]: 1 },
        max: { streak_max: streak },
        set: { streak, best, ...(hubLvl ? { hub_best: `${format.time(best[hubLvl]).replace(/\.\d$/, '')} (${LEVELS[hubLvl].label})` } : {}) },
      })
      ctx.overlay({
        title: '🎉 Gagné !',
        text: `${LEVELS[s.level].label} en ${format.time(t)}${rank === 1 ? '\nNouveau record !' : rank ? `\nRang ${rank} au classement` : ''}`,
        buttons: [
          ...(SCORED.includes(s.level) ? [{ label: '🏆 Classement', onClick: () => showScores(scoreId) }] : []),
          { label: 'Rejouer', primary: true, onClick: () => newGame() },
        ],
      })
    }

    async function lose(i) {
      s.elapsed = elapsed()
      s.open[i] = 1
      s.boom = i
      s.over = 'lost'
      renderAll()
      ctx.sfx('boom')
      ctx.save.clear()
      await ctx.stats.update({ inc: { played: 1, [`played_${s.level}`]: 1 }, set: { streak: 0 } })
      setTimeout(() => {
        if (s.over !== 'lost') return
        ctx.overlay({
          title: '💥 Perdu',
          text: `Vous avez touché une mine après ${format.time(s.elapsed).replace(/\.\d$/, '')}.`,
          buttons: [{ label: 'Voir la grille' }, { label: 'Rejouer', primary: true, onClick: () => newGame() }],
        })
      }, 650)
    }

    async function newGame(confirmAbandon = true) {
      if (confirmAbandon && s && s.startedAt && !s.over && s.open.some(Boolean)) {
        if (!(await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Abandonner', danger: true }))) return
        await ctx.stats.update({ inc: { played: 1, [`played_${s.level}`]: 1 }, set: { streak: 0 } })
      }
      root.querySelector(':scope > .overlay')?.remove()
      ctx.save.clear()
      s = fresh()
      paused = false
      board.classList.remove('blur')
      cursor = -1
      build()
    }

    async function changeLevel(level) {
      if (level === 'custom') {
        const c = prefs.custom
        const w = h('input', { type: 'number', min: 5, max: 50, value: c.w, style: { width: '80px' } })
        const hh = h('input', { type: 'number', min: 5, max: 30, value: c.h, style: { width: '80px' } })
        const n = h('input', { type: 'number', min: 1, max: 1400, value: c.n, style: { width: '80px' } })
        const ok = await ctx.dialog({
          title: 'Grille personnalisée',
          body: h('div', { class: 'form' }, h('label', null, 'Largeur'), w, h('label', null, 'Hauteur'), hh, h('label', null, 'Mines'), n),
          buttons: [{ label: 'Annuler', value: false }, { label: 'Valider', value: true, primary: true }],
        })
        if (!ok) { levelSel.value = s.level; return }
        const W = Math.max(5, Math.min(50, +w.value || 9)), H = Math.max(5, Math.min(30, +hh.value || 9))
        prefs.custom = { w: W, h: H, n: Math.max(1, Math.min(W * H - 9, +n.value || 10)) }
      }
      const prev = prefs.level
      prefs.level = level
      if (s && s.startedAt && !s.over && s.open.some(Boolean)) {
        if (!(await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Abandonner', danger: true }))) {
          prefs.level = prev
          levelSel.value = s.level
          return
        }
        await ctx.stats.update({ inc: { played: 1, [`played_${s.level}`]: 1 }, set: { streak: 0 } })
      }
      ctx.prefs.set(prefs)
      newGame(false)
    }

    function showScores(highlightId = null) {
      const v = SCORED.includes(prefs.level) ? prefs.level : 'beginner'
      ctx.showScores(v, {
        lowerBetter: true, fmt: (sc) => format.time(sc), title: 'Meilleurs temps', highlightId,
        variants: SCORED.map((k) => [k, LEVELS[k].label]),
      })
    }

    async function showStats() {
      const st = await ctx.stats.get().catch(() => ({}))
      const rows = SCORED.concat('custom').map((k) => {
        const p = st[`played_${k}`] || 0, w = st[`won_${k}`] || 0
        return h('tr', null, h('td', null, LEVELS[k].label), h('td', null, p), h('td', null, w),
          h('td', null, p ? `${Math.round((w / p) * 100)} %` : '—'),
          h('td', null, st.best?.[k] ? format.time(st.best[k]) : '—'))
      })
      ctx.dialog({
        title: 'Statistiques',
        body: h('div', { class: 'scores' },
          h('table', null, h('tbody', null,
            h('tr', { class: 'muted' }, ['Niveau', 'Parties', 'Victoires', 'Taux', 'Record'].map((t) => h('td', null, t))), rows)),
          h('p', { class: 'muted', style: { marginTop: '10px' } }, `Série en cours : ${st.streak || 0} · Meilleure série : ${st.streak_max || 0}`),
          h('label', { style: { display: 'flex', gap: '6px', marginTop: '10px', alignItems: 'center' } },
            h('input', { type: 'checkbox', checked: !!prefs.marks, onchange: (e) => { prefs.marks = e.target.checked; ctx.prefs.set(prefs) } }),
            'Marque « ? » au clic droit (après le drapeau)')),
        buttons: [
          { label: 'Réinitialiser', danger: true, value: 'reset' },
          { label: 'Fermer', value: true, primary: true },
        ],
      }).then(async (v) => {
        if (v === 'reset' && await ctx.confirm('Effacer statistiques et classements du Démineur ?', { ok: 'Effacer', danger: true })) {
          await ctx.call('game.reset', { what: 'scores' })
          await ctx.call('game.reset', { what: 'stats' })
          ctx.toast('Statistiques effacées', 'ok')
        }
      })
    }

    function help() {
      ctx.dialog({
        title: 'Démineur — aide',
        body: h('div', null,
          h('p', null, 'Ouvrez toutes les cases sans mine. Chaque chiffre indique le nombre de mines parmi les 8 cases voisines. Le premier clic ne tombe jamais sur une mine.'),
          h('ul', { style: { margin: '10px 0 0 18px', lineHeight: 1.8 } },
            h('li', null, 'Clic gauche : ouvrir · clic droit : drapeau'),
            h('li', null, 'Clic (gauche, droit ou milieu) sur un chiffre dont tous les drapeaux sont posés : ouvre les voisines'),
            h('li', null, h('span', { class: 'kbd' }, 'Flèches'), ' déplacer le curseur · ', h('span', { class: 'kbd' }, 'Espace'), ' ouvrir · ',
              h('span', { class: 'kbd' }, 'X'), ' drapeau · ', h('span', { class: 'kbd' }, 'C'), ' ouvrir les voisines · ', h('span', { class: 'kbd' }, 'F2'), ' nouvelle partie'),
            h('li', null, 'Manette : croix, A ouvrir, B drapeau, X voisines'))),
      })
    }

    // Pointer: left = reveal, right = mark, middle or left+right = chord. Pressed neighbours preview on chord.
    const idx = (e) => { const c = e.target.closest('.c'); return c ? +c.dataset.i : -1 }
    const clearPress = () => { for (const j of pressed) cells[j]?.classList.remove('press'); pressed = [] }
    board.addEventListener('contextmenu', (e) => e.preventDefault())
    board.addEventListener('pointerdown', (e) => {
      if (paused || s.over) return
      const i = idx(e)
      if (i < 0) return
      cursor = -1
      if (e.buttons === 3 || e.button === 1 || (s.open[i] && e.button === 0)) {
        clearPress()
        pressed = neighbors(i).filter((j) => !s.open[j] && s.mark[j] !== 1)
        for (const j of pressed) cells[j].classList.add('press')
      }
    })
    board.addEventListener('pointerup', (e) => {
      const i = idx(e)
      const wasChord = pressed.length > 0 || e.button === 1 || e.buttons !== 0
      clearPress()
      if (i < 0) return
      if (e.button === 2) act(i, wasChord && s.open[i] ? 'chord' : 'mark')
      else if (e.button === 1) act(i, 'chord')
      else if (e.button === 0) act(i, e.buttons & 2 ? 'chord' : 'reveal')
    })
    board.addEventListener('pointerleave', clearPress)

    const onKey = (e) => {
      if (e.key === 'F2') { e.preventDefault(); newGame() }
    }
    document.addEventListener('keydown', onKey)
    ctx.onCleanup(() => document.removeEventListener('keydown', onKey))

    ctx.onAction((a) => {
      if (paused) { if (a === 'a' || a === 'start') resume(); return }
      if (!s || s.over) { if (a === 'start') newGame(); return }
      const prev = cursor
      if (cursor < 0) cursor = ((s.h / 2) | 0) * s.w + ((s.w / 2) | 0)
      else {
        const x = cursor % s.w, y = (cursor / s.w) | 0
        if (a === 'left') cursor = y * s.w + (x - 1 + s.w) % s.w
        if (a === 'right') cursor = y * s.w + (x + 1) % s.w
        if (a === 'up') cursor = ((y - 1 + s.h) % s.h) * s.w + x
        if (a === 'down') cursor = ((y + 1) % s.h) * s.w + x
        if (a === 'a') act(cursor, 'reveal')
        if (a === 'b') act(cursor, 'mark')
        if (a === 'x') act(cursor, 'chord')
      }
      if (prev >= 0 && prev !== cursor) renderCell(prev)
      renderCell(cursor)
    })

    function pause() {
      if (paused || !s || s.over || !s.startedAt) return
      s.elapsed = elapsed()
      paused = true
      board.classList.add('blur')
      persist(true)
      ctx.overlay({ title: '⏸ Pause', text: 'Le chronomètre est arrêté.', buttons: [{ label: 'Reprendre', primary: true, onClick: resume }] })
    }
    function resume() {
      if (!paused) return
      paused = false
      timerStart = performance.now()
      board.classList.remove('blur')
      root.querySelector(':scope > .overlay')?.remove()
    }

    const saved = await ctx.save.get().catch(() => null)
    if (saved && saved.w && saved.open?.length === saved.w * saved.h) {
      s = { ...saved, over: null }
      prefs.level = s.level
      build()
      timerStart = performance.now()
      if (s.startedAt) {
        paused = true
        board.classList.add('blur')
        ctx.overlay({ title: 'Partie en cours', text: `${LEVELS[s.level]?.label || ''} · ${format.time(s.elapsed).replace(/\.\d$/, '')}`,
          buttons: [{ label: 'Nouvelle partie', onClick: () => newGame(false).then(() => ctx.stats.update({ inc: { played: 1 }, set: { streak: 0 } })) },
            { label: 'Reprendre', primary: true, onClick: resume }] })
      }
    } else {
      s = fresh()
      build()
    }

    return {
      pause,
      destroy() { clearTimeout(saveTimer); if (s && !s.over && s.startedAt) ctx.save.set({ ...s, elapsed: elapsed() }) },
    }
  },
}
