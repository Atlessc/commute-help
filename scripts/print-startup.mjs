import { accessSync, constants } from 'node:fs'
import { networkInterfaces } from 'node:os'
import { spawnSync } from 'node:child_process'

const requiredPaths = [
  ['.venv/bin/python', constants.X_OK],
  ['node_modules/.bin/concurrently', constants.X_OK],
  ['frontend/node_modules', constants.R_OK],
]

for (const [path, mode] of requiredPaths) {
  try {
    accessSync(path, mode)
  } catch {
    console.error(`Missing ${path}. Run \`npm run setup\` first.`)
    process.exit(1)
  }
}

const pythonCheck = spawnSync(
  '.venv/bin/python',
  ['-c', 'import fastapi, networkx, osmnx, uvicorn'],
  { encoding: 'utf8' },
)
if (pythonCheck.status !== 0) {
  console.error('Backend Python dependencies are incomplete. Run `npm run setup` first.')
  process.exit(1)
}

const graphCheck = spawnSync(
  '.venv/bin/python',
  [
    '-c',
    'from backend.app.core.settings import get_settings; s = get_settings(); raise SystemExit(0 if s.graph_path.exists() and s.graph_manifest_path.exists() else 1)',
  ],
)
if (graphCheck.status !== 0) {
  console.warn('\nRoad graph unavailable. The app will open in recovery mode.')
  console.warn('Run `npm run graph:build` once, then restart Commute Help.\n')
}

const lanAddresses = Object.values(networkInterfaces())
  .flat()
  .filter(
    (address) =>
      address &&
      address.family === 'IPv4' &&
      !address.internal &&
      !address.address.startsWith('169.254.'),
  )
  .map((address) => address.address)

console.log('\nCommute Help is starting…')
console.log('  Local: http://localhost:5173')
for (const address of lanAddresses) {
  console.log(`  LAN:   http://${address}:5173`)
}
console.log('  API requests are proxied through /api.')
console.log('  Press Control-C once to stop both services.\n')
