import { accessSync, constants } from 'node:fs'
import { networkInterfaces } from 'node:os'

const requiredPaths = [
  ['.venv/bin/python', constants.X_OK],
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
