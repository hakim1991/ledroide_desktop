import { generate, PEERS, ROW, COL, BOX } from './sudoku-core.js'

const LEVELS = [['facile', 'Facile'], ['moyen', 'Moyen'], ['difficile', 'Difficile'], ['expert', 'Expert']]
const LEVEL_NAME = Object.fromEntries(LEVELS)
const HINT_PENALTY = 30000

const CSS = `
.g-sudoku { flex: 1; display: flex; align-items: center; justify-content: center; gap: 22px; padding: 12px; min-height: 0; }
.g-sudoku.col { flex-direction: column; gap: 10px; }
.g-sudoku .board { --sz: 400px; width: var(--sz); height: var(--sz); display: grid; grid-template-columns: repeat(9, 1fr);
  grid-template-rows: repeat(9, 1fr); background: var(--surface); border: 2px solid #5b5b66; border-radius: 6px; overflow: hidden;
  position: relative; flex-shrink: 0; box-shadow: 0 8px 30px rgba(0,0,0,.35); }
.g-sudoku .cell { position: relative; display: grid; place-items: center; border-right: 1px solid #2c2c33; border-bottom: 1px solid #2c2c33;
  font-size: calc(var(--sz) / 9 * .56); font-weight: 500; color: var(--accent-h); cursor: pointer; transition: background .08s; }
.g-sudoku .cell.br { border-right: 2px solid #5b5b66; }
.g-sudoku .cell.bb { border-bottom: 2px solid #5b5b66; }
.g-sudoku .cell:nth-child(9n) { border-right: none; }
.g-sudoku .cell:nth-child(n+73) { border-bottom: none; }
.g-sudoku .cell.given { color: var(--text); font-weight: 700; }
.g-sudoku .cell.hinted { color: #67e8f9; }
.g-sudoku .cell.hinted::after { content: ''; position: absolute; top: 3px; right: 3px; width: 5px; height: 5px; border-radius: 50%; background: #67e8f9; opacity: .7; }
.g-sudoku .cell.hl { background: #1d1d26; }
.g-sudoku .cell.same { background: #272a45; }
.g-sudoku .cell.sel { background: #343a6e; box-shadow: inset 0 0 0 2px var(--accent-h); }
.g-sudoku .cell.bad { color: #f87171; background: rgba(239,68,68,.16); }
.g-sudoku .cell.bad.sel { background: rgba(239,68,68,.3); }
.g-sudoku .cell.pop .v { animation: g-sudoku-pop .18s ease-out; }
.g-sudoku .cell.flash { animation: g-sudoku-flash .6s ease-out; }
.g-sudoku .notes { position: absolute; inset: 2px; display: grid; grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(3, 1fr);
  font-size: calc(var(--sz) / 9 * .24); color: var(--muted); font-weight: 500; }
.g-sudoku .notes span { display: grid; place-items: center; line-height: 1; }
.g-sudoku .notes span.m { color: #fde68a; font-weight: 700; }
.g-sudoku .board.veiled .cell > *, .g-sudoku .board.veiled .cell::after { visibility: hidden; }
.g-sudoku .board.veiled .cell { background: var(--surface) !important; box-shadow: none !important; }
.g-sudoku .board.won .cell { animation: g-sudoku-win .5s ease-out both; }
.g-sudoku .side { display: flex; flex-direction: column; gap: 10px; width: 200px; flex-shrink: 0; }
.g-sudoku.col .side { width: var(--sz); flex-direction: row; flex-wrap: wrap; justify-content: center; }
.g-sudoku .pad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.g-sudoku.col .pad { grid-template-columns: repeat(9, 1fr); width: 100%; }
.g-sudoku .pad button { height: 52px; font-size: 22px; font-weight: 600; position: relative; padding: 0; }
.g-sudoku.col .pad button { height: 40px; font-size: 18px; }
.g-sudoku .pad button small { position: absolute; bottom: 2px; right: 5px; font-size: 10px; color: var(--muted); font-weight: 400; }
.g-sudoku .pad button.cur { border-color: var(--accent); background: #24264a; }
.g-sudoku .pad button.done { opacity: .35; }
.g-sudoku .pad.notes-mode button { color: #fde68a; font-size: 16px; font-weight: 500; }
.g-sudoku .tools { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.g-sudoku.col .tools { grid-template-columns: repeat(6, auto); }
.g-sudoku .tools button { display: flex; flex-direction: column; align-items: center; gap: 1px; padding: 6px 4px; font-size: 11px; }
.g-sudoku .tools button b { font-size: 16px; font-weight: 400; line-height: 1.1; }
.g-sudoku .opts { display: flex; flex-direction: column; gap: 4px; color: var(--muted); font-size: 12px; }
.g-sudoku.col .opts { flex-direction: row; gap: 12px; }
.g-sudoku .opts label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
.g-sudoku .help-keys td { padding: 2px 8px 2px 0; vertical-align: top; }
@keyframes g-sudoku-pop { from { transform: scale(.55); opacity: .3 } }
@keyframes g-sudoku-flash { 0% { background: rgba(103,232,249,.35) } }
@keyframes g-sudoku-win { 50% { background: rgba(99,102,241,.35) } }
`

export default {
  async mount(root, ctx) {
    const { h, format } = ctx
    let prefs = { level: 'facile', check: false, highlight: true }
    try { prefs = { ...prefs, ...((await ctx.prefs.get()) || {}) } } catch { /* defaults */ }
    if (!LEVEL_NAME[prefs.level]) prefs.level = 'facile'

    let st = null
    let paused = false, won = false, busy = false, alive = true
    let lastTick = performance.now()
    let saveTimer = 0, closeOverlay = null

    const board = h('div', { class: 'board' })
    const cells = []
    for (let i = 0; i < 81; i++) {
      const c = h('div', { class: `cell${COL[i] === 2 || COL[i] === 5 ? ' br' : ''}${ROW[i] === 2 || ROW[i] === 5 ? ' bb' : ''}` })
      c.addEventListener('pointerdown', (e) => { e.preventDefault(); if (st && !paused && !won) { st.sel = i; render() } })
      cells.push(c)
      board.append(c)
    }
    const noFocus = (e) => e.preventDefault()
    const padBtns = []
    const pad = h('div', { class: 'pad' })
    for (let d = 1; d <= 9; d++) {
      const b = h('button', { onmousedown: noFocus, onclick: () => input(d), title: `Chiffre ${d}` }, String(d), h('small'))
      padBtns.push(b)
      pad.append(b)
    }
    const tool = (icon, label, fn, title) => h('button', { onmousedown: noFocus, onclick: fn, title }, h('b', null, icon), label)
    const undoBtn = tool('↶', 'Annuler', () => undo(), 'Annuler (Ctrl+Z)')
    const redoBtn = tool('↷', 'Refaire', () => redo(), 'Refaire (Ctrl+Y)')
    const eraseBtn = tool('⌫', 'Effacer', () => erase(), 'Effacer (Suppr)')
    const notesBtn = tool('✎', 'Notes', () => toggleNotes(), 'Mode notes (N)')
    const hintBtn = tool('💡', 'Indice', () => hint(), `Révèle une case (+${HINT_PENALTY / 1000} s, touche H)`)
    const autoBtn = tool('⋯', 'Auto-notes', () => autoNotes(), 'Remplir toutes les notes possibles')
    const opt = (key, label) => {
      const inp = h('input', { type: 'checkbox', onchange: () => { prefs[key] = inp.checked; inp.blur(); ctx.prefs.set(prefs); render() } })
      inp.checked = !!prefs[key]
      return h('label', null, inp, label)
    }
    const side = h('div', { class: 'side' }, pad,
      h('div', { class: 'tools' }, undoBtn, redoBtn, eraseBtn, notesBtn, autoBtn, hintBtn),
      h('div', { class: 'opts' }, opt('check', 'Vérifier avec la solution'), opt('highlight', 'Surligner ligne / colonne / bloc')))
    const wrap = h('div', { class: 'g-sudoku' }, board, side)
    root.append(h('style', null, CSS), wrap)

    const timePill = h('span', { class: 'pill', title: 'Temps' })
    const levelPill = h('span', { class: 'pill' })
    const hintPill = h('span', { class: 'pill', title: 'Indices utilisés' })
    ctx.setStatus(levelPill, hintPill, timePill)

    const levelSeg = h('div', { class: 'seg' }, LEVELS.map(([id, name]) =>
      h('button', { 'data-level': id, onclick: () => newGame(id) }, name)))
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => newGame(prefs.level) }, 'Nouvelle partie'),
      levelSeg,
      h('button', { class: 'icon', title: 'Meilleurs temps', onclick: () => showScores() }, '🏆'),
      h('button', { class: 'icon', title: 'Statistiques', onclick: () => showStats() }, '📊'),
      h('button', { class: 'icon', title: 'Aide', onclick: () => showHelp() }, '❔'))

    const fit = () => {
      const W = root.clientWidth - 24, H = root.clientHeight - 24
      const rowSz = Math.min(H, W - 200 - 22)
      const colSz = Math.min(W, H - 150)
      const useCol = colSz > rowSz + 20
      wrap.classList.toggle('col', useCol)
      const sz = Math.max(220, Math.floor(useCol ? colSz : rowSz))
      wrap.style.setProperty('--sz', `${sz}px`)
      board.style.setProperty('--sz', `${sz}px`)
    }
    const ro = new ResizeObserver(fit)
    ro.observe(root)
    fit()

    const peersOf = (i) => PEERS[i]
    const isLocked = (i) => st.puzzle[i] || st.hinted[i]
    const moved = () => st && (st.undo.length > 0 || st.hints > 0)

    function conflicts() {
      const bad = new Uint8Array(81)
      for (let i = 0; i < 81; i++) {
        const v = st.values[i]
        if (!v) continue
        if (prefs.check && v !== st.solution[i]) bad[i] = 1
        for (const p of peersOf(i)) if (st.values[p] === v) { if (!isLocked(i)) bad[i] = 1 }
      }
      return bad
    }

    function render(popAt = -1) {
      if (!st) return
      const bad = conflicts()
      const sel = st.sel
      const selV = st.values[sel] || 0
      const focusDigit = selV || st.digit
      for (let i = 0; i < 81; i++) {
        const c = cells[i]
        const v = st.values[i]
        const peer = prefs.highlight && i !== sel && (ROW[i] === ROW[sel] || COL[i] === COL[sel] || BOX[i] === BOX[sel])
        c.className = `cell${COL[i] === 2 || COL[i] === 5 ? ' br' : ''}${ROW[i] === 2 || ROW[i] === 5 ? ' bb' : ''}` +
          (st.puzzle[i] ? ' given' : '') + (st.hinted[i] ? ' hinted' : '') + (peer ? ' hl' : '') +
          (v && v === focusDigit && i !== sel ? ' same' : '') + (i === sel ? ' sel' : '') + (bad[i] ? ' bad' : '') +
          (i === popAt ? ' pop' : '')
        const key = v ? `v${v}` : st.notes[i] ? `n${st.notes[i]}:${focusDigit}` : ''
        if (c.dataset.k !== key) {
          c.dataset.k = key
          if (v) c.replaceChildren(h('span', { class: 'v' }, String(v)))
          else if (st.notes[i]) {
            const n = h('div', { class: 'notes' })
            for (let d = 1; d <= 9; d++) n.append(h('span', { class: d === focusDigit && (st.notes[i] >> (d - 1)) & 1 ? 'm' : '' }, (st.notes[i] >> (d - 1)) & 1 ? String(d) : ''))
            c.replaceChildren(n)
          } else c.replaceChildren()
        }
      }
      const counts = new Array(10).fill(0)
      for (let i = 0; i < 81; i++) if (st.values[i]) counts[st.values[i]]++
      padBtns.forEach((b, k) => {
        const d = k + 1, left = 9 - counts[d]
        b.classList.toggle('cur', d === st.digit)
        b.classList.toggle('done', left <= 0)
        b.querySelector('small').textContent = left > 0 ? String(left) : '✓'
      })
      pad.classList.toggle('notes-mode', st.noteMode)
      notesBtn.classList.toggle('on', st.noteMode)
      undoBtn.disabled = !st.undo.length
      redoBtn.disabled = !st.redo.length
      for (const b of levelSeg.children) b.classList.toggle('on', b.dataset.level === st.level)
      levelPill.textContent = LEVEL_NAME[st.level]
      hintPill.textContent = `💡 ${st.hints}`
      updateTime()
    }

    function updateTime() {
      if (st) timePill.textContent = `⏱ ${format.time(st.elapsed).replace(/\.\d$/, '')}`
    }

    function tick() {
      const now = performance.now()
      if (st && !paused && !won && !busy) st.elapsed += now - lastTick
      lastTick = now
      updateTime()
    }
    const timer = setInterval(tick, 250)
    const autosave = setInterval(() => { if (st && !paused && !won && moved()) persist() }, 15000)

    function persist() {
      clearTimeout(saveTimer)
      if (!st || won) return
      tick()
      return ctx.save.set({ v: 1, ...st })
    }
    const saveSoon = () => { clearTimeout(saveTimer); saveTimer = setTimeout(persist, 500) }

    function commit(changes, popAt = -1) {
      if (!changes.length) return
      for (const [i, , , v, n] of changes) { st.values[i] = v; st.notes[i] = n }
      st.undo.push(changes)
      if (st.undo.length > 5000) st.undo.shift()
      st.redo = []
      render(popAt)
      saveSoon()
    }

    function placeChanges(i, d) {
      const ch = [[i, st.values[i], st.notes[i], d, 0]]
      const bit = 1 << (d - 1)
      for (const p of peersOf(i)) if (!st.values[p] && (st.notes[p] & bit)) ch.push([p, 0, st.notes[p], 0, st.notes[p] & ~bit])
      return ch
    }

    function input(d) {
      if (!st || paused || won || busy) return
      st.digit = d
      const i = st.sel
      if (isLocked(i)) { render(); return }
      if (st.noteMode) {
        if (st.values[i]) { render(); return }
        commit([[i, 0, st.notes[i], 0, st.notes[i] ^ (1 << (d - 1))]])
        ctx.sfx('tick')
        return
      }
      if (st.values[i] === d) { commit([[i, d, 0, 0, 0]]); ctx.sfx('move'); return }
      commit(placeChanges(i, d), i)
      if (prefs.check && d !== st.solution[i]) { st.mistakes++; ctx.sfx('error') }
      else if (peersOf(i).some((p) => st.values[p] === d)) ctx.sfx('error')
      else ctx.sfx('click')
      checkDone()
    }

    function erase() {
      if (!st || paused || won || busy) return
      const i = st.sel
      if (isLocked(i)) return
      if (st.values[i]) commit([[i, st.values[i], st.notes[i], 0, 0]])
      else if (st.notes[i]) commit([[i, 0, st.notes[i], 0, 0]])
      else return
      ctx.sfx('move')
    }

    function autoNotes() {
      if (!st || paused || won || busy) return
      const ch = []
      for (let i = 0; i < 81; i++) {
        if (st.values[i]) continue
        let m = 511
        for (const p of peersOf(i)) if (st.values[p]) m &= ~(1 << (st.values[p] - 1))
        if (m !== st.notes[i]) ch.push([i, 0, st.notes[i], 0, m])
      }
      if (ch.length) { commit(ch); ctx.sfx('pop') }
    }

    function applyHistory(from, to, dir) {
      if (!st || paused || won || busy || !from.length) return
      const changes = from.pop()
      for (const [i, v0, n0, v1, n1] of changes) {
        if (isLocked(i)) continue
        st.values[i] = dir < 0 ? v0 : v1
        st.notes[i] = dir < 0 ? n0 : n1
      }
      to.push(changes)
      const main = changes[0][0]
      if (!isLocked(main)) st.sel = main
      ctx.sfx('move')
      render()
      saveSoon()
      if (dir > 0) checkDone()
    }
    const undo = () => st && applyHistory(st.undo, st.redo, -1)
    const redo = () => st && applyHistory(st.redo, st.undo, 1)

    function toggleNotes() {
      if (!st || won) return
      st.noteMode = !st.noteMode
      ctx.sfx('tick')
      render()
    }

    function hint() {
      if (!st || paused || won || busy) return
      let i = st.sel
      if (isLocked(i) || st.values[i] === st.solution[i]) {
        const wrong = [], empty = []
        for (let k = 0; k < 81; k++) {
          if (isLocked(k)) continue
          if (!st.values[k]) empty.push(k)
          else if (st.values[k] !== st.solution[k]) wrong.push(k)
        }
        const pool = wrong.length ? wrong : empty
        if (!pool.length) return
        i = pool[Math.floor(Math.random() * pool.length)]
      }
      const d = st.solution[i]
      const bit = 1 << (d - 1)
      st.values[i] = d; st.notes[i] = 0; st.hinted[i] = 1
      for (const p of peersOf(i)) if (!st.values[p]) st.notes[p] &= ~bit
      st.hints++
      st.sel = i; st.digit = d
      ctx.sfx('pop')
      cells[i].classList.remove('flash')
      render(i)
      void cells[i].offsetWidth
      cells[i].classList.add('flash')
      saveSoon()
      checkDone()
    }

    function move(dr, dc) {
      if (!st || paused || won) return
      const r = (ROW[st.sel] + dr + 9) % 9, c = (COL[st.sel] + dc + 9) % 9
      st.sel = r * 9 + c
      render()
    }

    function checkDone() {
      for (let i = 0; i < 81; i++) if (!st.values[i]) return
      for (let i = 0; i < 81; i++) if (st.values[i] !== st.solution[i]) {
        ctx.toast('La grille est pleine mais contient des erreurs.', 'err')
        return
      }
      victory()
    }

    async function victory() {
      tick()
      won = true
      clearTimeout(saveTimer)
      board.classList.add('won')
      ctx.sfx('win')
      ctx.save.clear()
      const level = st.level
      const timeMs = Math.round(st.elapsed)
      const score = timeMs + st.hints * HINT_PENALTY
      let res = null
      try {
        res = await ctx.scores.submit(level, score, { lowerBetter: true, meta: { time: timeMs, hints: st.hints, mistakes: st.mistakes } })
      } catch (e) { ctx.toast(e.message, 'err') }
      let stats = {}
      try {
        stats = await ctx.stats.update({
          inc: { played: 1, won: 1, [`won_${level}`]: 1, hints: st.hints },
          min: { [`best_${level}`]: score },
        })
        let top = null
        for (const [id] of LEVELS) if (stats[`best_${id}`] != null) top = id
        if (top) await ctx.stats.update({ set: { hub_best: `${format.time(stats[`best_${top}`]).replace(/\.\d$/, '')} (${LEVEL_NAME[top]})` } })
      } catch { /* stats are best effort */ }
      if (!alive) return
      const lines = [`Niveau ${LEVEL_NAME[level]} · temps ${format.time(timeMs)}`]
      if (st.hints) lines.push(`${st.hints} indice${st.hints > 1 ? 's' : ''} : +${format.time(st.hints * HINT_PENALTY)} → ${format.time(score)}`)
      if (res) lines.push(res.record ? '🏆 Nouveau record !' : `Rang ${res.rank} sur ${res.total}`)
      closeOverlay = ctx.overlay({
        title: 'Grille résolue !',
        text: lines.join('\n'),
        buttons: [
          { label: 'Nouvelle partie', primary: true, onClick: () => newGame(level, true) },
          { label: 'Meilleurs temps', onClick: () => { showScores(level, res?.id).then(() => { if (won && alive) victoryAgain(lines, level) }) } },
          { label: 'Voir la grille', onClick: () => {} },
        ],
      })
    }
    const victoryAgain = (lines, level) => {
      closeOverlay = ctx.overlay({ title: 'Grille résolue !', text: lines.join('\n'),
        buttons: [{ label: 'Nouvelle partie', primary: true, onClick: () => newGame(level, true) }, { label: 'Voir la grille', onClick: () => {} }] })
    }

    async function newGame(level, force = false) {
      if (busy) return
      if (!force && st && !won && moved()) {
        const ok = await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Abandonner', danger: true })
        if (!ok || !alive) return
        ctx.stats.update({ inc: { played: 1, [`abandon_${st.level}`]: 1 } })
      }
      prefs.level = level
      ctx.prefs.set(prefs)
      busy = true
      closeOverlay?.()
      closeOverlay = ctx.overlay({ title: 'Génération…', text: `Grille ${LEVEL_NAME[level]} à solution unique` })
      await new Promise((r) => setTimeout(r, 30))
      const g = generate(level)
      if (!alive) return
      st = {
        level, puzzle: g.puzzle, solution: g.solution, values: g.puzzle.slice(), notes: new Array(81).fill(0),
        hinted: new Array(81).fill(0), undo: [], redo: [], elapsed: 0, hints: 0, mistakes: 0,
        sel: 40, digit: 1, noteMode: false, clues: g.clues,
      }
      won = false; paused = false; busy = false
      board.classList.remove('won', 'veiled')
      closeOverlay?.()
      closeOverlay = null
      lastTick = performance.now()
      render()
      ctx.save.set({ v: 1, ...st })
    }

    function setPaused(p) {
      if (!st || won || busy || paused === p) return
      tick()
      paused = p
      board.classList.toggle('veiled', p)
      if (p) {
        persist()
        closeOverlay = ctx.overlay({ title: 'Pause', text: 'La grille est masquée pendant la pause.',
          buttons: [{ label: 'Reprendre', primary: true, onClick: () => setPaused(false) }] })
      } else {
        closeOverlay?.()
        closeOverlay = null
        lastTick = performance.now()
      }
      render()
    }

    async function showScores(level = st?.level || prefs.level, highlightId = null) {
      await ctx.showScores(level, {
        lowerBetter: true, title: 'Meilleurs temps', highlightId, variants: LEVELS,
        fmt: (s, m) => `${format.time(s)}${m?.hints ? ` (💡${m.hints})` : ''}`,
      })
    }

    async function showStats() {
      let s = {}
      try { s = (await ctx.stats.get()) || {} } catch (e) { ctx.toast(e.message, 'err') }
      const rows = LEVELS.map(([id, name]) => h('tr', null, h('td', null, name), h('td', null, String(s[`won_${id}`] || 0)),
        h('td', null, s[`best_${id}`] != null ? format.time(s[`best_${id}`]) : '—'), h('td', null, String(s[`abandon_${id}`] || 0))))
      await ctx.dialog({
        title: 'Statistiques — Sudoku',
        body: h('div', null,
          h('p', null, `Parties terminées ou abandonnées : ${s.played || 0} · grilles résolues : ${s.won || 0} · indices utilisés : ${s.hints || 0}`),
          h('table', { class: 'help-keys', style: { marginTop: '10px', width: '100%' } },
            h('thead', null, h('tr', { class: 'muted' }, h('td', null, 'Niveau'), h('td', null, 'Résolues'), h('td', null, 'Meilleur temps'), h('td', null, 'Abandons'))),
            h('tbody', null, rows))),
      })
    }

    function showHelp() {
      const k = (t) => h('span', { class: 'kbd' }, t)
      ctx.dialog({
        title: 'Sudoku — aide',
        body: h('div', null,
          h('p', null, 'Remplissez la grille pour que chaque ligne, chaque colonne et chaque bloc 3×3 contienne les chiffres 1 à 9 une seule fois. Chaque grille générée a une solution unique.'),
          h('p', { style: { margin: '8px 0' } }, `Niveaux : Facile (≈38 indices, singletons), Moyen (≈30 indices), Difficile (paires, candidats verrouillés), Expert (techniques avancées). Un indice révèle une case et ajoute ${HINT_PENALTY / 1000} s au temps.`),
          h('table', { class: 'help-keys' }, h('tbody', null,
            h('tr', null, h('td', null, k('1'), '…', k('9')), h('td', null, 'Placer un chiffre (ou une note en mode notes)')),
            h('tr', null, h('td', null, k('Suppr'), ' ', k('⌫'), ' ', k('0')), h('td', null, 'Effacer la case')),
            h('tr', null, h('td', null, k('N')), h('td', null, 'Mode notes')),
            h('tr', null, h('td', null, k('Flèches')), h('td', null, 'Déplacer la sélection')),
            h('tr', null, h('td', null, k('Ctrl+Z'), ' ', k('Ctrl+Y')), h('td', null, 'Annuler / refaire')),
            h('tr', null, h('td', null, k('H')), h('td', null, 'Indice')),
            h('tr', null, h('td', null, k('Espace')), h('td', null, 'Placer le chiffre courant (surligné sur le pavé)')),
            h('tr', null, h('td', null, k('P'), ' ', k('Échap')), h('td', null, 'Pause')),
            h('tr', null, h('td', null, 'Manette'), h('td', null, 'Croix : déplacer · A : placer · B : effacer · X : notes · L/R : chiffre courant · Start : pause'))))),
      })
    }

    ctx.onAction((a) => {
      if (!st || busy) return
      if (a === 'start') { if (!won) setPaused(!paused); return }
      if (paused || won) return
      if (a === 'up') move(-1, 0)
      else if (a === 'down') move(1, 0)
      else if (a === 'left') move(0, -1)
      else if (a === 'right') move(0, 1)
      else if (a === 'a') input(st.digit)
      else if (a === 'b') erase()
      else if (a === 'x') toggleNotes()
      else if (a === 'l' || a === 'r') { st.digit = ((st.digit - 1 + (a === 'r' ? 1 : 8)) % 9) + 1; render() }
    })

    const onKey = (e) => {
      if (document.querySelector('.dlg-back')) return
      const t = e.target
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && t.type !== 'checkbox')) return
      if (!st || busy || paused || won) return
      if (e.ctrlKey || e.metaKey) {
        if (e.code === 'KeyZ' && !e.shiftKey) { e.preventDefault(); undo() }
        else if (e.code === 'KeyY' || (e.code === 'KeyZ' && e.shiftKey)) { e.preventDefault(); redo() }
        return
      }
      if (e.altKey) return
      const m = /^(?:Digit|Numpad)([0-9])$/.exec(e.code)
      if (m) { e.preventDefault(); if (m[1] === '0') erase(); else input(+m[1]) }
      else if (e.code === 'Delete') { e.preventDefault(); erase() }
      else if (e.code === 'KeyN') { e.preventDefault(); toggleNotes() }
      else if (e.code === 'KeyH') { e.preventDefault(); hint() }
    }
    document.addEventListener('keydown', onKey)

    let saved = null
    try { saved = await ctx.save.get() } catch (e) { ctx.toast(e.message, 'err') }
    if (!alive) return {}
    if (saved && saved.v === 1 && Array.isArray(saved.puzzle) && saved.puzzle.length === 81 && saved.values?.length === 81) {
      st = { undo: [], redo: [], hints: 0, mistakes: 0, digit: 1, sel: 40, noteMode: false, ...saved }
      lastTick = performance.now()
      render()
      ctx.toast('Partie reprise')
    } else await newGame(prefs.level, true)

    return {
      pause() { setPaused(true) },
      destroy() {
        alive = false
        if (st && !won && !busy) persist()
        clearInterval(timer)
        clearInterval(autosave)
        clearTimeout(saveTimer)
        ro.disconnect()
        document.removeEventListener('keydown', onKey)
      },
    }
  },
}
