import { readdirSync, readFileSync } from 'node:fs'
import { dirname, extname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const sourceRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const uiRoot = join(sourceRoot, 'ui')
const transportInfrastructureImports: Readonly<Record<string, readonly string[]>> = {
  'ui/session/SessionContext.tsx': ['onApiUnauthorized'],
}

function productionSources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return productionSources(path)
    if (!['.ts', '.tsx'].includes(extname(entry.name)) || /\.test\.tsx?$/.test(entry.name)) return []
    return [path]
  })
}

function apiBoundaryViolations(path: string) {
  const source = ts.createSourceFile(
    path,
    readFileSync(path, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const violations: string[] = []
  const sourcePath = relative(sourceRoot, path)
  const report = (node: ts.Node, reason: string) => {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart(source))
    violations.push(`${sourcePath}:${line + 1} ${reason}`)
  }
  const isApiModule = (value: string) => /(^|\/)lib\/api(?:\.[cm]?[jt]s)?$/.test(value)

  const visit = (node: ts.Node) => {
    if (
      ts.isImportDeclaration(node)
      && ts.isStringLiteral(node.moduleSpecifier)
      && isApiModule(node.moduleSpecifier.text)
    ) {
      const namedImports = node.importClause?.namedBindings
      const importedNames = namedImports && ts.isNamedImports(namedImports)
        ? namedImports.elements.map((element) => element.propertyName?.text ?? element.name.text)
        : []
      const allowedNames = transportInfrastructureImports[sourcePath] ?? []
      if (
        !node.importClause
        || node.importClause.name
        || !namedImports
        || ts.isNamespaceImport(namedImports)
        || importedNames.some((name) => !allowedNames.includes(name))
      ) {
        report(node, 'imports lib/api directly')
      }
    }

    if (ts.isCallExpression(node)) {
      if (ts.isIdentifier(node.expression) && node.expression.text === 'fetch') {
        report(node, 'calls fetch() directly')
      } else if (
        ts.isPropertyAccessExpression(node.expression)
        && node.expression.name.text === 'fetch'
        && ts.isIdentifier(node.expression.expression)
        && ['window', 'globalThis'].includes(node.expression.expression.text)
      ) {
        report(node, `calls ${node.expression.expression.text}.fetch() directly`)
      } else if (
        node.expression.kind === ts.SyntaxKind.ImportKeyword
        && node.arguments.length === 1
        && ts.isStringLiteral(node.arguments[0])
        && isApiModule(node.arguments[0].text)
      ) {
        report(node, 'imports lib/api dynamically')
      }
    }

    ts.forEachChild(node, visit)
  }
  visit(source)
  return violations
}

describe('UI service boundary', () => {
  it('keeps HTTP transport behind src/services', () => {
    const violations = productionSources(uiRoot).flatMap(apiBoundaryViolations)

    expect(
      violations,
      [
        'UI modules must call typed functions from src/services.',
        'Move direct lib/api imports and raw fetch calls behind that boundary.',
      ].join(' '),
    ).toEqual([])
  })
})
