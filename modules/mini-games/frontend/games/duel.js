// Shared shell for the two-player board games (connect4, tictactoe, reversi, chess…): setup, players, AI worker,
// undo, save/resume, session score, stats. Each game supplies its rules and its board view (see mountDuel).
// Optional spec extras: init/replay receive a custom start (S.start, e.g. a FEN) as last argument and the worker gets it
// as `start`; view api.load({ start, moves, first }) starts an imported (unrated) game; api.state() exposes the record;
// spec.firstLabel renames « Qui commence ? »; spec.note(pos) appends a short remark to the turn message.
import { h } from '../common.js'

export const LEVELS = [['easy', 'Facile'], ['medium', 'Moyen'], ['hard', 'Difficile']]
export const LEVEL_LABEL = Object.fromEntries(LEVELS)
const SESSIONS = {}
const MIN_AI_DELAY = 450

const CSS = `
.g-duel { flex: 1; min-height: 0; display: flex; flex-direction: column; align-items: center; padding: 10px 12px 8px; gap: 8px; }
.g-duel .bar { display: flex; gap: 10px; align-items: stretch; justify-content: center; width: 100%; max-width: 720px; flex-shrink: 0; }
.g-duel .pc { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 10px;
  background: var(--surface); border: 1px solid var(--border); transition: border-color .2s, box-shadow .2s, opacity .2s; opacity: .7; }
.g-duel .pc.turn { opacity: 1; border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.g-duel .pc.win { opacity: 1; border-color: var(--ok); box-shadow: 0 0 0 1px var(--ok) inset; }
.g-duel .pc .sw { width: 22px; height: 22px; flex-shrink: 0; display: grid; place-items: center; }
.g-duel .pc .nm { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.g-duel .pc .sub { color: var(--muted); font-size: 12px; white-space: nowrap; }
.g-duel .pc .info { min-width: 0; display: flex; flex-direction: column; }
.g-duel .pc .ex { margin-left: auto; font-weight: 700; font-size: 18px; font-variant-numeric: tabular-nums; }
.g-duel .dots { display: inline-flex; gap: 3px; margin-left: 4px; vertical-align: middle; }
.g-duel .dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--accent-h); animation: g-duel-dot 1s infinite; }
.g-duel .dots i:nth-child(2) { animation-delay: .15s } .g-duel .dots i:nth-child(3) { animation-delay: .3s }
@keyframes g-duel-dot { 0%, 80%, 100% { opacity: .25; transform: translateY(0) } 40% { opacity: 1; transform: translateY(-3px) } }
.g-duel .host { flex: 1; min-height: 0; width: 100%; display: grid; place-items: center; position: relative; }
.g-duel .msg { min-height: 20px; color: var(--muted); text-align: center; flex-shrink: 0; transition: color .2s; }
.g-duel .msg.hl { color: var(--warn); }
.g-duel-setup .row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 12px; }
.g-duel-setup .row > label { color: var(--muted); font-size: 12px; }
.g-duel-setup .names { display: flex; gap: 8px; } .g-duel-setup .names input { flex: 1; min-width: 0; }
.g-duel-stats table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
.g-duel-stats td, .g-duel-stats th { padding: 5px 8px; border-bottom: 1px solid var(--border); text-align: right; }
.g-duel-stats th { color: var(--muted); font-weight: 500; } .g-duel-stats td:first-child, .g-duel-stats th:first-child { text-align: left; }
.g-duel-help p { margin-bottom: 8px; } .g-duel-help ul { margin: 0 0 8px 18px; }
`

function seg(options, value, onChange) {
  const el = h('div', { class: 'seg' })
  const set = (v) => { value = v; for (const b of el.children) b.classList.toggle('on', b.dataset.v === v) }
  for (const [v, label] of options) el.append(h('button', { type: 'button', 'data-v': v, onclick: () => { set(v); onChange?.(v) } }, label))
  set(value)
  return { el, get: () => value, set }
}

export async function mountDuel(root, ctx, spec) {
  const session = (SESSIONS[spec.id] ??= { ai: {}, pvp: { wins: {}, draws: 0 } })
  root.append(h('style', null, CSS + spec.css))
  const barEl = h('div', { class: 'bar' })
  const hostEl = h('div', { class: 'host' })
  const msgEl = h('div', { class: 'msg' })
  const wrap = h('div', { class: `g-duel g-${spec.id}` }, barEl, hostEl, msgEl)
  root.append(wrap)

  let prefs = { mode: 'ai', level: 'medium', first: 'human', names: ['', ''], altNext: 0, variant: spec.variants?.[0][0] ?? null }
  try { prefs = { ...prefs, ...((await ctx.prefs.get()) || {}) } } catch { /* defaults */ }
  if (spec.variants && !spec.variants.some(([v]) => v === prefs.variant)) prefs.variant = spec.variants[0][0]

  let S = null
  let pos = null
  let worker = null, aiToken = 0, thinking = false, aiTimer = 0
  let destroyed = false
  let msgTimer = 0
  let overlayTimer = 0
  let lastAnim = null

  const view = spec.view(ctx, {
    pick: (m) => tryHuman(m),
    canPlay: () => humanTurn(),
    state: () => S,
    load: (opts) => newGame(false, { ...opts, imported: true }),
  })
  hostEl.append(view.el)

  const undoBtn = h('button', { title: 'Annuler le dernier coup (Retour arrière)', onclick: () => undo() }, '↶ Annuler')
  const sessionPill = h('span', { class: 'pill', title: 'Score de la session (victoires · nuls · défaites)' })
  ctx.setToolbar([
    h('button', { class: 'primary', onclick: () => newGame(), title: 'Nouvelle partie avec les mêmes réglages' }, '↻ Nouvelle partie'),
    h('button', { onclick: () => setup(), title: 'Mode, niveau, joueurs' }, '⚙ Réglages'),
    undoBtn,
    spec.scoreVariants ? h('button', { class: 'icon', title: 'Meilleurs scores', onclick: () => showScores() }, '🏆') : null,
    h('button', { class: 'icon', title: 'Statistiques', onclick: () => showStats() }, '📊'),
    h('button', { class: 'icon', title: 'Aide', onclick: () => help() }, '❔'),
  ].filter(Boolean))
  ctx.setStatus(sessionPill)

  const playerName = () => prefs.names[0] || ctx.player() || 'Vous'
  const isOverlay = () => !!root.querySelector(':scope > .overlay')
  const humanTurn = () => !!(S && pos && !S.over && !thinking && !S.players[spec.toMove(pos)].ai && !isOverlay())

  let sticky = null
  function message(text, hl = false, ms = 0) {
    clearTimeout(msgTimer)
    sticky = ms ? { text, hl, until: performance.now() + ms } : null
    show(text, hl)
    if (ms) msgTimer = setTimeout(() => { sticky = null; updateMessage() }, ms)
  }
  function show(text, hl) {
    msgEl.textContent = text || ''
    msgEl.classList.toggle('hl', !!hl)
  }
  function updateMessage() {
    if (sticky && performance.now() < sticky.until) return show(sticky.text, sticky.hl)
    if (!S) return show('')
    if (S.over) {
      const w = S.over.winner
      return show(w < 0 ? 'Match nul.' : S.mode === 'ai' && !S.players[w].ai ? 'Vous avez gagné.' : `${S.players[w].name} a gagné.`)
    }
    const p = S.players[spec.toMove(pos)]
    const note = spec.note?.(pos)
    if (thinking) return show(note ? `${note} Réflexion…` : 'Réflexion…', !!note)
    show((S.mode === 'ai' ? 'À vous de jouer.' : `À ${p.name} de jouer.`) + (note ? ` ${note}` : ''), !!note)
  }

  function renderBar() {
    const turn = S && !S.over ? spec.toMove(pos) : -1
    barEl.replaceChildren(...[0, 1].map((i) => {
      const p = S.players[i]
      const extra = spec.extra?.(pos, i)
      return h('div', { class: `pc${turn === i ? ' turn' : ''}${S.over && S.over.winner === i ? ' win' : ''}` },
        h('div', { class: 'sw' }, spec.swatch(i)),
        h('div', { class: 'info' },
          h('div', { class: 'nm', title: p.name }, p.name),
          h('div', { class: 'sub' }, spec.sides[i] + (p.ai ? ` · ${LEVEL_LABEL[S.level]}` : ''),
            thinking && turn === i ? h('span', { class: 'dots' }, h('i'), h('i'), h('i')) : null)),
        extra != null ? h('div', { class: 'ex' }, extra) : null)
    }))
  }

  function renderStatus() {
    wrap.dataset.moves = S ? S.moves.filter((e) => e.m !== -1).length : 0
    if (!S || S.mode === 'ai') {
      const lv = S?.level || prefs.level
      const s = session.ai[lv] || { w: 0, d: 0, l: 0 }
      sessionPill.textContent = `${LEVEL_LABEL[lv]} : ${s.w} V · ${s.d} N · ${s.l} D`
    } else {
      const names = S.players.map((p) => p.name)
      sessionPill.textContent = `${names.map((n) => `${n} ${session.pvp.wins[n] || 0}`).join(' – ')} · ${session.pvp.draws} nul${session.pvp.draws > 1 ? 's' : ''}`
    }
    undoBtn.disabled = !canUndo()
  }

  function render(anim = null) {
    if (!S) return
    renderBar()
    renderStatus()
    view.render(pos, {
      anim,
      last: [...S.moves].reverse().find((e) => e.m !== -1)?.m ?? null,
      over: S.over,
      interactive: humanTurn(),
      turn: S.over ? -1 : spec.toMove(pos),
      busy: thinking,
    })
    updateMessage()
  }

  function fit() {
    const w = hostEl.clientWidth, hh = hostEl.clientHeight
    if (w > 0 && hh > 0) view.fit(w, hh)
  }
  const ro = new ResizeObserver(fit)
  ro.observe(hostEl)

  function buildPlayers(mode, first) {
    let firstIdx = first === 'human' ? 0 : first === 'ai' ? 1 : prefs.altNext
    if (first === 'alt') prefs.altNext ^= 1
    if (mode === 'ai') {
      const human = { name: playerName(), ai: false }
      const bot = { name: `Ordinateur`, ai: true }
      return firstIdx === 0 ? [human, bot] : [bot, human]
    }
    const a = { name: prefs.names[0] || ctx.player() || 'Joueur 1', ai: false }
    const b = { name: prefs.names[1] || 'Joueur 2', ai: false }
    if (a.name === b.name) b.name += ' (2)'
    return firstIdx === 0 ? [a, b] : [b, a]
  }

  function stopAI() {
    clearTimeout(aiTimer)
    aiToken++
    if (thinking && worker) { worker.terminate(); worker = null }
    thinking = false
  }

  function closeOverlay() {
    clearTimeout(overlayTimer)
    root.querySelector(':scope > .overlay')?.remove()
  }

  async function newGame(force = false, opts = {}) {
    if (!force && S && !S.over && S.moves.some((e) => !S.players[e.p].ai && e.m !== -1)) {
      if (!(await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Nouvelle partie' }))) return false
    }
    stopAI()
    closeOverlay()
    const players = buildPlayers(prefs.mode, opts.first ?? prefs.first)
    ctx.prefs.set(prefs)
    S = { v: 1, game: spec.id, mode: prefs.mode, level: prefs.level, variant: prefs.variant, players, moves: [], over: null }
    if (opts.start != null) S.start = opts.start
    if (opts.imported) S.imported = true
    pos = spec.init(S.variant, S.start)
    for (const m of opts.moves || []) { S.moves.push({ p: spec.toMove(pos), m }); spec.apply(pos, m) }
    lastAnim = null
    view.reset?.(pos, S.variant)
    fit()
    render()
    save()
    proceed()
    return true
  }

  function save() {
    if (!S || S.over) return
    if (!S.moves.length) return ctx.save.clear()
    ctx.save.set(S)
  }

  function proceed() {
    if (!S || S.over || destroyed) return
    const res = spec.result(pos)
    if (res) return finish(res)
    if (spec.legal(pos).length === 0) {
      const p = spec.toMove(pos)
      spec.pass(pos)
      S.moves.push({ p, m: -1 })
      message(`${S.players[p].name} n’a aucun coup possible et passe son tour.`, true, 2600)
      ctx.sfx('error')
      render()
      save()
      const r2 = spec.result(pos)
      if (r2) return finish(r2)
    }
    maybeAI()
  }

  function maybeAI() {
    if (!S || S.over || destroyed) return
    if (!S.players[spec.toMove(pos)].ai) { render(); return }
    requestAI()
  }

  function ensureWorker() {
    if (worker) return worker
    worker = spec.worker()
    worker.onmessage = (e) => onAIMove(e.data)
    worker.onerror = (e) => {
      e.preventDefault?.()
      ctx.toast('Erreur de l’ordinateur : coup choisi au hasard.', 'err')
      worker?.terminate(); worker = null
      const legal = spec.legal(pos)
      onAIMove({ token: aiToken, move: legal[Math.floor(Math.random() * legal.length)], started: performance.now() - MIN_AI_DELAY })
    }
    return worker
  }

  let aiStarted = 0
  function requestAI() {
    const token = ++aiToken
    thinking = true
    aiStarted = performance.now()
    render()
    const delay = Math.max(0, (spec.animMs?.(lastAnim) ?? 0) - 100)
    aiTimer = setTimeout(() => {
      if (token !== aiToken || destroyed) return
      ensureWorker().postMessage({ token, level: S.level, variant: S.variant, moves: S.moves.map((e) => e.m), start: S.start ?? null })
    }, delay)
  }

  function onAIMove({ token, move }) {
    if (token !== aiToken || !S || S.over || destroyed) return
    const wait = Math.max(0, MIN_AI_DELAY - (performance.now() - aiStarted))
    aiTimer = setTimeout(() => {
      if (token !== aiToken || destroyed) return
      thinking = false
      if (!spec.legal(pos).some((m) => m === move)) {
        const legal = spec.legal(pos)
        move = legal[Math.floor(Math.random() * legal.length)]
      }
      play(move)
    }, wait)
  }

  function tryHuman(m) {
    if (!humanTurn()) return
    if (!spec.legal(pos).some((x) => x === m)) { ctx.sfx('error'); return }
    play(m)
  }

  function play(m) {
    const p = spec.toMove(pos)
    const anim = spec.apply(pos, m)
    lastAnim = anim
    S.moves.push({ p, m })
    ctx.sfx(spec.sfx?.(anim) || 'drop')
    render(anim)
    save()
    proceed()
  }

  function canUndo() {
    if (!S || S.over) return false
    if (S.mode === 'ai') return S.moves.some((e) => !S.players[e.p].ai && e.m !== -1)
    return S.moves.some((e) => e.m !== -1)
  }

  function undo() {
    if (!canUndo() || isOverlay()) return
    stopAI()
    while (S.moves.length) {
      const e = S.moves.pop()
      if (e.m === -1) continue
      if (S.mode === 'pvp' || !S.players[e.p].ai) break
    }
    pos = spec.replay(S.variant, S.moves.map((e) => e.m), S.start)
    lastAnim = null
    ctx.sfx('move')
    message('Coup annulé.', false, 1200)
    render()
    save()
    if (S.players[spec.toMove(pos)].ai) proceed()
  }

  async function finish(res) {
    S.over = res
    thinking = false
    render()
    ctx.save.clear()
    const w = res.winner
    let title, text = spec.endText?.(pos, res) || ''
    let scoreRes = null
    const hi = S.mode === 'ai' ? S.players.findIndex((p) => !p.ai) : -1
    if (S.imported) {
      ctx.sfx(w < 0 ? 'line' : S.mode === 'ai' && w !== hi ? 'lose' : 'win')
      title = w < 0 ? '🤝 Match nul' : S.mode === 'ai' ? (w === hi ? '🎉 Victoire !' : '😞 Défaite') : `🎉 ${S.players[w].name} gagne !`
      text += '\nPartie importée : non comptée dans les statistiques.'
    } else if (S.mode === 'ai') {
      const outcome = w < 0 ? 'd' : w === hi ? 'w' : 'l'
      const s = (session.ai[S.level] ??= { w: 0, d: 0, l: 0 }); s[outcome]++
      ctx.sfx(outcome === 'w' ? 'win' : outcome === 'l' ? 'lose' : 'line')
      title = outcome === 'w' ? '🎉 Victoire !' : outcome === 'l' ? '😞 Défaite' : '🤝 Match nul'
      const inc = { played: 1, [`${S.level}_played`]: 1, [`${S.level}_${outcome}`]: 1 }
      if (outcome === 'w') inc.won = 1
      const st = await ctx.stats.update({ inc })
      const best = [...LEVELS].reverse().find(([k]) => st?.[`${k}_w`] > 0)
      if (best) ctx.stats.update({ set: { hub_best: `${st[`${best[0]}_w`]} V en ${best[1]}` } })
      if (outcome === 'w' && spec.scoreFor) {
        const sc = spec.scoreFor(pos, hi, S.level)
        if (sc) {
          try {
            const r = await ctx.scores.submit(sc.variant, sc.score, { meta: sc.meta || {} })
            scoreRes = { ...r, variant: sc.variant }
            text += r.record ? '\nNouveau record !' : r.rank ? `\nRang ${r.rank} au classement` : ''
          } catch (e) { ctx.toast(e.message, 'err') }
        }
      }
    } else {
      if (w < 0) session.pvp.draws++
      else session.pvp.wins[S.players[w].name] = (session.pvp.wins[S.players[w].name] || 0) + 1
      ctx.sfx(w < 0 ? 'line' : 'win')
      title = w < 0 ? '🤝 Match nul' : `🎉 ${S.players[w].name} gagne !`
      ctx.stats.update({ inc: { played: 1, pvp_played: 1 } })
    }
    renderStatus()
    if (destroyed) return
    overlayTimer = setTimeout(() => {
      if (destroyed || !S?.over) return
      ctx.overlay({
        title, text: text.trim(),
        buttons: [
          { label: 'Voir le plateau' },
          ...(scoreRes ? [{ label: '🏆 Classement', onClick: () => showScores(scoreRes.variant, scoreRes.id) }] : []),
          { label: '⚙ Réglages', onClick: () => setup() },
          { label: 'Rejouer', primary: true, onClick: () => newGame(true) },
        ],
      })
    }, spec.endDelay ?? 900)
  }

  async function setup() {
    const mode = seg([['ai', 'Contre l’ordinateur'], ['pvp', '2 joueurs']], prefs.mode, () => sync())
    const level = seg(LEVELS, prefs.level)
    const firstAI = [['human', 'Moi'], ['ai', 'Ordinateur'], ['alt', 'Alterner']]
    const firstPvp = [['human', 'Joueur 1'], ['ai', 'Joueur 2'], ['alt', 'Alterner']]
    const firstHost = h('div')
    let first = null
    const variant = spec.variants ? seg(spec.variants, prefs.variant) : null
    const n1 = h('input', { maxlength: 24, value: prefs.names[0] || '', placeholder: ctx.player() || 'Joueur 1' })
    const n2 = h('input', { maxlength: 24, value: prefs.names[1] || '', placeholder: 'Joueur 2' })
    const levelRow = h('div', { class: 'row' }, h('label', null, 'Niveau de l’ordinateur'), level.el)
    function sync() {
      const m = mode.get()
      levelRow.classList.toggle('hidden', m !== 'ai')
      n2.classList.toggle('hidden', m !== 'pvp')
      n1.placeholder = m === 'ai' ? (ctx.player() || 'Votre nom') : (ctx.player() || 'Joueur 1')
      const cur = first ? first.get() : prefs.first
      first = seg(m === 'ai' ? firstAI : firstPvp, cur)
      firstHost.replaceChildren(first.el)
    }
    sync()
    const body = h('div', { class: 'g-duel-setup' },
      h('div', { class: 'row' }, h('label', null, 'Mode'), mode.el),
      variant ? h('div', { class: 'row' }, h('label', null, 'Variante'), variant.el) : null,
      levelRow,
      h('div', { class: 'row' }, h('label', null, spec.firstLabel || 'Qui commence ?'), firstHost),
      h('div', { class: 'row' }, h('label', null, 'Noms des joueurs'), h('div', { class: 'names' }, n1, n2)))
    const ok = await ctx.dialog({
      title: 'Réglages de la partie', body,
      buttons: [{ label: 'Annuler', value: false }, { label: 'Commencer', value: true, primary: true }],
    })
    if (!ok) return
    const before = S && !S.over && S.moves.some((e) => !S.players[e.p].ai && e.m !== -1)
    if (before && !(await ctx.confirm('Abandonner la partie en cours ?', { ok: 'Nouvelle partie' }))) return
    prefs = { ...prefs, mode: mode.get(), level: level.get(), first: first.get(), variant: variant ? variant.get() : prefs.variant,
      names: [n1.value.trim(), n2.value.trim()] }
    newGame(true)
  }

  async function showStats() {
    let st = {}
    try { st = (await ctx.stats.get()) || {} } catch (e) { ctx.toast(e.message, 'err') }
    const rows = LEVELS.map(([k, label]) => {
      const p = st[`${k}_played`] || 0, w = st[`${k}_w`] || 0, d = st[`${k}_d`] || 0, l = st[`${k}_l`] || 0
      const s = session.ai[k] || { w: 0, d: 0, l: 0 }
      return h('tr', null, h('td', null, label), h('td', null, p), h('td', null, w), h('td', null, d), h('td', null, l),
        h('td', null, p ? `${Math.round((100 * w) / p)} %` : '—'), h('td', { class: 'muted' }, `${s.w}/${s.d}/${s.l}`))
    })
    await ctx.dialog({
      title: 'Statistiques',
      body: h('div', { class: 'g-duel-stats' },
        h('table', null,
          h('thead', null, h('tr', null, ['Contre l’ordinateur', 'Parties', 'V', 'N', 'D', 'Victoires', 'Session'].map((t) => h('th', null, t)))),
          h('tbody', null, rows)),
        h('p', { class: 'muted', style: { marginTop: '10px' } },
          `Parties à deux joueurs : ${st.pvp_played || 0}. Session = victoires / nuls / défaites depuis l’ouverture de l’application.`)),
      buttons: [{ label: 'Fermer', value: true, primary: true }],
    })
  }

  function showScores(variant, highlightId = null) {
    const v = variant || (S?.mode === 'ai' ? S.level : prefs.level)
    ctx.showScores(v, { highlightId, variants: spec.scoreVariants, fmt: spec.scoreFmt, title: spec.scoreTitle })
  }

  function help() {
    ctx.dialog({
      title: `Aide — ${spec.name}`, wide: true,
      body: h('div', { class: 'g-duel-help' }, spec.help(),
        h('p', null, h('b', null, 'Contrôles : '), 'souris, ou ', h('span', { class: 'kbd' }, '←'), ' ', h('span', { class: 'kbd' }, '→'), ' ',
          h('span', { class: 'kbd' }, '↑'), ' ', h('span', { class: 'kbd' }, '↓'), ' pour déplacer le curseur, ',
          h('span', { class: 'kbd' }, 'Espace'), '/', h('span', { class: 'kbd' }, 'Entrée'), ' pour jouer, ',
          h('span', { class: 'kbd' }, 'Retour arrière'), ' pour annuler (manette : croix, A, B).'),
        h('p', { class: 'muted' }, 'Contre l’ordinateur, « Annuler » retire aussi sa réponse. La partie en cours est sauvegardée automatiquement.')),
    })
  }

  ctx.onAction((action) => {
    if (!S || isOverlay()) return
    if (action === 'b') return undo()
    if (action === 'a') { const m = view.cursorPick(); if (m != null) tryHuman(m); return }
    const d = { left: [-1, 0], right: [1, 0], up: [0, -1], down: [0, 1] }[action]
    if (d) { view.moveCursor(d[0], d[1]); ctx.sfx('tick') }
  })

  let saved = null
  try { saved = await ctx.save.get() } catch { /* none */ }
  if (saved && saved.v === 1 && saved.game === spec.id && Array.isArray(saved.moves) && saved.players?.length === 2) {
    try {
      S = { ...saved, over: null }
      if (spec.variants && !spec.variants.some(([v]) => v === S.variant)) throw new Error('variant')
      pos = spec.replay(S.variant, S.moves.map((e) => e.m), S.start)
      if (spec.result(pos)) throw new Error('finished')
      view.reset?.(pos, S.variant)
      fit()
      render()
      message('Partie reprise.', false, 1500)
      proceed()
    } catch {
      S = null
      ctx.save.clear()
    }
  }
  if (!S) await newGame(true)

  return {
    pause() { save() },
    destroy() {
      destroyed = true
      save()
      clearTimeout(aiTimer); clearTimeout(msgTimer); clearTimeout(overlayTimer)
      worker?.terminate(); worker = null
      ro.disconnect()
      view.destroy?.()
    },
  }
}
