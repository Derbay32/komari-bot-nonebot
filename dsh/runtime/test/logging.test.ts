import assert from 'node:assert/strict'
import test from 'node:test'
import { sanitizeLogText } from '../dist/logging.js'

test('stderr sanitization removes control bytes and credential-shaped values', () => {
  assert.equal(sanitizeLogText('key sk-abcdefghijklmnop secret'), 'key [REDACTED] secret')
  assert.equal(sanitizeLogText('Authorization: Bearer abc123'), 'Authorization: Bearer [REDACTED]')
  assert.equal(sanitizeLogText('https://user:pass@example.test/v1'), 'https://[REDACTED]@example.test/v1')
  assert.equal(sanitizeLogText('a\u0000b'), 'ab')
})
