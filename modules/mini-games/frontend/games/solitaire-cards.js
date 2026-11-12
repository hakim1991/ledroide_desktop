import { suitOf, rankOf, isRed, RANKS } from './solitaire-core.js'

export const RED = '#c22f3c'
export const BLACK = '#1c2230'
const GOLD = '#d9a93a', GOLD_D = '#8f6a1c', SKIN = '#f3d8bb', HAIR = '#5b3d24'

const SUIT_PATHS = [
  '<path d="M50 4C34 24 6 38 6 60c0 14 11 23 24 23 8 0 14-4 18-10-1 10-5 17-12 23h28c-7-6-11-13-12-23 4 6 10 10 18 10 13 0 24-9 24-23C94 38 66 24 50 4z"/>',
  '<path d="M50 92C22 70 4 52 4 31 4 16 15 6 29 6c10 0 17 6 21 14 4-8 11-14 21-14 14 0 25 10 25 25 0 21-18 39-46 61z"/>',
  '<circle cx="50" cy="27" r="19"/><circle cx="27" cy="59" r="19"/><circle cx="73" cy="59" r="19"/><path d="M44 50h12c0 18 4 30 12 44H32c8-14 12-26 12-44z"/>',
  '<path d="M50 3L88 50 50 97 12 50z"/>',
]

export const SYMBOL_DEFS = `<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>${
  SUIT_PATHS.map((p, i) => `<symbol id="sol-s${i}" viewBox="0 0 100 100">${p}</symbol>`).join('')}<clipPath id="sol-clip"><rect x="16" y="31" width="68" height="78" rx="5"/></clipPath></defs></svg>`

const use = (suit, x, y, size, rot = false) =>
  `<use href="#sol-s${suit}" x="${x - size / 2}" y="${y - size / 2}" width="${size}" height="${size}"${rot ? ` transform="rotate(180 ${x} ${y})"` : ''}/>`

const PIPS = {
  2: [[50, 0], [50, 1]],
  3: [[50, 0], [50, 0.5], [50, 1]],
  4: [[29, 0], [71, 0], [29, 1], [71, 1]],
  5: [[29, 0], [71, 0], [50, 0.5], [29, 1], [71, 1]],
  6: [[29, 0], [71, 0], [29, 0.5], [71, 0.5], [29, 1], [71, 1]],
  7: [[29, 0], [71, 0], [50, 0.25], [29, 0.5], [71, 0.5], [29, 1], [71, 1]],
  8: [[29, 0], [71, 0], [50, 0.25], [29, 0.5], [71, 0.5], [50, 0.75], [29, 1], [71, 1]],
  9: [[29, 0], [71, 0], [29, 1 / 3], [71, 1 / 3], [50, 0.5], [29, 2 / 3], [71, 2 / 3], [29, 1], [71, 1]],
  10: [[29, 0], [71, 0], [50, 1 / 6], [29, 1 / 3], [71, 1 / 3], [29, 2 / 3], [71, 2 / 3], [50, 5 / 6], [29, 1], [71, 1]],
}

function figure(rank, suit, col) {
  const tint = isRed(suit * 13) ? '#fbe8e6' : '#e6ebf5'
  const robe = col
  let hat = '', extra = ''
  if (rank === 13) {
    hat = `<path d="M37 55V41l6.5 7L50 37l6.5 11 6.5-7v14z" fill="${GOLD}" stroke="${GOLD_D}" stroke-width="1.2" stroke-linejoin="round"/>
      <circle cx="50" cy="44" r="2" fill="${col}"/>`
    extra = `<path d="M42 68q8 13 16 0" fill="#7a5a3c"/><path d="M68 76l6-30" stroke="${GOLD_D}" stroke-width="2.4" stroke-linecap="round"/>
      <circle cx="74" cy="44" r="3.4" fill="${GOLD}" stroke="${GOLD_D}"/>`
  } else if (rank === 12) {
    hat = `<path d="M37 72q-4-24 13-24t13 24z" fill="${HAIR}"/>`
    extra = `<path d="M41 53q9-9 18 0" fill="none" stroke="${GOLD}" stroke-width="3.2" stroke-linecap="round"/><circle cx="50" cy="48.5" r="2.4" fill="${col}"/>
      <path d="M66 98q-2-10 0-16" stroke="#3f7a43" stroke-width="2"/>
      <g fill="${GOLD}" stroke="${GOLD_D}" stroke-width=".8"><circle cx="66" cy="78" r="3"/><circle cx="70" cy="81" r="3"/><circle cx="62" cy="81" r="3"/><circle cx="66" cy="84" r="3"/></g>
      <circle cx="66" cy="81" r="2" fill="${col}"/>`
  } else {
    hat = `<path d="M38.5 58q2-13 15-12l9 5.5-.5 6.5z" fill="${col}" stroke="${BLACK}" stroke-width=".8"/>`
    extra = `<path d="M58 50q12-15 16-9-6 4-13 12" fill="${GOLD}" stroke="${GOLD_D}" stroke-width=".8"/>
      <path d="M31 104l8-28" stroke="${GOLD_D}" stroke-width="2.4" stroke-linecap="round"/>`
  }
  return `<rect x="16" y="31" width="68" height="78" rx="5" fill="${tint}" stroke="${col}" stroke-width="1.4"/>
    <g clip-path="url(#sol-clip)">
      <path d="M28 112l5-30q17-12 34 0l5 30z" fill="${robe}"/>
      <path d="M37 84l13 15 13-15" fill="none" stroke="${GOLD}" stroke-width="3" stroke-linejoin="round"/>
      <path d="M50 99v12" stroke="${GOLD}" stroke-width="3"/>
      ${rank === 12 ? hat : ''}
      <circle cx="50" cy="64" r="10.5" fill="${SKIN}" stroke="#a07850" stroke-width=".8"/>
      <circle cx="46" cy="63" r="1.2" fill="${BLACK}"/><circle cx="54" cy="63" r="1.2" fill="${BLACK}"/>
      <path d="M47 68.5q3 2 6 0" fill="none" stroke="#a0524a" stroke-width="1"/>
      ${rank !== 12 ? hat : ''}${extra}
    </g>
    ${use(suit, 24, 39, 11)}${use(suit, 76, 101, 11, true)}`
}

export function faceSVG(c) {
  const suit = suitOf(c), rank = rankOf(c), col = isRed(c) ? RED : BLACK
  const label = RANKS[rank]
  const corner = `<text x="${label.length > 1 ? 4 : 7}" y="24" font-size="${label.length > 1 ? 23 : 25}" font-weight="700" letter-spacing="${label.length > 1 ? -2 : 0}"
      font-family="'Segoe UI', system-ui, sans-serif">${label}</text>${use(suit, 83, 14, 19)}`
  let center
  if (rank === 1) center = use(suit, 50, 70, suit === 3 ? 50 : 46)
  else if (rank <= 10) center = PIPS[rank].map(([x, ry]) => use(suit, x, 41 + ry * 58, 18, ry > 0.5)).join('')
  else center = figure(rank, suit, col)
  return `<svg viewBox="0 0 100 140" fill="${col}" xmlns="http://www.w3.org/2000/svg">
    ${corner}<g transform="rotate(180 50 70)">${corner}</g>${center}</svg>`
}
