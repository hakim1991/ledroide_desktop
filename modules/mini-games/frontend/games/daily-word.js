import { SOLUTIONS, ACCEPTED } from './daily-word-words.js'

const ROWS = 6
const LEN = 5
const EPOCH = Date.UTC(2024, 0, 1)
const KB_ROWS = ['AZERTYUIOP', 'QSDFGHJKLM', '>WXCVBN<']
const RANK = { absent: 1, present: 2, correct: 3 }

export const normalize = (s) => String(s).replace(/œ/gi, 'OE').replace(/æ/gi, 'AE')
  .normalize('NFD').replace(/[̀-ͯ]/g, '').toUpperCase()

export function dayIndex(d = new Date()) {
  return Math.round((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - EPOCH) / 86400000)
}

// Two passes so that repeated letters are only marked "present" as many times as they remain unmatched.
export function scoreGuess(guess, answer) {
  const res = Array(LEN).fill('absent')
  const left = {}
  for (let i = 0; i < LEN; i++) {
    if (guess[i] === answer[i]) res[i] = 'correct'
    else left[answer[i]] = (left[answer[i]] || 0) + 1
  }
  for (let i = 0; i < LEN; i++) {
    if (res[i] !== 'correct' && left[guess[i]] > 0) { res[i] = 'present'; left[guess[i]]-- }
  }
  return res
}

function hardViolation(guess, history) {
  for (const { word, res } of history) {
    for (let i = 0; i < LEN; i++) {
      if (res[i] === 'correct' && guess[i] !== word[i]) return `La lettre n°${i + 1} doit être ${word[i]}`
    }
    const need = {}
    for (let i = 0; i < LEN; i++) if (res[i] !== 'absent') need[word[i]] = (need[word[i]] || 0) + 1
    for (const [ch, n] of Object.entries(need)) {
      if ([...guess].filter((c) => c === ch).length < n) return `Le mot doit contenir ${n > 1 ? `${n} × ` : ''}${ch}`
    }
  }
  return null
}

const CSS = `
.g-daily-word { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: safe center; gap: 14px;
  padding: 12px 16px; position: relative; min-height: 0;
  --ok-c: #2f9e5b; --pr-c: #c9a227; --ab-c: #34343b; --tile: 54px; --key: 44px; }
.g-daily-word.cb { --ok-c: #f0782f; --pr-c: #3c9ae8; }
.g-daily-word .board { display: grid; grid-template-rows: repeat(${ROWS}, var(--tile)); gap: 6px; }
.g-daily-word .row { display: grid; grid-template-columns: repeat(${LEN}, var(--tile)); gap: 6px; }
.g-daily-word .tile { display: grid; place-items: center; border: 2px solid var(--border); border-radius: 8px;
  font-weight: 800; font-size: calc(var(--tile) * .5); background: var(--surface); color: var(--text);
  text-transform: uppercase; transition: border-color .1s; }
.g-daily-word .tile.filled { border-color: #52525b; }
.g-daily-word .tile.correct, .g-daily-word .key.correct { background: var(--ok-c); border-color: var(--ok-c); color: #fff; }
.g-daily-word .tile.present, .g-daily-word .key.present { background: var(--pr-c); border-color: var(--pr-c); color: #fff; }
.g-daily-word .tile.absent { background: var(--ab-c); border-color: var(--ab-c); color: #a1a1aa; }
.g-daily-word .key.absent { background: #1c1c20; border-color: #1c1c20; color: #52525b; }
.g-daily-word .tile.pop { animation: dwPop .12s ease; }
.g-daily-word .tile.flip { animation: dwFlip .5s ease; }
.g-daily-word .tile.bounce { animation: dwBounce .6s ease; }
.g-daily-word .row.shake { animation: dwShake .45s ease; }
.g-daily-word .row.cur .tile:not(.filled) { border-color: #3a3a42; }
@keyframes dwPop { 50% { transform: scale(1.12) } }
@keyframes dwFlip { 0% { transform: rotateX(0) } 50% { transform: rotateX(90deg) } 100% { transform: rotateX(0) } }
@keyframes dwBounce { 0%, 100% { transform: translateY(0) } 40% { transform: translateY(-30%) } 70% { transform: translateY(6%) } }
@keyframes dwShake { 10%, 90% { transform: translateX(-2px) } 20%, 80% { transform: translateX(4px) }
  30%, 50%, 70% { transform: translateX(-7px) } 40%, 60% { transform: translateX(7px) } }
.g-daily-word .kb { display: flex; flex-direction: column; gap: 6px; align-items: center; width: 100%; }
.g-daily-word .kb-row { display: flex; gap: 5px; justify-content: center; width: 100%; }
.g-daily-word .key { height: var(--key); min-width: 0; flex: 0 1 calc(var(--key) * .82); padding: 0; border-radius: 6px;
  font-weight: 700; font-size: calc(var(--key) * .36); background: #3f3f46; border-color: #3f3f46; transition: background .2s; }
.g-daily-word .key.wide { flex-basis: calc(var(--key) * 1.35); font-size: calc(var(--key) * .3); }
.g-daily-word .key:hover:not(:disabled) { filter: brightness(1.15); background: #4b4b53; }
.g-daily-word .key.correct:hover, .g-daily-word .key.present:hover { background: inherit; }
.g-daily-word .msg { position: absolute; top: 10px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg);
  padding: 7px 14px; border-radius: 8px; font-weight: 600; pointer-events: none; z-index: 30; animation: fade .15s; }
.g-daily-word .result { display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 18px; max-width: 460px; }
.g-daily-word .result .big { font-size: 16px; font-weight: 700; }
.g-daily-word .result .word { letter-spacing: .18em; font-weight: 800; font-size: 18px; color: var(--accent-h); }
.g-daily-word .result .btns { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
.g-daily-word .cd { font-variant-numeric: tabular-nums; font-weight: 700; }
.dw-stats .nums { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; text-align: center; margin-bottom: 14px; }
.dw-stats .nums b { display: block; font-size: 22px; }
.dw-stats .nums span { font-size: 11px; color: var(--muted); }
.dw-stats h4 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 6px 0 8px; }
.dw-stats .bar { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
.dw-stats .bar i { font-style: normal; width: 12px; text-align: right; }
.dw-stats .bar div { background: #3f3f46; color: #fff; font-weight: 600; padding: 2px 7px; border-radius: 4px; text-align: right;
  min-width: 26px; transition: width .4s; }
.dw-stats .bar div.hl { background: var(--accent); }
.dw-stats .free { margin-top: 14px; color: var(--muted); }
.dw-opts label { display: flex; gap: 8px; align-items: flex-start; margin: 8px 0; cursor: pointer; }
.dw-opts small { display: block; color: var(--muted); }
`

const fmtCountdown = () => {
  const now = new Date()
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  const s = Math.max(0, Math.floor((next - now) / 1000))
  return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map((n) => String(n).padStart(2, '0')).join(':')
}

export default {
  async mount(root, ctx) {
    const { h } = ctx
    const order = ctx.shuffle([...SOLUTIONS], ctx.seeded('mot-du-jour-v1'))
    const dailyAnswer = (day) => order[((day % order.length) + order.length) % order.length]
    const randomAnswer = () => SOLUTIONS[Math.floor(Math.random() * SOLUTIONS.length)]

    let prefs = { mode: 'daily', hard: false, colorblind: false, anyWord: false, ...(await ctx.prefs.get().catch(() => ({}))) }
    let saved = (await ctx.save.get().catch(() => null)) || {}
    let stats = (await ctx.stats.get().catch(() => ({}))) || {}

    const mkSlot = (mode, answer, guesses = [], extra = {}) => {
      const s = { mode, answer, rows: [], done: false, won: false, hard: !!prefs.hard, ...extra }
      for (const g of guesses) {
        s.rows.push({ word: g, res: scoreGuess(g, answer) })
        if (g === answer) { s.done = true; s.won = true }
      }
      if (s.rows.length >= ROWS) s.done = true
      return s
    }
    const loadDaily = () => {
      const day = dayIndex()
      const fin = prefs.daily
      if (fin && fin.day === day) return mkSlot('daily', dailyAnswer(day), fin.guesses, { day, hard: !!fin.hard, fresh: false })
      const sv = saved.daily
      if (sv && sv.day === day) return mkSlot('daily', dailyAnswer(day), sv.guesses, { day, hard: !!sv.hard })
      return mkSlot('daily', dailyAnswer(day), [], { day })
    }
    const loadFree = () => {
      const sv = saved.free
      if (sv && /^[A-Z]{5}$/.test(sv.answer || '')) return mkSlot('free', sv.answer, sv.guesses || [], { hard: !!sv.hard })
      return mkSlot('free', randomAnswer())
    }
    const slots = { daily: loadDaily(), free: loadFree() }
    let mode = prefs.mode === 'free' ? 'free' : 'daily'
    let cur = ''
    let busy = false
    let msgTimer = 0

    const slot = () => slots[mode]

    const persist = () => {
      const s = {}
      const d = slots.daily, f = slots.free
      if (!d.done && d.rows.length) s.daily = { day: d.day, guesses: d.rows.map((r) => r.word), hard: d.hard }
      if (!f.done && f.rows.length) s.free = { answer: f.answer, guesses: f.rows.map((r) => r.word), hard: f.hard }
      saved = s
      return Object.keys(s).length ? ctx.save.set(s) : ctx.save.clear()
    }
    const savePrefs = () => ctx.prefs.set(prefs)

    // ── DOM ──
    const wrap = h('div', { class: 'g-daily-word' })
    const board = h('div', { class: 'board' })
    const bottom = h('div', { style: { width: '100%', display: 'flex', justifyContent: 'center' } })
    const tiles = []
    for (let r = 0; r < ROWS; r++) {
      const row = h('div', { class: 'row' })
      tiles.push([])
      for (let c = 0; c < LEN; c++) { const t = h('div', { class: 'tile' }); tiles[r].push(t); row.append(t) }
      board.append(row)
    }
    const keys = {}
    const kb = h('div', { class: 'kb' }, KB_ROWS.map((line) => h('div', { class: 'kb-row' }, [...line].map((k) => {
      const label = k === '>' ? 'Entrée' : k === '<' ? '⌫' : k
      const b = h('button', {
        class: `key${k === '>' || k === '<' ? ' wide' : ''}`, tabindex: '-1', 'data-k': k,
        title: k === '<' ? 'Effacer' : null,
        onclick: (e) => { e.currentTarget.blur(); press(k === '>' ? 'ENTER' : k === '<' ? 'BACK' : k) },
      }, label)
      if (k.length === 1 && /[A-Z]/.test(k)) keys[k] = b
      return b
    }))))
    wrap.append(h('style', null, CSS), board, bottom)
    root.append(wrap)

    const say = (text, ms = 1500) => {
      wrap.querySelector('.msg')?.remove()
      clearTimeout(msgTimer)
      const m = h('div', { class: 'msg' }, text)
      wrap.append(m)
      msgTimer = setTimeout(() => m.remove(), ms)
    }

    const paintTile = (t, ch, state) => {
      t.textContent = ch || ''
      t.className = `tile${ch ? ' filled' : ''}${state ? ` ${state}` : ''}`
    }
    const paintKeyboard = () => {
      const best = {}
      for (const { word, res } of slot().rows) {
        for (let i = 0; i < LEN; i++) if (!best[word[i]] || RANK[res[i]] > RANK[best[word[i]]]) best[word[i]] = res[i]
      }
      for (const [k, b] of Object.entries(keys)) b.className = `key${best[k] ? ` ${best[k]}` : ''}`
    }
    const paintBoard = () => {
      const s = slot()
      for (let r = 0; r < ROWS; r++) {
        const row = s.rows[r]
        board.children[r].className = `row${r === s.rows.length && !s.done ? ' cur' : ''}`
        for (let c = 0; c < LEN; c++) {
          if (row) paintTile(tiles[r][c], row.word[c], row.res[c])
          else if (r === s.rows.length && !s.done) paintTile(tiles[r][c], cur[c])
          else paintTile(tiles[r][c], '')
        }
      }
    }

    const shareText = () => {
      const s = slots.daily
      const em = { correct: prefs.colorblind ? '🟧' : '🟩', present: prefs.colorblind ? '🟦' : '🟨', absent: '⬛' }
      const head = `Mot du jour n°${s.day + 1} — ${s.won ? s.rows.length : 'X'}/${ROWS}${s.hard ? ' (difficile)' : ''}`
      return `${head}\n\n${s.rows.map((r) => r.res.map((x) => em[x]).join('')).join('\n')}`
    }
    const copyResult = async () => {
      const text = shareText()
      try {
        await navigator.clipboard.writeText(text)
      } catch {
        const ta = h('textarea', { style: { position: 'fixed', opacity: '0' } }, text)
        document.body.append(ta); ta.select()
        const ok = document.execCommand('copy'); ta.remove()
        if (!ok) { ctx.toast('Copie impossible', 'err'); return }
      }
      ctx.toast('Résultat copié dans le presse-papiers', 'ok')
    }

    const resultPanel = () => {
      const s = slot()
      const lines = []
      if (s.won) lines.push(h('div', { class: 'big' }, s.rows.length === 1 ? 'Du premier coup !' : `Trouvé en ${s.rows.length} essais`))
      else lines.push(h('div', { class: 'big' }, 'Pas cette fois…'), h('div', null, 'Le mot était ', h('span', { class: 'word' }, s.answer)))
      if (s.mode === 'daily') {
        lines.push(h('div', { class: 'muted' }, 'Prochain mot dans ', h('span', { class: 'cd' }, fmtCountdown())))
        lines.push(h('div', { class: 'btns' },
          h('button', { class: 'primary', onclick: copyResult }, '📋 Copier le résultat'),
          h('button', { onclick: showStats }, '📊 Statistiques'),
          h('button', { onclick: () => setMode('free') }, '∞ Mode libre')))
      } else {
        lines.push(h('div', { class: 'btns' },
          h('button', { class: 'primary', onclick: newFree }, 'Nouveau mot'),
          h('button', { onclick: showStats }, '📊 Statistiques')))
      }
      return h('div', { class: 'result' }, lines)
    }

    const renderBottom = () => bottom.replaceChildren(slot().done ? resultPanel() : kb)

    const renderStatus = () => {
      const s = slot()
      ctx.setStatus(
        h('span', { class: 'pill' }, s.mode === 'daily' ? `📅 n°${s.day + 1}` : '∞ Libre'),
        h('span', { class: 'pill' }, `Essai ${Math.min(s.rows.length + (s.done ? 0 : 1), ROWS)}/${ROWS}`),
        s.hard ? h('span', { class: 'pill', title: 'Mode difficile' }, '🔥 Difficile') : null)
    }

    const renderToolbar = () => {
      ctx.setToolbar(
        h('div', { class: 'seg' },
          h('button', { class: mode === 'daily' ? 'on' : '', onclick: () => setMode('daily') }, '📅 Du jour'),
          h('button', { class: mode === 'free' ? 'on' : '', onclick: () => setMode('free') }, '∞ Libre')),
        mode === 'free' ? h('button', { onclick: newFree }, 'Nouveau mot') : null,
        h('button', { class: 'icon', title: 'Statistiques', onclick: showStats }, '📊'),
        h('button', { class: 'icon', title: 'Options', onclick: showOptions }, '⚙️'),
        h('button', { class: 'icon', title: 'Aide', onclick: showHelp }, '❔'))
    }

    const render = () => {
      wrap.classList.toggle('cb', !!prefs.colorblind)
      paintBoard(); paintKeyboard(); renderBottom(); renderStatus(); renderToolbar()
    }

    function setMode(m) {
      if (busy || m === mode) return
      mode = m
      cur = ''
      wrap.querySelector('.msg')?.remove()
      prefs = { ...prefs, mode }
      savePrefs()
      render()
    }

    async function newFree() {
      if (busy) return
      const f = slots.free
      if (!f.done && f.rows.length) {
        if (!(await ctx.confirm('Abandonner le mot en cours ? La partie comptera comme perdue.', { ok: 'Abandonner', danger: true }))) return
        await recordFree(false)
      }
      slots.free = mkSlot('free', randomAnswer())
      mode = 'free'
      cur = ''
      wrap.querySelector('.msg')?.remove()
      persist()
      render()
    }

    async function recordFree(won) {
      const streak = won ? (stats.free_streak || 0) + 1 : 0
      stats = await ctx.stats.update({ inc: { free_played: 1, free_won: won ? 1 : 0 }, max: { free_best: streak }, set: { free_streak: streak } })
    }

    async function recordDaily(s) {
      const prevStreak = stats.last_win_day === s.day - 1 ? (stats.streak || 0) : 0
      const streak = s.won ? prevStreak + 1 : 0
      const inc = { played: 1, won: s.won ? 1 : 0 }
      inc[s.won ? `dist_${s.rows.length}` : 'fails'] = 1
      stats = await ctx.stats.update({
        inc, max: { max_streak: streak },
        set: { streak, last_day: s.day, ...(s.won ? { last_win_day: s.day } : {}) },
      })
      stats = await ctx.stats.update({ set: { hub_best: `série ${stats.max_streak || 0}` } })
    }

    const currentStreak = () => (stats.last_win_day != null && stats.last_win_day >= dayIndex() - 1 ? stats.streak || 0 : 0)

    function showStats() {
      const st = stats
      const played = st.played || 0, won = st.won || 0
      const dist = Array.from({ length: ROWS }, (_, i) => st[`dist_${i + 1}`] || 0)
      const maxD = Math.max(1, ...dist)
      const d = slots.daily
      const hl = d.done && d.won ? d.rows.length : 0
      const body = h('div', { class: 'dw-stats' },
        h('h4', null, 'Mot du jour'),
        h('div', { class: 'nums' },
          h('div', null, h('b', null, played), h('span', null, 'Parties')),
          h('div', null, h('b', null, played ? Math.round((won / played) * 100) : 0), h('span', null, '% victoires')),
          h('div', null, h('b', null, currentStreak()), h('span', null, 'Série actuelle')),
          h('div', null, h('b', null, st.max_streak || 0), h('span', null, 'Série max'))),
        h('h4', null, 'Répartition des essais'),
        dist.map((n, i) => h('div', { class: 'bar' }, h('i', null, i + 1),
          h('div', { class: i + 1 === hl ? 'hl' : '', style: { width: `${Math.max(7, (n / maxD) * 100)}%` } }, n))),
        d.done ? h('div', { class: 'muted', style: { marginTop: '10px' } }, 'Prochain mot dans ', h('span', { class: 'cd' }, fmtCountdown())) : null,
        h('div', { class: 'free' }, `Mode libre : ${st.free_played || 0} partie(s), ${st.free_won || 0} victoire(s), série ${st.free_streak || 0} (meilleure ${st.free_best || 0}).`))
      const buttons = [{ label: 'Fermer', value: false, primary: !d.done }]
      if (d.done) buttons.unshift({ label: '📋 Copier le résultat', value: 'copy', primary: true })
      ctx.dialog({ title: 'Statistiques', body, buttons }).then((v) => { if (v === 'copy') copyResult() })
    }

    function showOptions() {
      const s = slot()
      const lockHard = s.rows.length > 0 && !s.done
      const cb = (key, label, help, disabled) => {
        const input = h('input', { type: 'checkbox', checked: !!prefs[key], disabled })
        return [h('label', null, input, h('div', null, label, h('small', null, help))), input]
      }
      const [lHard, iHard] = cb('hard', 'Mode difficile', lockHard ? 'Modifiable seulement avant le premier essai.' : 'Les indices révélés doivent être réutilisés dans les essais suivants.', lockHard)
      const [lCb, iCb] = cb('colorblind', 'Couleurs contrastées', 'Orange / bleu au lieu de vert / jaune (daltonisme).')
      const [lAny, iAny] = cb('anyWord', 'Accepter tout mot de 5 lettres', 'Ne vérifie plus les propositions dans le dictionnaire.')
      ctx.dialog({
        title: 'Options', body: h('div', { class: 'dw-opts' }, lHard, lCb, lAny),
        buttons: [{ label: 'Annuler', value: null }, { label: 'Enregistrer', primary: true, value: () => ({ hard: iHard.checked, colorblind: iCb.checked, anyWord: iAny.checked }) }],
      }).then((v) => {
        if (!v) return
        prefs = { ...prefs, ...v }
        savePrefs()
        if (!lockHard && !s.done) s.hard = !!prefs.hard
        render()
      })
    }

    function showHelp() {
      const ex = (word, states) => h('div', { style: { display: 'flex', gap: '4px', margin: '6px 0' } }, [...word].map((c, i) =>
        h('div', { class: `tile ${states[i] || ''}`, style: { width: '30px', height: '30px', fontSize: '15px', borderRadius: '6px' } }, c)))
      ctx.dialog({
        title: 'Mot du jour — règles', wide: false,
        body: h('div', { class: `g-daily-word${prefs.colorblind ? ' cb' : ''}`, style: { display: 'block', padding: 0 } },
          h('p', null, `Trouvez le mot de ${LEN} lettres en ${ROWS} essais. Chaque proposition doit être un mot du dictionnaire (sans accents : É = E).`),
          ex('SAPIN', ['correct']), h('p', { class: 'muted' }, 'S est dans le mot, à la bonne place.'),
          ex('CORDE', ['', '', 'present']), h('p', { class: 'muted' }, 'R est dans le mot, mais ailleurs.'),
          ex('FLUTE', ['', '', '', 'absent']), h('p', { class: 'muted' }, 'T n’est pas dans le mot (ou plus d’exemplaires).'),
          h('p', { style: { marginTop: '8px' } }, '📅 Du jour : le même mot pour tout le monde, une seule partie par jour. ∞ Libre : mots aléatoires illimités.'),
          h('p', null, 'Clavier : lettres (les accents sont ignorés), ', h('span', { class: 'kbd' }, 'Entrée'), ' pour valider, ', h('span', { class: 'kbd' }, '⌫'), ' pour effacer.')),
      })
    }

    // ── Game flow ──
    function press(k) {
      const s = slot()
      if (busy || s.done) return
      const r = s.rows.length
      if (k === 'ENTER') return submit()
      if (k === 'BACK') {
        if (!cur) return
        cur = cur.slice(0, -1)
        paintTile(tiles[r][cur.length], '')
        return
      }
      if (cur.length >= LEN) return
      cur += k
      const t = tiles[r][cur.length - 1]
      paintTile(t, k)
      t.classList.add('pop')
      ctx.sfx('tick')
    }

    function reject(text) {
      const row = board.children[slot().rows.length]
      row.classList.remove('shake'); void row.offsetWidth; row.classList.add('shake')
      setTimeout(() => row.classList.remove('shake'), 500)
      ctx.sfx('error')
      say(text)
    }

    async function submit() {
      const s = slot()
      if (cur.length < LEN) return reject('Pas assez de lettres')
      if (!prefs.anyWord && !ACCEPTED.has(cur)) return reject('Mot inconnu')
      if (!s.rows.length) s.hard = !!prefs.hard
      if (s.hard) { const v = hardViolation(cur, s.rows); if (v) return reject(v) }
      busy = true
      const word = cur
      const res = scoreGuess(word, s.answer)
      const r = s.rows.length
      s.rows.push({ word, res })
      cur = ''
      board.children[r].classList.remove('cur')
      if (word === s.answer) { s.done = true; s.won = true } else if (s.rows.length >= ROWS) s.done = true
      if (s.mode === 'daily' && s.done) {
        prefs = { ...prefs, daily: { day: s.day, guesses: s.rows.map((x) => x.word), won: s.won, hard: s.hard } }
        savePrefs()
      }
      persist()
      for (let i = 0; i < LEN; i++) {
        setTimeout(() => {
          tiles[r][i].classList.remove('pop'); tiles[r][i].classList.add('flip'); ctx.sfx('flag')
        }, i * 280)
        setTimeout(() => { paintTile(tiles[r][i], word[i], res[i]); tiles[r][i].classList.add('flip') }, i * 280 + 250)
      }
      await new Promise((ok) => setTimeout(ok, (LEN - 1) * 280 + 520))
      if (!wrap.isConnected) return
      for (const t of tiles[r]) t.classList.remove('flip')
      paintKeyboard()
      if (s.done) await finish(s, r)
      else { board.children[r + 1]?.classList.add('cur'); renderStatus() }
      busy = false
    }

    async function finish(s, r) {
      if (s.won) {
        const praise = ['Génial !', 'Magnifique !', 'Impressionnant !', 'Splendide !', 'Bien joué !', 'Ouf !'][r] || 'Bravo !'
        tiles[r].forEach((t, i) => setTimeout(() => t.classList.add('bounce'), i * 90))
        ctx.sfx('win')
        say(praise, 1800)
        await new Promise((ok) => setTimeout(ok, 900))
      } else {
        ctx.sfx('lose')
        say(s.answer, 2500)
      }
      if (s.mode === 'daily') await recordDaily(s)
      else await recordFree(s.won)
      if (!wrap.isConnected) return
      render()
      const buttons = s.mode === 'daily'
        ? [{ label: '📋 Copier', primary: true, onClick: copyResult }, { label: '📊 Statistiques', onClick: showStats }, { label: 'Fermer' }]
        : [{ label: 'Nouveau mot', primary: true, onClick: newFree }, { label: 'Fermer' }]
      ctx.overlay({
        title: s.won ? 'Bravo !' : 'Perdu',
        text: s.won ? `Mot trouvé en ${s.rows.length} essai${s.rows.length > 1 ? 's' : ''} : ${s.answer}` : `Le mot était ${s.answer}.`,
        body: s.mode === 'daily' ? h('div', { class: 'muted' }, 'Prochain mot dans ', h('span', { class: 'cd' }, fmtCountdown())) : null,
        buttons,
      })
    }

    // ── Input & timers ──
    const onKey = (e) => {
      if (document.querySelector('.dlg-back') || e.ctrlKey || e.metaKey || e.altKey) return
      const t = e.target
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (root.querySelector(':scope > .overlay')) return
      if (e.key === 'Enter') { e.preventDefault(); if (!e.repeat) press('ENTER'); return }
      if (e.key === 'Backspace') { e.preventDefault(); press('BACK'); return }
      if (e.key.length === 1 || e.key.length === 2) {
        const n = normalize(e.key)
        if (/^[A-Z]{1,2}$/.test(n)) { e.preventDefault(); for (const c of n) press(c) }
      }
    }
    document.addEventListener('keydown', onKey)
    ctx.onCleanup(() => document.removeEventListener('keydown', onKey))

    const tick = setInterval(() => {
      const txt = fmtCountdown()
      for (const el of document.querySelectorAll('.g-daily-word .cd, .dw-stats .cd, .overlay .cd')) el.textContent = txt
      if (!busy && slots.daily.day !== dayIndex()) {
        saved = { ...saved, daily: null }
        slots.daily = loadDaily()
        if (mode === 'daily') { cur = ''; render() }
      }
    }, 1000)
    ctx.onCleanup(() => clearInterval(tick))

    const fit = () => {
      const W = root.clientWidth, H = root.clientHeight
      const key = Math.max(30, Math.min(54, Math.floor(H * 0.075), Math.floor((W - 40) / 10.5)))
      const kbH = key * 3 + 12
      const tile = Math.max(28, Math.min(64, Math.floor((W - 40) / LEN) - 6, Math.floor((H - kbH - 60) / ROWS) - 6))
      wrap.style.setProperty('--tile', `${tile}px`)
      wrap.style.setProperty('--key', `${key}px`)
    }
    const ro = new ResizeObserver(fit)
    ro.observe(root)
    ctx.onCleanup(() => ro.disconnect())
    fit()
    render()

    return {
      pause() {},
      destroy() { clearTimeout(msgTimer) },
    }
  },
}
