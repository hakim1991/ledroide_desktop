// Shared runtime for every game: bridge access, scores/saves/stats/prefs, sound, input, UI helpers.
// A game is an ES module in games/<id>.js:  export default { mount(root, ctx) { ...; return { destroy(), pause() } } }

export const MODULE_ID = 'mini-games'

let bridge = window.ledroide
if (!bridge || !bridge.call) {
  const { mockBridge } = await import('./dev-mock.js')
  bridge = mockBridge
}

export function call(action, payload = {}, opts = {}) {
  return bridge.call(MODULE_ID, action, payload, { timeout: 20000, ...opts })
}

// ── DOM helpers ───────────────────────────────────────────────────────────────

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag)
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue
      if (k === 'class') el.className = v
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v)
      else if (k === 'dataset') Object.assign(el.dataset, v)
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v)
      else if (k === 'html') el.innerHTML = v
      else if (v === true) el.setAttribute(k, '')
      else el.setAttribute(k, v)
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue
    el.append(c instanceof Node ? c : document.createTextNode(String(c)))
  }
  return el
}

export const format = {
  time(ms) {
    ms = Math.max(0, Math.round(ms))
    const s = Math.floor(ms / 1000), m = Math.floor(s / 60), hh = Math.floor(m / 60)
    const t = `${String(s % 60).padStart(2, '0')}.${Math.floor((ms % 1000) / 100)}`
    return hh ? `${hh}:${String(m % 60).padStart(2, '0')}:${t}` : `${m}:${t}`
  },
  int(n) { return Math.round(n).toLocaleString('fr-FR') },
  date(iso) {
    const d = new Date(iso)
    return isNaN(d) ? '' : d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit', year: '2-digit' })
  },
}

export function shuffle(a, rnd = Math.random) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]]
  }
  return a
}

// Deterministic PRNG (mulberry32) — for daily puzzles and reproducible boards.
export function seeded(seed) {
  let s = typeof seed === 'number' ? seed >>> 0 : [...String(seed)].reduce((a, c) => (Math.imul(a ^ c.charCodeAt(0), 2654435761) >>> 0), 2166136261)
  return () => {
    s = (s + 0x6D2B79F5) >>> 0
    let t = s
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function todayKey(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// ── Toasts & dialogs ──────────────────────────────────────────────────────────

export function toast(msg, kind = '') {
  const box = document.getElementById('toasts')
  const t = h('div', { class: `toast ${kind}` }, msg)
  box.append(t)
  setTimeout(() => t.classList.add('out'), 2600)
  setTimeout(() => t.remove(), 3000)
}

// Modal dialog over the whole window. buttons: [{label, value, primary, danger}]. Resolves with the clicked value
// (or undefined on Escape / backdrop click when `dismissable`).
export function dialog({ title, body, buttons = [{ label: 'OK', value: true, primary: true }], dismissable = true, wide = false }) {
  return new Promise((resolve) => {
    const back = h('div', { class: 'dlg-back' })
    const close = (v) => { back.remove(); document.removeEventListener('keydown', onKey, true); resolve(v) }
    const onKey = (e) => {
      if (e.key === 'Escape' && dismissable) { e.stopPropagation(); e.preventDefault(); close(undefined) }
      if (e.key === 'Enter' && !(e.target instanceof HTMLTextAreaElement)) {
        const p = buttons.find((b) => b.primary)
        if (p) { e.stopPropagation(); e.preventDefault(); close(typeof p.value === 'function' ? p.value() : p.value) }
      }
    }
    const dlg = h('div', { class: `dlg${wide ? ' wide' : ''}` },
      title ? h('div', { class: 'dlg-title' }, title) : null,
      h('div', { class: 'dlg-body' }, body ?? ''),
      h('div', { class: 'dlg-btns' }, buttons.map((b) => h('button', {
        class: b.primary ? 'primary' : b.danger ? 'danger' : '',
        onclick: () => close(typeof b.value === 'function' ? b.value() : b.value),
      }, b.label))))
    back.append(dlg)
    if (dismissable) back.addEventListener('mousedown', (e) => { if (e.target === back) close(undefined) })
    document.body.append(back)
    document.addEventListener('keydown', onKey, true)
    const first = dlg.querySelector('input, select, textarea') || dlg.querySelector('button.primary')
    first?.focus()
  })
}

export const confirmDlg = (text, { ok = 'Confirmer', danger = false } = {}) =>
  dialog({ title: 'Confirmation', body: text, buttons: [{ label: 'Annuler', value: false }, { label: ok, value: true, primary: !danger, danger }] })
    .then((v) => !!v)

// ── Sound (WebAudio, no assets) ───────────────────────────────────────────────

const SFX = {
  click: [[660, 0.04, 'square', 0.25]],
  move: [[420, 0.03, 'triangle', 0.3]],
  rotate: [[520, 0.035, 'triangle', 0.3]],
  drop: [[180, 0.08, 'square', 0.35]],
  pop: [[880, 0.05, 'sine', 0.35], [1320, 0.05, 'sine', 0.25]],
  merge: [[523, 0.05, 'triangle', 0.3], [784, 0.07, 'triangle', 0.3]],
  flag: [[740, 0.05, 'square', 0.2]],
  eat: [[600, 0.04, 'square', 0.25], [900, 0.05, 'square', 0.25]],
  hit: [[300, 0.04, 'square', 0.3]],
  bounce: [[240, 0.03, 'sine', 0.3]],
  line: [[523, 0.06, 'square', 0.25], [659, 0.06, 'square', 0.25], [784, 0.09, 'square', 0.25]],
  error: [[160, 0.12, 'sawtooth', 0.25]],
  tick: [[1200, 0.015, 'square', 0.12]],
  lose: [[392, 0.12, 'triangle', 0.35], [330, 0.12, 'triangle', 0.35], [262, 0.25, 'triangle', 0.35]],
  win: [[523, 0.09, 'triangle', 0.35], [659, 0.09, 'triangle', 0.35], [784, 0.09, 'triangle', 0.35], [1047, 0.25, 'triangle', 0.35]],
  boom: [[90, 0.3, 'sawtooth', 0.45]],
}
let audioCtx = null
export const sound = { enabled: true, volume: 0.5 }

export function sfx(name) {
  if (!sound.enabled || sound.volume <= 0) return
  const seq = SFX[name]
  if (!seq) return
  try {
    audioCtx ??= new AudioContext()
    if (audioCtx.state === 'suspended') audioCtx.resume()
    let t = audioCtx.currentTime
    for (const [freq, dur, type, gain] of seq) {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain()
      o.type = type
      o.frequency.setValueAtTime(freq, t)
      if (name === 'boom') o.frequency.exponentialRampToValueAtTime(30, t + dur)
      g.gain.setValueAtTime(gain * sound.volume, t)
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur)
      o.connect(g).connect(audioCtx.destination)
      o.start(t)
      o.stop(t + dur + 0.02)
      t += dur * 0.9
    }
  } catch { /* audio unavailable */ }
}

// ── Input: keyboard + gamepad unified into actions ───────────────────────────
// Actions: up down left right a b x y start select l r. Keyboard: arrows/WASD/ZQSD, Space/Enter = a,
// Backspace/X = b, P/Escape = start, Shift = select, Q/E? no (Q is left on AZERTY). C = x, V = y.

const KEYMAP = {
  ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
  KeyW: 'up', KeyS: 'down', KeyA: 'left', KeyD: 'right',
  Space: 'a', Enter: 'a', NumpadEnter: 'a', Backspace: 'b', KeyX: 'b', KeyC: 'x', KeyV: 'y',
  KeyP: 'start', Escape: 'start', ShiftLeft: 'select', ShiftRight: 'select',
}
const PAD_BUTTONS = { 0: 'a', 1: 'b', 2: 'x', 3: 'y', 4: 'l', 5: 'r', 8: 'select', 9: 'start', 12: 'up', 13: 'down', 14: 'left', 15: 'right' }
const REPEATABLE = new Set(['up', 'down', 'left', 'right'])

export function createInput() {
  const listeners = new Set()
  const held = new Set()
  let raf = 0
  const padState = new Map()

  const emit = (action, ev) => { for (const fn of [...listeners]) fn(action, ev) }
  const typing = (t) => t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))

  const onDown = (e) => {
    if (typing(e.target) || document.querySelector('.dlg-back') || e.ctrlKey || e.metaKey || e.altKey) return
    const action = KEYMAP[e.code]
    if (!action) return
    e.preventDefault()
    if (e.repeat && !REPEATABLE.has(action)) return
    held.add(action)
    emit(action, { source: 'key', repeat: e.repeat, code: e.code, key: e.key })
  }
  const onUp = (e) => {
    const action = KEYMAP[e.code]
    if (action) held.delete(action)
  }

  const pollPads = (now) => {
    raf = requestAnimationFrame(pollPads)
    if (!document.hasFocus() || document.querySelector('.dlg-back')) return
    const pads = navigator.getGamepads ? navigator.getGamepads() : []
    for (const pad of pads) {
      if (!pad) continue
      const st = padState.get(pad.index) || {}
      const pressed = {}
      for (const [i, act] of Object.entries(PAD_BUTTONS)) if (pad.buttons[i]?.pressed) pressed[act] = true
      const [ax, ay] = [pad.axes[0] || 0, pad.axes[1] || 0]
      if (ax < -0.5) pressed.left = true
      if (ax > 0.5) pressed.right = true
      if (ay < -0.5) pressed.up = true
      if (ay > 0.5) pressed.down = true
      for (const act of new Set([...Object.keys(pressed), ...Object.keys(st)])) {
        const was = st[act]
        if (pressed[act] && !was) {
          st[act] = { since: now, next: now + 220 }
          held.add(act)
          emit(act, { source: 'pad', repeat: false })
        } else if (pressed[act] && was && REPEATABLE.has(act) && now >= was.next) {
          was.next = now + 70
          emit(act, { source: 'pad', repeat: true })
        } else if (!pressed[act] && was) {
          delete st[act]
          held.delete(act)
        }
      }
      padState.set(pad.index, st)
    }
  }

  return {
    on(fn) {
      listeners.add(fn)
      if (listeners.size === 1) {
        document.addEventListener('keydown', onDown)
        document.addEventListener('keyup', onUp)
        raf = requestAnimationFrame(pollPads)
      }
      return () => this.off(fn)
    },
    off(fn) {
      listeners.delete(fn)
      if (!listeners.size) {
        document.removeEventListener('keydown', onDown)
        document.removeEventListener('keyup', onUp)
        cancelAnimationFrame(raf)
        held.clear()
      }
    },
    isHeld: (action) => held.has(action),
    destroy() { listeners.clear(); this.off(() => {}) },
  }
}

// ── Per-game context ─────────────────────────────────────────────────────────

export function createContext(game, { hubPrefs, toolbarEl, statusEl, rootEl }) {
  const cleanups = []
  const pauseFns = []
  const input = createInput()
  const id = game.id
  cleanups.push(() => input.destroy())

  const ctx = {
    game: id,
    h, format, shuffle, seeded, todayKey, toast, dialog, confirm: confirmDlg, sfx,
    call: (action, payload = {}) => call(action, { game: id, ...payload }),
    player: () => hubPrefs.player || '',

    scores: {
      submit: (variant, score, { lowerBetter = false, meta = {} } = {}) =>
        call('score.submit', { game: id, variant, score, lower_better: lowerBetter, meta, player: hubPrefs.player || '' }),
      list: (variant, { lowerBetter = false, limit = 10 } = {}) =>
        call('scores.list', { game: id, variant, lower_better: lowerBetter, limit }).then((r) => r.scores),
      clear: (variant) => call('scores.clear', { game: id, variant: variant ?? null }),
    },
    save: {
      get: () => call('save.get', { game: id }).then((r) => r.state),
      set: (state) => call('save.set', { game: id, state }).catch((e) => toast(`Sauvegarde : ${e.message}`, 'err')),
      clear: () => call('save.clear', { game: id }).catch(() => {}),
    },
    stats: {
      get: () => call('stats.get', { game: id }).then((r) => r.stats),
      update: (ops) => call('stats.update', { game: id, ...ops }).then((r) => r.stats).catch(() => ({})),
    },
    prefs: {
      get: () => call('prefs.get', { game: id }).then((r) => r.prefs),
      set: (data) => call('prefs.set', { game: id, data }).catch((e) => toast(`Préférences : ${e.message}`, 'err')),
    },

    // Header areas owned by the game
    setToolbar(...nodes) { toolbarEl.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false)) },
    setStatus(...nodes) { statusEl.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false)) },

    // Overlay inside the game area (pause, game over…). Returns close().
    overlay({ title, text, body, buttons = [] }) {
      rootEl.querySelector(':scope > .overlay')?.remove()
      const ov = h('div', { class: 'overlay' },
        h('div', { class: 'overlay-card' },
          title ? h('div', { class: 'overlay-title' }, title) : null,
          text ? h('div', { class: 'overlay-text' }, text) : null,
          body || null,
          buttons.length ? h('div', { class: 'overlay-btns' }, buttons.map((b) =>
            h('button', { class: b.primary ? 'primary' : '', onclick: () => { ov.remove(); b.onClick?.() } }, b.label))) : null))
      rootEl.append(ov)
      ov.querySelector('button.primary')?.focus()
      return () => ov.remove()
    },

    async showScores(variant, { lowerBetter = false, fmt = (s) => format.int(s), title = 'Meilleurs scores', highlightId = null, variants = null } = {}) {
      const list = h('div', { class: 'scores' })
      const render = async (v) => {
        list.replaceChildren(h('div', { class: 'muted' }, 'Chargement…'))
        const rows = await ctx.scores.list(v, { lowerBetter, limit: 10 }).catch(() => [])
        list.replaceChildren(rows.length
          ? h('table', null, h('tbody', null, rows.map((r, i) => h('tr', { class: r.id === highlightId ? 'me' : '' },
            h('td', { class: 'rk' }, i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i + 1}.`),
            h('td', { class: 'sc' }, fmt(r.score, r.meta)),
            h('td', { class: 'pl' }, r.player || '—'),
            h('td', { class: 'dt muted' }, format.date(r.created_at))))))
          : h('div', { class: 'muted' }, 'Aucun score pour l’instant.'))
      }
      let sel = null
      if (variants && variants.length > 1) {
        sel = h('select', { onchange: () => render(sel.value) }, variants.map(([v, label]) => h('option', { value: v, selected: v === variant }, label)))
      }
      render(variant)
      await dialog({ title, body: h('div', null, sel ? h('div', { style: { marginBottom: '8px' } }, sel) : null, list),
        buttons: [{ label: 'Fermer', value: true, primary: true }] })
    },

    // Unified keyboard/gamepad actions. fn(action, ev). Removed automatically on unmount.
    onAction(fn) { const off = input.on(fn); cleanups.push(off); return off },
    isHeld: (a) => input.isHeld(a),
    // Called when the window loses focus / tab hidden / user leaves the game.
    onPause(fn) { pauseFns.push(fn) },
    onCleanup(fn) { cleanups.push(fn) },

    // requestAnimationFrame loop with dt (ms), stopped on cleanup. Returns stop().
    loop(fn) {
      let raf = 0, last = performance.now(), alive = true
      const tick = (now) => {
        if (!alive) return
        const dt = Math.min(100, now - last)
        last = now
        fn(dt, now)
        if (alive) raf = requestAnimationFrame(tick)
      }
      raf = requestAnimationFrame(tick)
      const stop = () => { alive = false; cancelAnimationFrame(raf) }
      cleanups.push(stop)
      return stop
    },

    _pause() { for (const fn of pauseFns) try { fn() } catch (e) { console.error(e) } },
    _destroy() { for (const fn of cleanups.splice(0).reverse()) try { fn() } catch (e) { console.error(e) } },
  }
  return ctx
}
