import assert from 'node:assert/strict'
import test from 'node:test'
import {
  callbackDataLimit,
  MAX_CALLBACK_DATA_BYTES,
  MAX_FINAL_SUMMARY_CALLBACK_BYTES,
  MAX_FRAME_BYTES,
  MAX_RESULT_BYTES,
  ProtocolError,
  encodeFrame,
  parseCallbackResult,
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

test('parses closed control frames separately from task frames', () => {
  const frame = parseClientFrame(JSON.stringify({
    v: 1,
    kind: 'control',
    id: 'ctl-1',
    payload: {
      op: 'providers.list',
      args: { nested: ['value', { count: 1 }] },
    },
  }))
  assert.equal(frame.kind, 'control')
  if (frame.kind === 'control') {
    assert.equal(frame.payload.op, 'providers.list')
    assert.deepEqual(frame.payload.args, { nested: ['value', { count: 1 }] })
  }
  assert.throws(
    () => parseClientFrame(JSON.stringify({
      v: 1,
      kind: 'control',
      id: 'ctl-2',
      payload: { op: 'providers.list', args: {}, extra: true },
    })),
    (error: unknown) => error instanceof ProtocolError && error.code === 'invalid_control_payload',
  )
  assert.throws(
    () => parseClientFrame('{"v":1,"kind":"control","id":"ctl-3","payload":{"op":"providers.list","args":{"value":1e999}}}'),
    (error: unknown) => error instanceof ProtocolError && error.code === 'json_non_finite',
  )
})


function callbackResultLine(result: unknown): string {
  return JSON.stringify({
    v: 1,
    kind: 'callback_result',
    id: 'cb-test',
    payload: { ok: true, result },
  })
}

test('the named final-summary ceiling is kind and name scoped', () => {
  assert.equal(MAX_FINAL_SUMMARY_CALLBACK_BYTES, MAX_RESULT_BYTES + 1024)
  assert.equal(callbackDataLimit('summary.final_summary', 'summary'), MAX_FINAL_SUMMARY_CALLBACK_BYTES)
  assert.equal(callbackDataLimit('summary.final_summary', 'chat'), MAX_CALLBACK_DATA_BYTES)
  assert.equal(callbackDataLimit('summary.history_page', 'summary'), MAX_CALLBACK_DATA_BYTES)
  assert.equal(callbackDataLimit('chat.submit_final_response', 'chat'), MAX_CALLBACK_DATA_BYTES)
})

test('callback result parser default stays generic while the envelope admits the named maximum', () => {
  const result = { summary_text: 'a'.repeat(MAX_CALLBACK_DATA_BYTES + 1) }
  const payload = { ok: true, result }
  assert.throws(() => parseCallbackResult(payload), /callback_result_too_large/)
  const accepted = parseCallbackResult(payload, MAX_FINAL_SUMMARY_CALLBACK_BYTES)
  assert.equal(accepted.ok, true)

  const frame = parseClientFrame(callbackResultLine(result))
  assert.equal(frame.kind, 'callback_result')

  const tooLarge = { summary_text: 'a'.repeat(MAX_FINAL_SUMMARY_CALLBACK_BYTES + 1) }
  assert.throws(
    () => parseClientFrame(callbackResultLine(tooLarge)),
    /callback_result_too_large/,
  )
})
