import assert from 'node:assert/strict'
import test from 'node:test'
import {
  MAX_FRAME_BYTES,
  ProtocolError,
  encodeFrame,
  parseClientFrame,
  parseTaskRequest,
} from '../dist/protocol.js'

function requestLine(): string {
  return JSON.stringify({
    v: 1,
    kind: 'request',
    id: 'req-1',
    payload: {
      task_id: 'task-1',
      kind: 'chat',
      task_data: { message: 'hello' },
      budget: { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 },
    },
  })
}

test('parses the closed request envelope', () => {
  const frame = parseClientFrame(requestLine())
  assert.equal(frame.kind, 'request')
  assert.equal(frame.id, 'req-1')
  if (frame.kind === 'request') {
    assert.equal(frame.payload.kind, 'chat')
    assert.deepEqual(frame.payload.task_data, { message: 'hello' })
  }
})

test('unknown kinds and envelope fields fail closed', () => {
  assert.throws(
    () => parseClientFrame('{"v":1,"kind":"management","id":"x","payload":{}}'),
    (error: unknown) => error instanceof ProtocolError && error.code === 'unknown_frame_kind',
  )
  assert.throws(
    () => parseClientFrame('{"v":1,"kind":"cancel","id":"x","payload":{"reason":"x"},"extra":1}'),
    (error: unknown) => error instanceof ProtocolError && error.code === 'invalid_envelope',
  )
})

test('request payloads reject model messages, tool schemas, and credentials', () => {
  const payload = {
    task_id: 'task-1',
    kind: 'chat',
    task_data: { message: 'x' },
    budget: { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 },
    messages: [],
  }
  assert.throws(
    () => parseTaskRequest(payload),
    (error: unknown) => error instanceof ProtocolError && error.code === 'invalid_request_payload',
  )
})

test('oversized and non-finite payloads fail closed', () => {
  assert.throws(
    () => encodeFrame({
      v: 1,
      kind: 'result',
      id: 'x',
      payload: { ok: true, result: { value: 'x'.repeat(MAX_FRAME_BYTES) } },
    }),
    (error: unknown) => error instanceof ProtocolError && error.code === 'frame_too_large',
  )
  assert.throws(
    () => parseTaskRequest({
      task_id: 'task-1',
      kind: 'chat',
      task_data: { value: Number.NaN },
      budget: { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 },
    }),
    (error: unknown) => error instanceof ProtocolError && error.code === 'json_non_finite',
  )
})

test('unknown callback names and mismatched IDs are not resolvable', () => {
  assert.throws(
    () => parseClientFrame(JSON.stringify({
      v: 1,
      kind: 'callback_result',
      id: 'cb-1',
      payload: { ok: false, error: { code: 'Not Safe', message: 'x' } },
    })),
    (error: unknown) => error instanceof ProtocolError && error.code === 'invalid_error_code',
  )
})
