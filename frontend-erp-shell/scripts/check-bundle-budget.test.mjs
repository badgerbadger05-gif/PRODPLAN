import assert from 'node:assert/strict'
import test from 'node:test'

import {
  classifyChunks,
  evaluateBundleBudget,
  renderReport,
} from './check-bundle-budget.mjs'

const manifest = {
  'src/main.tsx': {
    file: 'assets/index.js',
    isEntry: true,
    imports: ['_shared.js'],
    dynamicImports: ['src/pages/Ledger.tsx'],
  },
  '_shared.js': {
    file: 'assets/shared.js',
  },
  'src/pages/Ledger.tsx': {
    file: 'assets/ledger.js',
    isDynamicEntry: true,
    imports: ['_shared.js'],
  },
}

test('separates the initial import graph from lazy route chunks', () => {
  const chunks = classifyChunks(manifest)

  assert.deepEqual([...chunks.initialFiles].sort(), [
    'assets/index.js',
    'assets/shared.js',
  ])
  assert.deepEqual([...chunks.routeFiles], ['assets/ledger.js'])
})

test('reports a route chunk that exceeds its budget', () => {
  const results = evaluateBundleBudget(
    manifest,
    {
      'assets/index.js': 100,
      'assets/shared.js': 20,
      'assets/ledger.js': 70,
    },
    { initialChunkBytes: 110, routeChunkBytes: 64 },
  )

  assert.equal(results.find(({ file }) => file === 'assets/index.js').passed, true)
  assert.equal(results.find(({ file }) => file === 'assets/ledger.js').passed, false)
  assert.match(renderReport(results), /FAIL route/)
})

test('fails clearly when a manifest asset is missing from dist', () => {
  assert.throws(
    () =>
      evaluateBundleBudget(
        manifest,
        { 'assets/index.js': 100, 'assets/shared.js': 20 },
        { initialChunkBytes: 110, routeChunkBytes: 64 },
      ),
    /assets\/ledger\.js/,
  )
})
