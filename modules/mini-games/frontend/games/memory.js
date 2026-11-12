const SIZES = [['4x3', 4, 3], ['4x4', 4, 4], ['6x4', 6, 4], ['6x5', 6, 5], ['6x6', 6, 6]]
const THEMES = {
  animals: ['Animaux', [...'🐶🐱🦊🐻🐼🐨🐯🦁🐸🐵🐧🦉🦄🐝🐢🐙🦀🐬🦋🐞🦔🦒🐘🐳']],
  food: ['Gourmandises', [...'🍎🍐🍊🍋🍌🍉🍇🍓🍒🍑🥭🍍🥥🥝🥑🥕🌽🥐🧀🍕🍔🍩🍪🧁']],
  nature: ['Nature', ['🌵', '🌲', '🌴', '🍀', '🍁', '🌻', '🌹', '🌷', '🌼', '🌙', '⭐', '☀️', '⛅', '🌈', '❄️', '🔥', '💧', '🌊', '🍄', '🌸', '🌍', '⚡', '🐚', '🪨']],
  games: ['Loisirs', ['⚽', '🏀', '🏈', '⚾', '🎾', '🏐', '🎱', '🏓', '🏸', '🥊', '🎯', '🎳', '⛳', '🛹', '🎲', '🎸', '🎹', '🎺', '🥁', '♟️', '🧩', '🎨', '🪁', '🎮']],
  travel: ['Transports', ['🚗', '🚕', '🚌', '🏎️', '🚓', '🚑', '🚒', '🚚', '🚜', '🛵', '🚲', '🛴', '🚂', '✈️', '🚁', '🚀', '⛵', '🛶', '🚤', '🛸', '🚠', '🛺', '🚇', '🛻']],
  mine: ['Mes images', []],
}
const DELAYS = [[500, 'Rapide (0,5 s)'], [900, 'Normal (0,9 s)'], [1500, 'Lent (1,5 s)'], [2500, 'Très lent (2,5 s)']]
const MAX_IMAGES = 40
const THUMB = 160
const FLIP_MS = 380
const PLAYER_COLORS = ['#818cf8', '#f59e0b']

const sizeOf = (key) => SIZES.find((s) => s[0] === key) || SIZES[1]
const sizeLabel = (key) => key.replace('x', '×')
const fmtTime = (ms) => {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const CSS = `
.g-memory { flex: 1; display: flex; align-items: center; justify-content: center; padding: 16px; min-height: 0; }
.g-memory .grid { display: grid; gap: var(--gap); }
.g-memory .cd { width: var(--card); height: var(--card); perspective: calc(var(--card) * 4); cursor: pointer; position: relative; border-radius: 12px; }
.g-memory .in { position: absolute; inset: 0; transform-style: preserve-3d; transition: transform ${FLIP_MS}ms cubic-bezier(.3,.7,.3,1.1); }
.g-memory .cd.up .in, .g-memory .cd.found .in { transform: rotateY(180deg); }
.g-memory .face { position: absolute; inset: 0; border-radius: 12px; backface-visibility: hidden; -webkit-backface-visibility: hidden;
  display: grid; place-items: center; overflow: hidden; }
.g-memory .back { background:
    radial-gradient(circle at 50% 50%, rgba(255,255,255,.16) 0 9%, transparent 10%),
    repeating-linear-gradient(45deg, rgba(255,255,255,.05) 0 6px, transparent 6px 12px),
    linear-gradient(150deg, #5458e8, #3a2f9e 60%, #2b2370);
  border: 1px solid rgba(165,170,255,.35); box-shadow: 0 4px 14px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.15); transition: filter .15s; }
.g-memory .cd:not(.up):not(.found):hover .back { filter: brightness(1.15); }
.g-memory .front { transform: rotateY(180deg); background: linear-gradient(160deg, #26263a, #1c1c2b); border: 1px solid #34344c;
  font-size: calc(var(--card) * .56); line-height: 1; box-shadow: 0 4px 14px rgba(0,0,0,.35); }
.g-memory .front img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.g-memory .cd.found { cursor: default; }
.g-memory .cd.found .front { border-color: var(--pc, var(--ok)); box-shadow: 0 0 0 2px var(--pc, var(--ok)), 0 0 18px color-mix(in srgb, var(--pc, var(--ok)) 45%, transparent); }
.g-memory .cd.hit { animation: gm-hit .5s ease-out ${FLIP_MS - 80}ms; }
.g-memory .cd.miss .front { animation: gm-miss .32s ease-in-out ${FLIP_MS - 60}ms; }
.g-memory .grid.done .cd.found .front { animation: gm-wave .6s ease-in-out calc(var(--i) * 25ms); }
.g-memory .grid.kb .cd.cur::after { content: ''; position: absolute; inset: -5px; border-radius: 15px; border: 2px solid var(--accent-h);
  box-shadow: 0 0 12px rgba(129,140,248,.6); pointer-events: none; }
@keyframes gm-hit { 0% { transform: scale(1) } 40% { transform: scale(1.12) } 100% { transform: scale(1) } }
@keyframes gm-miss { 25% { transform: rotateY(180deg) translateX(-4px) } 75% { transform: rotateY(180deg) translateX(4px) } }
@keyframes gm-wave { 50% { transform: rotateY(180deg) translateY(-8px) } }
.gm-pl { cursor: pointer; }
.gm-pl.turn { border-color: var(--pc); box-shadow: 0 0 0 1px var(--pc) inset; }
.gm-pl .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--pc); }
.gm-imgs { display: grid; grid-template-columns: repeat(auto-fill, minmax(64px, 1fr)); gap: 8px; margin: 10px 0; max-height: 46vh; overflow-y: auto; }
.gm-imgs .it { position: relative; aspect-ratio: 1; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }
.gm-imgs .it img { width: 100%; height: 100%; object-fit: cover; display: block; }
.gm-imgs .it button { position: absolute; top: 3px; right: 3px; padding: 0 6px; font-size: 11px; background: rgba(0,0,0,.65); border-color: transparent; }
.gm-help p { margin-bottom: 8px; }
`

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader()
    fr.onload = () => resolve(fr.result)
    fr.onerror = () => reject(fr.error)
    fr.readAsDataURL(file)
  })
}

async function toThumb(file) {
  const img = new Image()
  img.src = await readAsDataURL(file)
  await img.decode()
  const w = img.naturalWidth, hgt = img.naturalHeight, s = Math.min(w, hgt)
  if (!s) throw new Error('image vide')
  const c = document.createElement('canvas')
  c.width = c.height = THUMB
  const g = c.getContext('2d')
  g.fillStyle = '#1c1c2b'
  g.fillRect(0, 0, THUMB, THUMB)
  g.drawImage(img, (w - s) / 2, (hgt - s) / 2, s, s, 0, 0, THUMB, THUMB)
  return c.toDataURL('image/jpeg', 0.82)
}

export default {
  async mount(root, ctx) {
    const { h } = ctx
    let prefs = {}
    try { prefs = (await ctx.prefs.get()) || {} } catch { prefs = {} }
    prefs = { size: '4x4', theme: 'animals', mode: 'solo', delay: 900, names: ['Joueur 1', 'Joueur 2'], images: [], best: {}, ...prefs }
    if (!SIZES.some((s) => s[0] === prefs.size)) prefs.size = '4x4'
    if (!THEMES[prefs.theme]) prefs.theme = 'animals'
    prefs.best ||= {}
    prefs.images ||= []

    let st = null
    let pendingHide = null
    let timerIv = 0
    let closeOv = null
    let destroyed = false
    let cursor = 0
    const cardEls = []

    const grid = h('div', { class: 'grid' })
    root.append(h('style', null, CSS), h('div', { class: 'g-memory' }, grid))

    const sizeSel = h('select', { title: 'Taille', onchange: () => changeSetting('size', sizeSel.value) },
      SIZES.map(([k, c, r]) => h('option', { value: k }, `${sizeLabel(k)} · ${c * r / 2} paires`)))
    const themeSel = h('select', { title: 'Thème des cartes', onchange: () => changeSetting('theme', themeSel.value) },
      Object.entries(THEMES).map(([k, [label]]) => h('option', { value: k }, label)))
    const modeSeg = h('div', { class: 'seg' },
      h('button', { 'data-m': 'solo', onclick: () => changeSetting('mode', 'solo') }, 'Solo'),
      h('button', { 'data-m': 'duo', onclick: () => changeSetting('mode', 'duo') }, '2 joueurs'))
    ctx.setToolbar(
      h('button', { class: 'primary', onclick: () => newGame(true) }, 'Nouvelle partie'),
      sizeSel, themeSel, modeSeg,
      h('button', { class: 'icon', title: 'Mes images', onclick: manageImages }, '🖼️'),
      h('button', { class: 'icon', title: 'Options', onclick: options }, '⚙️'),
      h('button', { class: 'icon', title: 'Meilleurs scores (solo)', onclick: () => showScores() }, '🏆'),
      h('button', { class: 'icon', title: 'Aide', onclick: help }, '❔'),
    )

    const savePrefs = () => ctx.prefs.set(prefs)
    const imageMap = () => new Map(prefs.images.map((im) => [im.id, im.d]))

    function elapsed() {
      return st.elapsed + (st.since ? performance.now() - st.since : 0)
    }
    function startClock() {
      if (st.since || st.over) return
      st.since = performance.now()
      clearInterval(timerIv)
      timerIv = setInterval(renderStatus, 500)
    }
    function stopClock() {
      if (st?.since) { st.elapsed = elapsed(); st.since = 0 }
      clearInterval(timerIv)
      timerIv = 0
    }

    function deal(sizeKey, theme) {
      const [, cols, rows] = sizeOf(sizeKey)
      const pairs = cols * rows / 2
      let pool = theme === 'mine' ? prefs.images.map((im) => `img:${im.id}`) : [...THEMES[theme][1]]
      ctx.shuffle(pool)
      if (pool.length < pairs) {
        const extra = ctx.shuffle([...THEMES.animals[1]]).slice(0, pairs - pool.length)
        if (theme === 'mine') ctx.toast(pool.length ? `Seulement ${pool.length} image(s) : complété avec des emoji.` : 'Aucune image ajoutée : cartes emoji utilisées (🖼️ pour en ajouter).')
        pool = pool.concat(extra)
      }
      const picked = pool.slice(0, pairs)
      return ctx.shuffle([...picked, ...picked])
    }

    function newState() {
      return {
        v: 1, size: prefs.size, theme: prefs.theme, mode: prefs.mode,
        cards: deal(prefs.size, prefs.theme), found: [], open: [], moves: 0, elapsed: 0, since: 0,
        scores: [0, 0], turn: 0, names: [...prefs.names], over: false, started: false,
      }
    }

    function build() {
      const [, cols] = sizeOf(st.size)
      const imgs = imageMap()
      grid.replaceChildren()
      grid.classList.remove('done')
      cardEls.length = 0
      grid.style.gridTemplateColumns = `repeat(${cols}, var(--card))`
      st.cards.forEach((k, i) => {
        let face
        if (k.startsWith('img:')) {
          const d = imgs.get(k.slice(4))
          face = d ? h('img', { src: d, alt: '', draggable: 'false' }) : '❓'
        } else face = k
        const el = h('div', { class: 'cd', 'data-i': i, 'data-k': k, style: `--i:${i}` },
          h('div', { class: 'in' }, h('div', { class: 'face back' }), h('div', { class: 'face front' }, face)))
        el.addEventListener('click', () => { grid.classList.remove('kb'); cursor = i; flip(i) })
        grid.append(el)
        cardEls.push(el)
      })
      cursor = Math.min(cursor, st.cards.length - 1)
      refreshCards()
      layout()
    }

    function refreshCards() {
      cardEls.forEach((el, i) => {
        const owner = st.found[i]
        el.classList.toggle('found', owner != null)
        el.classList.toggle('up', st.open.includes(i))
        el.classList.toggle('cur', i === cursor)
        if (owner != null && st.mode === 'duo') el.style.setProperty('--pc', PLAYER_COLORS[owner])
        else el.style.removeProperty('--pc')
      })
    }

    function layout() {
      if (!st) return
      const [, cols, rows] = sizeOf(st.size)
      const W = root.clientWidth - 32, H = root.clientHeight - 32
      const gap = Math.max(6, Math.min(12, Math.round(Math.min(W, H) / 60)))
      const card = Math.max(36, Math.floor(Math.min((W - gap * (cols - 1)) / cols, (H - gap * (rows - 1)) / rows, 150)))
      grid.style.setProperty('--card', `${card}px`)
      grid.style.setProperty('--gap', `${gap}px`)
    }

    function pill(label, value) {
      return h('span', { class: 'pill' }, h('span', { class: 'muted' }, label), h('b', null, value))
    }

    function renderStatus() {
      if (!st) return
      const total = st.cards.length / 2
      const foundPairs = st.scores[0] + st.scores[1]
      if (st.mode === 'solo') {
        ctx.setStatus(pill('Paires', `${foundPairs}/${total}`), pill('Coups', String(st.moves)), pill('Temps', fmtTime(elapsed())))
      } else {
        ctx.setStatus(
          [0, 1].map((p) => h('span', {
            class: `pill gm-pl${st.turn === p && !st.over ? ' turn' : ''}`, style: `--pc:${PLAYER_COLORS[p]}`,
            title: 'Renommer les joueurs', onclick: renamePlayers,
          }, h('span', { class: 'dot' }), st.names[p], h('b', null, String(st.scores[p])))),
          pill('Paires', `${foundPairs}/${total}`))
      }
      sizeSel.value = st.size
      themeSel.value = st.theme
      for (const b of modeSeg.children) b.classList.toggle('on', b.dataset.m === st.mode)
    }

    function serialize() {
      const s = { ...st, elapsed: Math.round(elapsed()), since: 0 }
      if (s.open.length === 2) { s.open = []; s.turn = s.mode === 'duo' ? 1 - s.turn : s.turn }
      return s
    }
    function saveNow() {
      if (!st || st.over || destroyed || !st.started) return
      return ctx.save.set(serialize())
    }

    function hideOpen() {
      if (!pendingHide) return
      clearTimeout(pendingHide)
      pendingHide = null
      for (const i of st.open) cardEls[i]?.classList.remove('miss')
      st.open = []
      if (st.mode === 'duo') st.turn = 1 - st.turn
      refreshCards()
      renderStatus()
    }

    function flip(i) {
      if (!st || st.over || closeOv || document.querySelector('.dlg-back')) return
      if (pendingHide) hideOpen()
      if (st.found[i] != null || st.open.includes(i)) return
      st.started = true
      startClock()
      st.open.push(i)
      ctx.sfx('click')
      refreshCards()
      if (st.open.length < 2) return
      st.moves++
      const [a, b] = st.open
      if (st.cards[a] === st.cards[b]) {
        const who = st.mode === 'duo' ? st.turn : 0
        st.found[a] = st.found[b] = who
        st.scores[who]++
        st.open = []
        for (const j of [a, b]) {
          const el = cardEls[j]
          el.classList.remove('hit'); void el.offsetWidth; el.classList.add('hit')
          el.addEventListener('animationend', () => el.classList.remove('hit'), { once: true })
        }
        setTimeout(() => { if (!destroyed) ctx.sfx('pop') }, FLIP_MS - 80)
        refreshCards()
        renderStatus()
        if (st.found.filter((x) => x != null).length === st.cards.length) {
          finish()
        } else saveNow()
      } else {
        for (const j of [a, b]) cardEls[j].classList.add('miss')
        pendingHide = setTimeout(hideOpen, FLIP_MS + prefs.delay)
        renderStatus()
        saveNow()
      }
    }

    async function finish() {
      st.over = true
      stopClock()
      renderStatus()
      await ctx.save.clear()
      const time = Math.round(st.elapsed)
      const s = st
      setTimeout(() => { if (!destroyed && st === s) grid.classList.add('done') }, FLIP_MS + 200)
      if (s.mode === 'duo') {
        ctx.stats.update({ inc: { played: 1, duo_played: 1 } })
        const [x, y] = s.scores
        const title = x === y ? 'Égalité !' : `${s.names[x > y ? 0 : 1]} gagne !`
        await wait(FLIP_MS + 500)
        if (destroyed || st !== s) return
        ctx.sfx('win')
        closeOv = ctx.overlay({
          title,
          text: `${s.names[0]} : ${x} paire${x > 1 ? 's' : ''}\n${s.names[1]} : ${y} paire${y > 1 ? 's' : ''}\n${s.moves} coups · ${fmtTime(time)}`,
          buttons: [{ label: 'Rejouer', primary: true, onClick: () => { closeOv = null; newGame() } }],
        })
        return
      }
      const prev = prefs.best[s.size]
      if (!prev || s.moves < prev.moves || (s.moves === prev.moves && time < prev.time)) {
        prefs.best[s.size] = { moves: s.moves, time }
        savePrefs()
      }
      const hardest = [...SIZES].reverse().find(([k]) => prefs.best[k])
      const hb = hardest ? `${prefs.best[hardest[0]].moves} coups (${sizeLabel(hardest[0])})` : ''
      ctx.stats.update({ inc: { played: 1, won: 1 }, set: { hub_best: hb } })
      let r = null
      try {
        r = await ctx.scores.submit(`solo-${s.size}`, s.moves, { lowerBetter: true, meta: { time, theme: s.theme } })
      } catch (e) { ctx.toast(`Score : ${e.message}`, 'err') }
      await wait(FLIP_MS + 500)
      if (destroyed || st !== s) return
      ctx.sfx('win')
      const pairs = s.cards.length / 2
      const rankTxt = r ? (r.record ? '🏅 Nouveau record !' : `Rang ${r.rank} sur ${r.total}`) : ''
      closeOv = ctx.overlay({
        title: 'Toutes les paires !',
        text: `${s.moves} coups pour ${pairs} paires · ${fmtTime(time)}\nPrécision : ${Math.round(pairs / s.moves * 100)} %${rankTxt ? '\n' + rankTxt : ''}`,
        buttons: [
          { label: '🏆 Scores', onClick: () => { closeOv = null; showScores(r?.id) } },
          { label: 'Rejouer', primary: true, onClick: () => { closeOv = null; newGame() } },
        ],
      })
    }

    const wait = (ms) => new Promise((r) => setTimeout(r, ms))

    async function newGame(ask = false) {
      if (ask && st && st.started && !st.over) {
        const ok = await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Nouvelle partie' })
        if (!ok) return false
      }
      closeOv?.(); closeOv = null
      clearTimeout(pendingHide); pendingHide = null
      stopClock()
      st = newState()
      ctx.save.clear()
      build()
      renderStatus()
      return true
    }

    async function changeSetting(key, value) {
      if (st && st[key] === value) return
      const old = prefs[key]
      prefs[key] = value
      if (!(await newGame(true))) { prefs[key] = old; renderStatus(); return }
      savePrefs()
    }

    function showScores(highlightId = null) {
      ctx.showScores(`solo-${st?.size || prefs.size}`, {
        lowerBetter: true, highlightId,
        fmt: (s, m) => `${s} coups${m?.time != null ? ` · ${fmtTime(m.time)}` : ''}`,
        variants: SIZES.map(([k, c, r]) => [`solo-${k}`, `${sizeLabel(k)} (${c * r / 2} paires)`]),
      })
    }

    async function renamePlayers() {
      const ins = [0, 1].map((p) => h('input', { maxlength: 20, value: st.names[p] }))
      const ok = await ctx.dialog({
        title: 'Joueurs',
        body: h('div', { class: 'form' }, h('label', null, 'Joueur 1'), ins[0], h('label', null, 'Joueur 2'), ins[1]),
        buttons: [{ label: 'Annuler', value: false }, { label: 'Enregistrer', value: true, primary: true }],
      })
      if (!ok) return
      const names = ins.map((el, p) => el.value.trim().slice(0, 20) || `Joueur ${p + 1}`)
      prefs.names = names
      st.names = [...names]
      savePrefs()
      renderStatus()
      saveNow()
    }

    async function options() {
      const delSel = h('select', null, DELAYS.map(([v, l]) => h('option', { value: v, selected: v === prefs.delay }, l)))
      const ins = [0, 1].map((p) => h('input', { maxlength: 20, value: prefs.names[p] }))
      const ok = await ctx.dialog({
        title: 'Options',
        body: h('div', { class: 'form' },
          h('label', null, 'Délai de retour'), delSel,
          h('label', null, 'Joueur 1'), ins[0],
          h('label', null, 'Joueur 2'), ins[1]),
        buttons: [{ label: 'Annuler', value: false }, { label: 'Enregistrer', value: true, primary: true }],
      })
      if (!ok) return
      prefs.delay = +delSel.value
      prefs.names = ins.map((el, p) => el.value.trim().slice(0, 20) || `Joueur ${p + 1}`)
      if (st) st.names = [...prefs.names]
      savePrefs()
      renderStatus()
      saveNow()
    }

    async function manageImages() {
      const list = h('div', { class: 'gm-imgs' })
      const info = h('div', { class: 'muted' })
      const file = h('input', { type: 'file', accept: 'image/*', multiple: true, class: 'hidden', 'data-role': 'gm-file' })
      let changed = false
      const render = () => {
        info.textContent = `${prefs.images.length}/${MAX_IMAGES} images · redimensionnées en ${THUMB}×${THUMB}, stockées localement.`
        list.replaceChildren(...prefs.images.map((im) => h('div', { class: 'it' },
          h('img', { src: im.d, alt: '' }),
          h('button', { title: 'Supprimer', onclick: () => { prefs.images = prefs.images.filter((x) => x.id !== im.id); changed = true; render() } }, '✕'))))
        if (!prefs.images.length) list.append(h('div', { class: 'muted', style: { gridColumn: '1 / -1' } }, 'Aucune image pour l’instant.'))
        addBtn.disabled = prefs.images.length >= MAX_IMAGES
      }
      const addBtn = h('button', { onclick: () => file.click() }, '+ Ajouter des images')
      file.addEventListener('change', async () => {
        const files = [...file.files]
        file.value = ''
        const room = MAX_IMAGES - prefs.images.length
        if (files.length > room) ctx.toast(`Limite de ${MAX_IMAGES} images : ${files.length - room} ignorée(s).`, 'err')
        let bad = 0
        for (const f of files.slice(0, Math.max(0, room))) {
          try {
            prefs.images.push({ id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7), d: await toThumb(f) })
            changed = true
          } catch { bad++ }
        }
        if (bad) ctx.toast(`${bad} fichier(s) illisible(s).`, 'err')
        render()
      })
      const clearBtn = h('button', { class: 'danger', onclick: () => { prefs.images = []; changed = true; render() } }, 'Tout supprimer')
      render()
      await ctx.dialog({
        title: 'Mes images',
        wide: true,
        body: h('div', null,
          h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' } }, addBtn, clearBtn, file),
          list, info,
          h('div', { class: 'muted', style: { marginTop: '6px' } }, 'Choisissez le thème « Mes images » pour jouer avec. Il faut autant d’images que de paires (sinon des emoji complètent).')),
        buttons: [{ label: 'Fermer', value: true, primary: true }],
      })
      if (!changed) return
      await savePrefs()
      if (st) {
        const imgs = imageMap()
        cardEls.forEach((el, i) => {
          const k = st.cards[i]
          if (!k.startsWith('img:')) return
          const d = imgs.get(k.slice(4))
          el.querySelector('.front').replaceChildren(d ? h('img', { src: d, alt: '', draggable: 'false' }) : '❓')
        })
      }
    }

    function help() {
      ctx.dialog({
        title: 'Mémoire — règles',
        body: h('div', { class: 'gm-help' },
          h('p', null, 'Retournez deux cartes à chaque coup. Si elles sont identiques, la paire reste visible ; sinon elles se retournent après un court délai (réglable dans ⚙️).'),
          h('p', null, 'Solo : trouvez toutes les paires en un minimum de coups (le temps départage). Un classement par taille de grille.'),
          h('p', null, '2 joueurs : chacun son tour sur le même écran ; trouver une paire permet de rejouer. Cliquez sur un nom pour le modifier.'),
          h('p', null, 'Thème « Mes images » : ajoutez vos photos via 🖼️ (jusqu’à 40).'),
          h('p', null, h('span', { class: 'kbd' }, '←↑→↓'), ' déplacer le curseur, ', h('span', { class: 'kbd' }, 'Espace'), ' / ', h('span', { class: 'kbd' }, 'Entrée'), ' ou bouton A : retourner. Souris : cliquer une carte.')),
      })
    }

    ctx.onAction((a) => {
      if (!st || closeOv) return
      const [, cols] = sizeOf(st.size)
      const n = st.cards.length
      const kb = grid.classList.contains('kb')
      grid.classList.add('kb')
      if (a === 'a') { if (kb) flip(cursor); refreshCards(); return }
      const d = { left: -1, right: 1, up: -cols, down: cols }[a]
      if (d == null) return
      if (kb) {
        if (a === 'left' || a === 'right') {
          const row = Math.floor(cursor / cols)
          cursor = row * cols + ((cursor % cols) + d + cols) % cols
        } else cursor = (cursor + d + n) % n
        ctx.sfx('tick')
      }
      refreshCards()
    })
    const onMouse = () => grid.classList.remove('kb')
    grid.addEventListener('mousemove', onMouse)

    const ro = new ResizeObserver(() => layout())
    ro.observe(root)

    let saved = null
    try { saved = await ctx.save.get() } catch { saved = null }
    if (saved && saved.v === 1 && Array.isArray(saved.cards) && SIZES.some((s) => s[0] === saved.size)) {
      st = { ...newState(), ...saved, since: 0, over: false, open: (saved.open || []).slice(0, 1) }
      st.found = Array.from({ length: st.cards.length }, (_, i) => saved.found?.[i] ?? null)
      build()
      renderStatus()
    } else {
      st = newState()
      build()
      renderStatus()
    }

    return {
      pause() {
        if (!st) return
        if (pendingHide) hideOpen()
        stopClock()
        renderStatus()
        saveNow()
      },
      destroy() {
        if (st && !st.over) { if (pendingHide) hideOpen(); stopClock(); saveNow() }
        destroyed = true
        clearTimeout(pendingHide)
        clearInterval(timerIv)
        ro.disconnect()
      },
    }
  },
}
