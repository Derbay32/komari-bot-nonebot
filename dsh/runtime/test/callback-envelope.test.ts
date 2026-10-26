import assert from 'node:assert/strict'
import test from 'node:test'
import { CallbackDispatcher } from '../dist/host.js'
import {
  MAX_CALLBACK_DATA_BYTES,
  MAX_FINAL_SUMMARY_CALLBACK_BYTES,
} from '../dist/protocol.js'
import type { ServerFrame } from '../dist/protocol.js'

function captureDispatcher(): {
  readonly dispatcher: CallbackDispatcher
  readonly frames: ServerFrame[]
} {
  const frames: ServerFrame[] = []
  const dispatcher = new CallbackDispatcher(async (frame) => {
    frames.push(frame)
  })
  return { dispatcher, frames }
}

function callbackId(frames: readonly ServerFrame[]): string {
  const frame = frames[0]
  assert.ok(frame !== undefined)
  if (frame.kind !== 'callback') throw new Error('expected callback frame')
  return frame.id
}

test('generic callback replies above 256 KiB are rejected before the tool observes them', async () => {
  const { dispatcher, frames } = captureDispatcher()
  const controller = new AbortController()
  const pending = dispatcher.requestCallback(
    {
      request_id: 'req-generic',
      task_id: 'task-generic',
      name: 'chat.submit_final_response',
      data: {},
    },
    controller.signal,
  )
  const id = callbackId(frames)
  const accepted = dispatcher.handleResult(id, {
    ok: true,
    result: { content: 'a'.repeat(MAX_CALLBACK_DATA_BYTES + 1) },
  })

  assert.equal(accepted, true)
  await assert.rejects(pending, /callback_failed/)
})

test('the recorded named final-summary ceiling admits a 300-KiB reply', async () => {
  const { dispatcher, frames } = captureDispatcher()
  const controller = new AbortController()
  const content = 'a'.repeat(300 * 1024)
  const pending = dispatcher.requestCallback(
    {
      request_id: 'req-summary',
      task_id: 'task-summary',
      name: 'summary.final_summary',
      data: { content },
    },
    controller.signal,
    { maxResultBytes: MAX_FINAL_SUMMARY_CALLBACK_BYTES },
  )
  const id = callbackId(frames)
  const accepted = dispatcher.handleResult(id, {
    ok: true,
    result: { accepted: true, code: null, missing: [], result: { summary_text: content } },
  })

  assert.equal(accepted, true)
  const resolved = await pending
  assert.deepEqual(resolved, {
    accepted: true,
    code: null,
    missing: [],
    result: { summary_text: content },
  })
})

test('an uncorrelated result never acquires the named allowance', () => {
  const { dispatcher } = captureDispatcher()
  assert.equal(
    dispatcher.handleResult('cb-unknown', {
      ok: true,
      result: { summary_text: 'a'.repeat(MAX_CALLBACK_DATA_BYTES + 1) },
    }),
    false,
  )
})
