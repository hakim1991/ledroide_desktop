#!/usr/bin/env node
'use strict'
/**
 * Downloads Python 3.12 embeddable (Windows x64) and sets up
 * the bundled Python environment in backend/python-dist/.
 *
 * Run once before building:  node scripts/setup-python.js
 * Re-run after adding deps:  node scripts/setup-python.js --force
 */

const https        = require('https')
const fs           = require('fs')
const path         = require('path')
const { spawnSync } = require('child_process')

const PYTHON_VERSION = '3.12.9'
const DIST_DIR       = path.resolve(__dirname, '../backend/python-dist')
const EMBED_ZIP      = path.join(DIST_DIR, '_embed.zip')
const PYTHON_EXE     = path.join(DIST_DIR, 'python.exe')
const GET_PIP_PATH   = path.join(DIST_DIR, '_get-pip.py')
const SDK_DIR        = path.resolve(__dirname, '../backend/sdk')

const EMBED_URL   = `https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-amd64.zip`
const GET_PIP_URL = 'https://bootstrap.pypa.io/get-pip.py'

const BASE_DEPS = [
  'setuptools',
  'wheel',
  'fastapi',
  'uvicorn[standard]',
  'pydantic',
  'python-multipart',
  'Pillow',
  'numpy',
]

// ── Helpers ───────────────────────────────────────────────────────────────────

function log(msg)  { console.log(`\x1b[36m[setup-python]\x1b[0m ${msg}`) }
function ok(msg)   { console.log(`\x1b[32m[setup-python]\x1b[0m ✓ ${msg}`) }
function fail(msg) { console.error(`\x1b[31m[setup-python]\x1b[0m ✗ ${msg}`); process.exit(1) }

function download(url, dest) {
  return new Promise((resolve, reject) => {
    log(`Downloading ${path.basename(dest)} …`)

    function get(u) {
      const mod = u.startsWith('https') ? https : require('http')
      mod.get(u, (res) => {
        if (res.statusCode === 301 || res.statusCode === 302) {
          return get(res.headers.location)
        }
        if (res.statusCode !== 200) {
          return reject(new Error(`HTTP ${res.statusCode} for ${u}`))
        }
        const total = parseInt(res.headers['content-length'] || '0', 10)
        let received = 0
        const file = fs.createWriteStream(dest)
        res.on('data', chunk => {
          received += chunk.length
          if (total) {
            const pct = Math.round(received / total * 100)
            process.stdout.write(`\r  ${pct}%  (${Math.round(received / 1e6)} MB)`)
          }
        })
        res.pipe(file)
        file.on('finish', () => { process.stdout.write('\n'); file.close(resolve) })
        file.on('error', reject)
      }).on('error', reject)
    }

    get(url)
  })
}

function ps(command) {
  const r = spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', command], {
    stdio: 'inherit',
    encoding: 'utf8',
  })
  if (r.status !== 0) fail(`PowerShell command failed:\n  ${command}`)
}

function pip(...args) {
  const r = spawnSync(PYTHON_EXE, ['-m', 'pip', ...args, '--no-warn-script-location'], {
    stdio: 'inherit',
    encoding: 'utf8',
  })
  if (r.status !== 0) fail(`pip ${args.join(' ')} failed`)
}

function installSdk() {
  const sitePackages = path.join(DIST_DIR, 'Lib', 'site-packages')
  const sdkSrc = path.join(SDK_DIR, 'ledroide_sdk')
  const sdkDst = path.join(sitePackages, 'ledroide_sdk')
  if (fs.existsSync(sdkDst)) {
    fs.rmSync(sdkDst, { recursive: true, force: true })
  }
  fs.cpSync(sdkSrc, sdkDst, { recursive: true })
  ok('ledroide_sdk copied to site-packages.')
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  const force = process.argv.includes('--force')

  if (fs.existsSync(PYTHON_EXE) && !force) {
    ok('Python dist already exists — skipping download.')
    log('Run with --force to re-download.')

    log('Updating ledroide_sdk …')
    installSdk()
    return
  }

  if (force && fs.existsSync(DIST_DIR)) {
    log('--force: removing existing dist …')
    ps(`Remove-Item -Recurse -Force '${DIST_DIR}'`)
  }

  log(`Setting up Python ${PYTHON_VERSION} embeddable in backend/python-dist/`)
  fs.mkdirSync(DIST_DIR, { recursive: true })

  // 1 — Download Python embeddable zip
  await download(EMBED_URL, EMBED_ZIP)

  // 2 — Extract
  log('Extracting …')
  ps(`Expand-Archive -Path '${EMBED_ZIP}' -DestinationPath '${DIST_DIR}' -Force`)
  fs.unlinkSync(EMBED_ZIP)
  ok('Python extracted.')

  // 3 — Enable site-packages (uncomment "import site" in the ._pth file)
  const pthFiles = fs.readdirSync(DIST_DIR).filter(f => /python\d+\._pth$/.test(f))
  if (!pthFiles.length) fail('Could not find ._pth file in Python embeddable dist')

  const pthPath    = path.join(DIST_DIR, pthFiles[0])
  let   pthContent = fs.readFileSync(pthPath, 'utf8')

  if (pthContent.includes('#import site')) {
    pthContent = pthContent.replace('#import site', 'import site')
    fs.writeFileSync(pthPath, pthContent)
    ok(`site-packages enabled (${pthFiles[0]})`)
  } else if (!pthContent.includes('import site')) {
    // Append if missing
    pthContent += '\nimport site\n'
    fs.writeFileSync(pthPath, pthContent)
    ok(`site-packages added (${pthFiles[0]})`)
  } else {
    ok('site-packages already enabled.')
  }

  // 4 — Install pip
  log('Installing pip …')
  await download(GET_PIP_URL, GET_PIP_PATH)
  const pipResult = spawnSync(PYTHON_EXE, [GET_PIP_PATH], { stdio: 'inherit' })
  if (pipResult.status !== 0) fail('get-pip.py failed')
  fs.unlinkSync(GET_PIP_PATH)
  ok('pip installed.')

  // 5 — Upgrade pip
  pip('install', '--upgrade', 'pip')

  // 6 — Install base dependencies
  log('Installing base dependencies …')
  pip('install', ...BASE_DEPS)
  ok('Base dependencies installed.')

  // 7 — Install ledroide_sdk (direct copy, no build step needed)
  log('Installing ledroide_sdk …')
  installSdk()

  ok(`\nPython environment ready → backend/python-dist/`)
  log('You can now run: npm run dist')
}

main().catch(e => fail(e.message))
