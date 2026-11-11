import { _electron as electron } from 'playwright'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import assert from 'node:assert/strict'

const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'ledroide-e2e-'))
const app = await electron.launch({
  args: ['.'],
  env: { ...process.env, LEDROIDE_USER_DATA: userData, LEDROIDE_BACKEND_PORT: '8799' },
})
const errors = []
try {
  const win = await app.firstWindow()
  win.on('pageerror', (e) => errors.push(e.message))
  win.on('console', (m) => m.type() === 'error' && !m.text().startsWith('Failed to load resource') && errors.push(m.text()))

  await win.getByText('Catalogue').first().click()
  await win.locator('.cc').first().waitFor({ timeout: 30000 })
  const cards = await win.locator('.cc').count()
  assert.ok(cards > 0, 'catalog lists modules')

  const search = win.locator('.cp-filters .flt-search-input')

  await win.locator('.cp-filters .flt-chip', { hasText: 'Réseau' }).click()
  const networkCards = await win.locator('.cc .cc-category').allTextContents()
  assert.ok(networkCards.length > 0 && networkCards.every((c) => c === 'Réseau'), 'category filter')
  await win.locator('.cc-tag-chip', { hasText: '#dns' }).first().click()
  const dnsCount = await win.locator('.cc').count()
  assert.ok(dnsCount > 0 && dnsCount < networkCards.length, 'tag filter narrows results')
  await win.locator('.cp-filters .flt-clear').click()
  assert.equal(await win.locator('.cc').count(), cards, 'clearing filters restores all')

  for (const name of ['Module Demo', 'Calculatrice']) {
    await search.fill(name)
    await win.locator('.cc', { hasText: name }).getByRole('button', { name: 'Activer' }).click()
    await win.locator('.cc-modal').getByRole('button', { name: 'Activer' }).click()
    await win.locator('.cc', { hasText: name }).locator('.cc-badge-active').waitFor()
  }

  await win.getByText('Modules').first().click()
  const homeSearch = win.locator('.mg-toolbar .flt-search-input')
  const tiles = await win.locator('.mg-tile').count()
  assert.ok(tiles >= 2)
  await homeSearch.fill('calcul')
  assert.deepEqual(await win.locator('.mg-tile-name').allTextContents(), ['Calculatrice'], 'home search')
  await homeSearch.fill('')
  assert.equal(await win.locator('.mg-tile').count(), tiles)

  await win.getByText('Variables').first().click()
  await win.getByRole('button', { name: '+ Nouvelle variable' }).click()
  await win.locator('.env-key').last().fill('E2E_TEST_VAR')
  await win.locator('.env-row').last().locator('input[placeholder="valeur"]').fill('hello')
  await win.getByRole('button', { name: 'Enregistrer' }).click()
  await win.locator('.app-toast', { hasText: 'variable' }).waitFor()
  const env = await win.evaluate(() => window.ledroide.api('/system/env'))
  assert.deepEqual(env.vars.map((v) => [v.key, v.value]), [['E2E_TEST_VAR', 'hello']])

  await win.getByText('Modules').first().click()
  await win.locator('.mg-tile', { hasText: 'Module Demo' }).click()
  await win.locator('.app-tab', { hasText: 'Module Demo' }).waitFor()

  const frame = win.frameLocator('iframe.mf-iframe')
  await frame.locator('.btn-inc').click()
  await frame.locator('#count', { hasText: '1' }).waitFor()

  const forbidden = await win.evaluate(async () => {
    const r = await fetch(`${window.ledroide.backendOrigin}/modules`)
    return r.status
  })
  assert.equal(forbidden, 403, 'management API rejects calls without token')
  const authorized = await win.evaluate(async () => (await window.ledroide.api('/modules')).length)
  assert.ok(authorized > 0)

  assert.deepEqual(errors, [], `renderer errors: ${errors.join('\n')}`)
  console.log(`e2e smoke OK (${cards} modules)`)
} finally {
  await app.close()
  fs.rmSync(userData, { recursive: true, force: true })
}
