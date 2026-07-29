import { readFile, stat } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

export const DEFAULT_BUDGETS = Object.freeze({
  initialChunkBytes: 280 * 1024,
  routeChunkBytes: 64 * 1024,
})

function formatKiB(bytes) {
  return `${(bytes / 1024).toFixed(1)} KiB`
}

export function classifyChunks(manifest) {
  const entries = Object.entries(manifest)
  const initialFiles = new Set()
  const routeFiles = new Set()

  const visitInitial = (key) => {
    const chunk = manifest[key]
    if (!chunk || initialFiles.has(chunk.file)) return
    initialFiles.add(chunk.file)
    for (const importedKey of chunk.imports ?? []) visitInitial(importedKey)
  }

  for (const [key, chunk] of entries) {
    if (chunk.isEntry) visitInitial(key)
  }

  for (const [, chunk] of entries) {
    if (
      chunk.file.endsWith('.js') &&
      !initialFiles.has(chunk.file) &&
      !chunk.isEntry
    ) {
      routeFiles.add(chunk.file)
    }
  }

  return { initialFiles, routeFiles }
}

export function evaluateBundleBudget(manifest, fileSizes, budgets = DEFAULT_BUDGETS) {
  const { initialFiles, routeFiles } = classifyChunks(manifest)
  const measurements = [
    ...[...initialFiles]
      .filter((file) => file.endsWith('.js'))
      .map((file) => ({
        kind: 'initial',
        file,
        bytes: fileSizes[file],
        limit: budgets.initialChunkBytes,
      })),
    ...[...routeFiles].map((file) => ({
      kind: 'route',
      file,
      bytes: fileSizes[file],
      limit: budgets.routeChunkBytes,
    })),
  ]

  const missing = measurements.filter(({ bytes }) => !Number.isFinite(bytes))
  if (missing.length > 0) {
    throw new Error(`Missing size for bundle file(s): ${missing.map(({ file }) => file).join(', ')}`)
  }

  return measurements.map((measurement) => ({
    ...measurement,
    passed: measurement.bytes <= measurement.limit,
  }))
}

export function renderReport(results) {
  const lines = results
    .sort((left, right) => right.bytes - left.bytes)
    .map(({ kind, file, bytes, limit, passed }) => {
      const status = passed ? 'PASS' : 'FAIL'
      return `${status} ${kind.padEnd(7)} ${formatKiB(bytes).padStart(10)} / ${formatKiB(limit).padStart(10)}  ${file}`
    })

  return [`Bundle budget (uncompressed production JavaScript):`, ...lines].join('\n')
}

async function run() {
  const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const distRoot = path.join(projectRoot, 'dist')
  const manifest = JSON.parse(
    await readFile(path.join(distRoot, '.vite', 'manifest.json'), 'utf8'),
  )
  const { initialFiles, routeFiles } = classifyChunks(manifest)
  const files = [...new Set([...initialFiles, ...routeFiles])].filter((file) =>
    file.endsWith('.js'),
  )
  const sizes = Object.fromEntries(
    await Promise.all(
      files.map(async (file) => [file, (await stat(path.join(distRoot, file))).size]),
    ),
  )
  const results = evaluateBundleBudget(manifest, sizes)
  console.log(renderReport(results))

  if (results.some(({ passed }) => !passed)) process.exitCode = 1
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  run().catch((error) => {
    console.error(`Bundle budget check failed: ${error.message}`)
    process.exitCode = 1
  })
}
