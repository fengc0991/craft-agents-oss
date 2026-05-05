import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const editPopoverSource = readFileSync(
  join(import.meta.dir, '..', 'EditPopover.tsx'),
  'utf8',
)

function expectInlineExecutionFor(key: string) {
  expect(editPopoverSource).toMatch(
    new RegExp(`'${key}':\\s*\\(location\\)\\s*=>\\s*\\(\\{[\\s\\S]*?inlineExecution:\\s*true,`)
  )
}

describe('EditPopover add-source variants', () => {
  it('keep source creation in inline execution mode for WebUI', () => {
    expectInlineExecutionFor('add-source')
    expectInlineExecutionFor('add-source-api')
    expectInlineExecutionFor('add-source-mcp')
    expectInlineExecutionFor('add-source-local')
  })

  it('keeps add-skill aligned with the same inline flow', () => {
    expectInlineExecutionFor('add-skill')
  })
})
