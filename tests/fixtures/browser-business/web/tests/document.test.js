import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'

test('The fixture has a form and a module entry point', () => {
  const document = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  assert.match(document, /<form/)
  assert.match(document, /type="module"/)
})
