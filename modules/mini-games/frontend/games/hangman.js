import { THEMES } from './hangman-words.js'

const DIFFS = {
  facile: { label: 'Facile', max: 10 },
  normal: { label: 'Normal', max: 8 },
  difficile: { label: 'Difficile', max: 6 },
}
const KB_ROWS = ['AZERTYUIOP', 'QSDFGHJKLM', 'WXCVBN']
// The full drawing has 10 strokes; harder levels start with the gallows already partly built.
const PARTS = [
  'M12 232 H160',
  'M45 232 V14',
  'M45 14 H142 M45 54 L85 14',
  'M142 14 V44',
  'M142 44 a17 17 0 1 0 0.01 0',
  'M142 78 V146',
  'M142 96 L116 128',
  'M142 96 L168 128',
  'M142 146 L120 194',
  'M142 146 L164 194',
]

const base = (ch) => ch.normalize('NFD').replace(/[̀-ͯ]/g, '').toUpperCase()
const isLetter = (ch) => /^[A-Z]$/.test(base(ch))
const letterKey = base
const lettersOf = (word) => new Set([...word].filter(isLetter).map(letterKey))

const CSS = `
.g-hangman { flex: 1; display: flex; align-items: center; justify-content: safe center; gap: 28px; padding: 14px 20px; min-height: 0;
  --key: 40px; --gal: 320px; }
.g-hangman.narrow { flex-direction: column; gap: 10px; }
.g-hangman .gal { width: calc(var(--gal) * .78); height: var(--gal); flex-shrink: 0; }
.g-hangman .gal path { fill: none; stroke: #d4d4d8; stroke-width: 5; stroke-linecap: round; stroke-linejoin: round;
  stroke-dasharray: 1 2; stroke-dashoffset: 1; opacity: 0; transition: stroke-dashoffset .5s ease; }
.g-hangman .gal path.wood { stroke: #a16207; stroke-width: 7; }
.g-hangman .gal path.on { stroke-dashoffset: 0; opacity: 1; }
.g-hangman .gal .face { opacity: 0; transition: opacity .4s .3s; }
.g-hangman .gal .face path { stroke-dasharray: none; stroke-dashoffset: 0; opacity: 1; stroke-width: 2.5; }
.g-hangman .gal.lost .face.dead, .g-hangman .gal.won .face.happy { opacity: 1; }
.g-hangman .gal.lost .body { stroke: var(--err); }
.g-hangman .gal.lost .fig { animation: hmSwing 2.4s ease-in-out infinite; transform-origin: 142px 14px; }
.g-hangman .gal.won .fig { animation: hmJump .6s ease 2; transform-origin: 142px 200px; }
@keyframes hmSwing { 0%, 100% { transform: rotate(3deg) } 50% { transform: rotate(-3deg) } }
@keyframes hmJump { 50% { transform: translateY(-8px) } }
.g-hangman .side { display: flex; flex-direction: column; align-items: center; gap: 14px; min-width: 0; max-width: 620px; }
.g-hangman .theme { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
.g-hangman .clue { color: var(--accent-h); font-style: italic; }
.g-hangman .clue:empty { display: none; }
.g-hangman .word { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px 22px; }
.g-hangman .grp { display: flex; gap: 5px; }
.g-hangman .slot { width: calc(var(--key) * .78); height: calc(var(--key) * 1.05); display: grid; place-items: end center;
  font-size: calc(var(--key) * .62); font-weight: 800; border-bottom: 3px solid #52525b; padding-bottom: 2px; }
.g-hangman .slot.sym { border-bottom-color: transparent; width: auto; min-width: 10px; color: var(--muted); }
.g-hangman .slot.show { animation: hmPop .3s ease; }
.g-hangman .slot.missed { color: var(--err); }
.g-hangman.win .slot { color: var(--ok); animation: hmJump .5s ease; }
@keyframes hmPop { 0% { transform: scale(.3); opacity: 0 } 70% { transform: scale(1.2) } }
.g-hangman .lives { display: flex; gap: 4px; }
.g-hangman .lives i { width: 12px; height: 12px; border-radius: 50%; background: var(--ok); transition: background .3s, transform .3s; }
.g-hangman .lives i.lost { background: #3f3f46; transform: scale(.7); }
.g-hangman .kb { display: flex; flex-direction: column; gap: 6px; align-items: center; }
.g-hangman .kb-row { display: flex; gap: 5px; }
.g-hangman .key { width: calc(var(--key) * .9); height: var(--key); padding: 0; font-weight: 700; font-size: calc(var(--key) * .4); }
.g-hangman .key.hit { background: rgba(34,197,94,.22); border-color: rgba(34,197,94,.6); color: #86efac; opacity: 1; }
.g-hangman .key.miss { background: rgba(239,68,68,.12); border-color: rgba(239,68,68,.3); color: #7f1d1d; opacity: 1; }
.g-hangman .shake { animation: hmShake .35s ease; }
@keyframes hmShake { 25% { transform: translateX(-6px) } 75% { transform: translateX(6px) } }
.hm-duo .row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
.hm-duo input[type=password], .hm-duo input[type=text] { width: 100%; }
`

export default {
  async mount(root, ctx) {
    const { h } = ctx
    const prefs = { diff: 'normal', theme: 'tous', recent: [], ...(await ctx.prefs.get().catch(() => ({}))) }
    if (!DIFFS[prefs.diff]) prefs.diff = 'normal'
    if (prefs.theme !== 'tous' && !THEMES.some((t) => t.id === prefs.theme)) prefs.theme = 'tous'
    let stats = (await ctx.stats.get().catch(() => ({}))) || {}
    const saved = await ctx.save.get().catch(() => null)

    let g = null
    let streak = 0
    let closeOverlay = null

    const savePrefs = () => ctx.prefs.set({ ...prefs, recent: prefs.recent.slice(-60) })
    const persist = () => {
      if (g && !g.done) return ctx.save.set({ ...g, guessed: [...g.guessed], streak })
      if (streak > 0) return ctx.save.set({ word: null, diff: prefs.diff, streak })
      return ctx.save.clear()
    }

    const pickWord = () => {
      const pool = prefs.theme === 'tous' ? THEMES : THEMES.filter((t) => t.id === prefs.theme)
      const all = pool.flatMap((t) => t.words.map((w) => ({ w, t })))
      const recent = new Set(prefs.recent)
      const fresh = all.filter((x) => !recent.has(x.w))
      const list = fresh.length ? fresh : all
      const pick = list[Math.floor(Math.random() * list.length)]
      prefs.recent = [...prefs.recent.filter((w) => w !== pick.w), pick.w].slice(-60)
      savePrefs()
      return pick
    }

    const newGame = ({ mode = 'solo', word, themeName, clue } = {}) => {
      closeOverlay?.()
      if (mode === 'solo') {
        const p = pickWord()
        word = p.w
        themeName = `${p.t.icon} ${p.t.name}`
      }
      g = { mode, word, themeName, clue: clue || '', diff: prefs.diff, guessed: new Set(), errors: 0, hints: 0, done: false, won: false }
      persist()
      render(true)
    }

    // ── DOM ──
    const wrap = h('div', { class: 'g-hangman' })
    const svgNS = 'http://www.w3.org/2000/svg'
    const sv = (tag, attrs) => { const el = document.createElementNS(svgNS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el }
    const gal = sv('svg', { class: 'gal', viewBox: '0 0 180 240' })
    const fig = sv('g', { class: 'fig' })
    const parts = PARTS.map((d, i) => {
      const p = sv('path', { d, pathLength: '1', class: i < 4 ? 'wood' : 'body' })
      ;(i < 4 ? gal : fig).append(p)
      return p
    })
    const dead = sv('g', { class: 'face dead' })
    for (const d of ['M133 38 l6 6 M139 38 l-6 6', 'M145 38 l6 6 M151 38 l-6 6', 'M134 55 q8 -6 16 0']) dead.append(sv('path', { d, stroke: '#ef4444' }))
    const happy = sv('g', { class: 'face happy' })
    for (const d of ['M136 40 v2', 'M148 40 v2', 'M134 50 q8 8 16 0']) happy.append(sv('path', { d }))
    fig.append(dead, happy)
    gal.append(fig)

    const themeEl = h('div', { class: 'theme' })
    const clueEl = h('div', { class: 'clue' })
    const wordEl = h('div', { class: 'word' })
    const livesEl = h('div', { class: 'lives' })
    const keys = {}
    const kb = h('div', { class: 'kb' }, KB_ROWS.map((line) => h('div', { class: 'kb-row' }, [...line].map((k) => {
      keys[k] = h('button', { class: 'key', tabindex: '-1', 'data-k': k, onclick: (e) => { e.currentTarget.blur(); guess(k) } }, k)
      return keys[k]
    }))))
    const side = h('div', { class: 'side' }, themeEl, clueEl, wordEl, livesEl, kb)
    wrap.append(h('style', null, CSS), gal, side)
    root.append(wrap)

    const maxErr = () => DIFFS[g.diff].max
    const isRevealed = (ch) => !isLetter(ch) || g.guessed.has(letterKey(ch))
    const allFound = () => [...lettersOf(g.word)].every((l) => g.guessed.has(l))

    const renderWord = (animateLetter) => {
      wordEl.replaceChildren(...g.word.split(' ').map((part) => h('div', { class: 'grp' }, [...part].map((ch) => {
        if (!isLetter(ch)) return h('div', { class: 'slot sym' }, ch)
        const shown = isRevealed(ch)
        const missed = g.done && !g.won && !shown
        const cls = `slot${shown && animateLetter === letterKey(ch) ? ' show' : ''}${missed ? ' missed' : ''}`
        return h('div', { class: cls }, shown || g.done ? ch : '')
      }))))
    }

    const renderGallows = () => {
      const shown = 10 - maxErr() + g.errors
      parts.forEach((p, i) => p.classList.toggle('on', i < shown))
      gal.classList.toggle('lost', g.done && !g.won)
      gal.classList.toggle('won', g.done && g.won && shown > 4)
    }

    const renderKeys = () => {
      const inWord = lettersOf(g.word)
      for (const [k, b] of Object.entries(keys)) {
        const used = g.guessed.has(k)
        b.className = `key${used ? (inWord.has(k) ? ' hit' : ' miss') : ''}`
        b.disabled = used || g.done
      }
    }

    const renderStatus = () => {
      const left = maxErr() - g.errors
      livesEl.replaceChildren(...Array.from({ length: maxErr() }, (_, i) => h('i', { class: i < left ? '' : 'lost', title: `${left} vie(s) restante(s)` })))
      ctx.setStatus(
        g.mode === 'solo' ? h('span', { class: 'pill', title: 'Mots trouvés d’affilée' }, `🔥 Série ${streak}`) : h('span', { class: 'pill' }, '👥 Deux joueurs'),
        h('span', { class: 'pill' }, `❌ ${g.errors}/${maxErr()}`),
        g.hints ? h('span', { class: 'pill' }, `💡 ${g.hints}`) : null)
    }

    const renderToolbar = () => {
      const themeSel = h('select', { title: 'Thème', onchange: () => setTheme(themeSel.value) },
        h('option', { value: 'tous', selected: prefs.theme === 'tous' }, '🎲 Tous les thèmes'),
        THEMES.map((t) => h('option', { value: t.id, selected: prefs.theme === t.id }, `${t.icon} ${t.name}`)))
      ctx.setToolbar(
        h('button', { onclick: () => requestNew() }, 'Nouveau mot'),
        h('div', { class: 'seg' }, Object.entries(DIFFS).map(([id, d]) =>
          h('button', { class: prefs.diff === id ? 'on' : '', title: `${d.max} erreurs autorisées`, onclick: () => setDiff(id) }, d.label))),
        themeSel,
        h('button', { title: 'Un joueur choisit le mot, l’autre devine', onclick: duoSetup }, '👥 2 joueurs'),
        h('button', { title: 'Révèle une lettre (coûte une erreur)', onclick: hint, disabled: !g || g.done }, '💡 Indice'),
        h('button', { class: 'icon', title: 'Meilleures séries', onclick: () => showScores() }, '🏆'),
        h('button', { class: 'icon', title: 'Aide', onclick: showHelp }, '❔'))
    }

    function render(fresh = false, animateLetter = null) {
      wrap.classList.toggle('win', g.done && g.won)
      themeEl.textContent = g.mode === 'duo' ? 'Mot choisi par le joueur 1' : g.themeName
      clueEl.textContent = g.clue ? `Indice : ${g.clue}` : ''
      renderWord(fresh ? null : animateLetter)
      renderGallows(); renderKeys(); renderStatus(); renderToolbar()
    }

    const showScores = (highlightId = null) => ctx.showScores(prefs.diff, {
      title: 'Meilleures séries', highlightId, fmt: (s) => `${s} mot${s > 1 ? 's' : ''}`,
      variants: Object.entries(DIFFS).map(([id, d]) => [id, d.label]),
    })

    function showHelp() {
      ctx.dialog({
        title: 'Pendu — règles',
        body: h('div', null,
          h('p', null, 'Devinez le mot lettre par lettre. Chaque lettre absente ajoute un trait au dessin : quand il est complet, c’est perdu.'),
          h('p', null, 'Les accents sont ignorés (E révèle aussi É, È, Ê…). Tirets, espaces et apostrophes sont affichés d’office.'),
          h('p', null, 'Facile : 10 erreurs · Normal : 8 · Difficile : 6. 💡 Indice : révèle une lettre mais compte comme une erreur.'),
          h('p', null, 'En solo, enchaînez les mots pour allonger votre série : elle est enregistrée au classement de la difficulté quand vous perdez.'),
          h('p', null, '👥 2 joueurs : un joueur saisit un mot secret, l’autre le devine.'),
          h('p', null, 'Clavier : tapez simplement les lettres.')),
      })
    }

    // ── Logic ──
    function guess(k) {
      if (!g || g.done || g.guessed.has(k) || closeOverlay) return
      g.guessed.add(k)
      if (lettersOf(g.word).has(k)) {
        ctx.sfx('pop')
        if (allFound()) return end(true, k)
      } else {
        g.errors++
        ctx.sfx('hit')
        wordEl.classList.remove('shake'); void wordEl.offsetWidth; wordEl.classList.add('shake')
        if (g.errors >= maxErr()) return end(false, k)
      }
      persist()
      render(false, k)
    }

    function hint() {
      if (!g || g.done) return
      if (g.errors >= maxErr() - 1) { ctx.toast('Plus assez de vies pour un indice', 'err'); return }
      const hidden = [...lettersOf(g.word)].filter((l) => !g.guessed.has(l))
      if (!hidden.length) return
      const k = hidden[Math.floor(Math.random() * hidden.length)]
      g.guessed.add(k)
      g.errors++
      g.hints++
      ctx.sfx('flag')
      if (allFound()) return end(true, k)
      persist()
      render(false, k)
    }

    async function end(won, k) {
      g.done = true
      g.won = won
      ctx.sfx(won ? 'win' : 'lose')
      let rank = null
      if (g.mode === 'solo') {
        if (won) {
          streak++
          stats = await ctx.stats.update({ inc: { played: 1, won: 1 }, max: { best: streak, [`best_${g.diff}`]: streak } })
          stats = await ctx.stats.update({ set: { hub_best: `série ${stats.best || streak}` } })
        } else {
          stats = await ctx.stats.update({ inc: { played: 1 } })
          if (streak > 0) rank = await ctx.scores.submit(g.diff, streak, { meta: { last: g.word } }).catch((e) => { ctx.toast(e.message, 'err'); return null })
        }
      } else {
        stats = await ctx.stats.update({ inc: { duo_played: 1, duo_won: won ? 1 : 0 } })
      }
      const lostStreak = !won && g.mode === 'solo' ? streak : 0
      if (!won && g.mode === 'solo') streak = 0
      persist()
      if (!wrap.isConnected) return
      render(false, k)
      let text
      if (g.mode === 'duo') text = won ? `Le mot « ${g.word} » a été trouvé !` : `Le mot était « ${g.word} ».`
      else if (won) text = `« ${g.word} »\nSérie : ${streak} mot${streak > 1 ? 's' : ''}${g.errors === 0 ? ' — sans faute !' : ''}`
      else text = `Le mot était « ${g.word} ».${lostStreak ? `\nSérie terminée : ${lostStreak} mot${lostStreak > 1 ? 's' : ''}.` : ''}`
      const body = rank ? h('div', { style: { fontWeight: 700, color: rank.record ? 'var(--warn)' : 'var(--accent-h)' } },
        rank.record ? '🏆 Nouveau record !' : rank.rank ? `Rang ${rank.rank} sur ${rank.total}` : '') : null
      const buttons = g.mode === 'duo'
        ? [{ label: 'Nouvelle manche à deux', primary: true, onClick: () => { closeOverlay = null; duoSetup() } },
          { label: 'Jouer en solo', onClick: () => { closeOverlay = null; newGame() } }]
        : [{ label: won ? 'Mot suivant' : 'Nouvelle partie', primary: true, onClick: () => { closeOverlay = null; newGame() } }]
      if (rank) buttons.push({ label: '🏆 Classement', onClick: () => { closeOverlay = null; showScores(rank.id); newGame() } })
      closeOverlay = ctx.overlay({ title: won ? 'Bravo !' : 'Pendu !', text, body, buttons })
      const close = closeOverlay
      closeOverlay = () => { close(); closeOverlay = null }
    }

    async function abandon() {
      if (!g || g.done) return true
      const inProgress = g.mode === 'solo' && (g.guessed.size > 0 || streak > 0)
      if (!inProgress) return true
      const msg = streak > 0 ? `Abandonner ? Votre série de ${streak} mot${streak > 1 ? 's' : ''} sera enregistrée et remise à zéro.` : 'Abandonner le mot en cours ?'
      if (!(await ctx.confirm(msg, { ok: 'Abandonner', danger: true }))) return false
      if (g.guessed.size) stats = await ctx.stats.update({ inc: { played: 1 } })
      if (streak > 0) await ctx.scores.submit(g.diff, streak, { meta: { last: g.word } }).catch(() => {})
      streak = 0
      return true
    }

    async function requestNew() {
      if (!(await abandon())) return
      newGame()
    }

    async function setDiff(id) {
      if (id === prefs.diff) return
      if (!(await abandon())) return
      prefs.diff = id
      savePrefs()
      newGame()
    }

    function setTheme(id) {
      prefs.theme = id
      savePrefs()
      if (g && !g.done && g.mode === 'solo' && g.guessed.size === 0) newGame()
      else ctx.toast('Le thème s’appliquera au prochain mot')
    }

    async function duoSetup() {
      const secret = h('input', { type: 'password', maxlength: 30, placeholder: 'Mot ou expression', autocomplete: 'off' })
      const clue = h('input', { type: 'text', maxlength: 40, placeholder: 'Facultatif (ex. un animal)' })
      const show = h('input', { type: 'checkbox', onchange: () => { secret.type = show.checked ? 'text' : 'password' } })
      const body = h('div', { class: 'hm-duo' },
        h('p', { class: 'muted' }, 'Joueur 1 : saisissez le mot secret à l’abri des regards, puis passez la main au joueur 2.'),
        h('div', { class: 'row' }, secret),
        h('label', { class: 'row muted' }, show, 'Afficher'),
        h('div', { class: 'row' }, clue))
      const v = await ctx.dialog({ title: '👥 Deux joueurs', body,
        buttons: [{ label: 'Annuler', value: null }, { label: 'Commencer', primary: true, value: () => ({ word: secret.value, clue: clue.value }) }] })
      if (!v) return
      const word = v.word.trim().replace(/\s+/g, ' ').replace(/[’`]/g, "'").toUpperCase()
      const letters = [...word].filter(isLetter).length
      if (!/^[A-ZÀ-ÖØ-ÞŒ' -]+$/i.test(word) || letters < 2) {
        ctx.toast('Mot invalide : lettres, espaces, tirets ou apostrophes (2 lettres minimum)', 'err')
        return duoSetup()
      }
      if (!(await abandon())) return
      newGame({ mode: 'duo', word, clue: v.clue.trim() })
    }

    const onKey = (e) => {
      if (document.querySelector('.dlg-back') || e.ctrlKey || e.metaKey || e.altKey) return
      const t = e.target
      if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (root.querySelector(':scope > .overlay') || e.key.length !== 1) return
      const k = letterKey(e.key)
      if (/^[A-Z]$/.test(k)) { e.preventDefault(); guess(k) }
    }
    document.addEventListener('keydown', onKey)
    ctx.onCleanup(() => document.removeEventListener('keydown', onKey))

    const fit = () => {
      const W = root.clientWidth, H = root.clientHeight
      const narrow = W < 760
      wrap.classList.toggle('narrow', narrow)
      const key = Math.max(28, Math.min(46, Math.floor((narrow ? W - 40 : W * 0.6) / 11), Math.floor(H / (narrow ? 15 : 11))))
      const gal = narrow ? Math.max(100, Math.min(260, H - key * 7 - 50)) : Math.max(180, Math.min(400, H - 40))
      wrap.style.setProperty('--key', `${key}px`)
      wrap.style.setProperty('--gal', `${gal}px`)
    }
    const ro = new ResizeObserver(fit)
    ro.observe(root)
    ctx.onCleanup(() => ro.disconnect())
    fit()

    if (saved && typeof saved.word === 'string' && saved.word && !saved.done) {
      streak = saved.streak || 0
      if (DIFFS[saved.diff]) prefs.diff = saved.diff
      g = { mode: saved.mode === 'duo' ? 'duo' : 'solo', word: saved.word, themeName: saved.themeName || '', clue: saved.clue || '',
        diff: prefs.diff, guessed: new Set(saved.guessed || []), errors: saved.errors || 0, hints: saved.hints || 0, done: false, won: false }
      render(true)
    } else {
      if (saved && saved.streak && DIFFS[saved.diff]) { streak = saved.streak; prefs.diff = saved.diff }
      newGame()
    }

    return {
      pause() { if (g && !g.done) persist() },
      destroy() {},
    }
  },
}
