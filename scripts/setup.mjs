import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

function run(command, args) {
  console.log(`\n> ${command} ${args.join(' ')}`)
  const result = spawnSync(command, args, { stdio: 'inherit' })
  if (result.error) {
    console.error(result.error.message)
    process.exit(1)
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1)
  }
}

function output(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8' })
  return result.status === 0 ? result.stdout.trim() : null
}

if (process.platform !== 'darwin') {
  console.warn('Commute Help is designed and tested primarily for macOS.')
}

const nodeMajor = Number(process.versions.node.split('.')[0])
if (nodeMajor < 20) {
  console.error(`Node.js 20 or newer is required; found ${process.versions.node}.`)
  process.exit(1)
}

if (!existsSync('.venv/bin/python')) {
  const candidates = ['python3.12', 'python3']
  const systemPython = candidates.find(
    (candidate) =>
      output(candidate, [
        '-c',
        'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
      ]) === '3.12',
  ) ?? null

  if (!systemPython) {
    console.error('Python 3.12 is required but was not found.')
    process.exit(1)
  }
  run(systemPython, ['-m', 'venv', '.venv'])
}

const pythonVersion = output('.venv/bin/python', [
  '-c',
  'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
])
if (pythonVersion !== '3.12') {
  console.error(`The .venv must use Python 3.12; found ${pythonVersion ?? 'unknown'}.`)
  process.exit(1)
}

run('npm', ['install'])
run('npm', ['--prefix', 'frontend', 'install'])
run('.venv/bin/python', ['-m', 'pip', 'install', '-r', 'backend/requirements.txt'])
run('.venv/bin/python', ['-m', 'scripts.sumo_doctor'])

console.log('\nSetup complete. Start Commute Help with `npm run dev`.')
console.log('Run `npm run doctor` to verify local data and dependencies.')
console.log('If the road graph is missing, run `npm run graph:build` once.')
