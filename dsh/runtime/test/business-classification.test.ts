import assert from 'node:assert/strict'
import test from 'node:test'
import {
  classifyTerminalImageFailure,
  requestContainsNativeImages,
} from '../dist/business.js'

type Messages = Parameters<typeof requestContainsNativeImages>[0]
type Modalities = Parameters<typeof requestContainsNativeImages>[1]

function messages(content: readonly Record<string, unknown>[]): Messages {
  return [{ content }] as unknown as Messages
}

function modalities(value: readonly string[] | undefined): Modalities {
  return value as unknown as Modalities
}

test('native image request detection follows the official text-only projection rule', () => {
  const image = messages([{ type: 'image', offloaded: false }])
  assert.equal(requestContainsNativeImages(image, modalities(undefined)), true)
  assert.equal(requestContainsNativeImages(image, modalities(['text', 'image'])), true)
  assert.equal(requestContainsNativeImages(image, modalities(['text'])), false)
  assert.equal(
    requestContainsNativeImages(messages([{ type: 'image', offloaded: true }]), modalities(['text', 'image'])),
    false,
  )
  assert.equal(requestContainsNativeImages(messages([{ type: 'text', text: 'no image' }]), modalities(undefined)), false)
})

test('only positive terminal provider failures on an image-bearing frozen route classify', () => {
  for (const code of [
    'AUTH',
    'QUOTA',
    'ACCOUNT_QUOTA',
    'RATE_LIMIT',
    'SERVER',
    'TIMEOUT',
    'TRANSPORT',
    'PI_AI_ERROR',
    'EMPTY_RESPONSE',
    'STREAM_CLOSED',
    'INVALID_REQUEST',
  ]) {
    assert.equal(classifyTerminalImageFailure(code, true, true), 'image_model_failed', code)
  }
  for (const code of [
    'INVALID_REPLAY_STATE',
    'INVALID_PREPARED_CALL',
    'INVALID_MODEL_INFO',
    'UNSUPPORTED_CONTENT',
    'UNSUPPORTED_OPTION',
    'IMAGE_OFFLOAD_REQUIRED',
    'CONTEXT_WINDOW_EXCEEDED',
    'ADMISSION_DENIED',
    'ABORTED',
    'MISSING_CREDENTIAL',
    'UNKNOWN',
  ]) {
    assert.equal(classifyTerminalImageFailure(code, true, true), undefined, code)
  }
  assert.equal(classifyTerminalImageFailure('INVALID_REQUEST', true, false), undefined)
  assert.equal(classifyTerminalImageFailure('INVALID_REQUEST', false, true), undefined)
  assert.equal(classifyTerminalImageFailure('IMAGE_INPUT_FAILED', true, true), 'image_input_failed')
})
