import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { LlmError } from '@deepseek-ai/dsh-llm'
import { startHost } from '../dist/host.js'
import type { TaskAttachmentStore } from '../dist/attachments.js'
import type { JsonValue } from '../dist/protocol.js'
import { minimalChatTaskData, startModelEndpoint, writeTestProfile } from './support.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)
const BUDGET = { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 }
process.env.FIXTURE_KEY = 'fixture-test-key'

function descriptor(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    index: 0,
    origin: 'current',
    media_type: 'image/png',
    byte_count: PNG_1X1.byteLength,
    sha256: createHash('sha256').update(PNG_1X1).digest('hex'),
    ...overrides,
  }
}

function dataWithImages(images: readonly Record<string, unknown>[]): { [key: string]: JsonValue } {
  return { ...minimalChatTaskData({ text: 'image input' }), images } as unknown as { [key: string]: JsonValue }
}

async function runInvalidImage(
  images: readonly Record<string, unknown>[],
  chunkBody: (index: number, offset: number) => JsonValue,
): Promise<{ readonly error: string; readonly requests: number }> {
  const endpoint = await startModelEndpoint([])
  const root = mkdtempSync(join(tmpdir(), 'komari-image-input-'))
  const profile = writeTestProfile(root, endpoint.baseURL)
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(root, 'home'),
      attachmentRoot: join(root, 'attachments'),
      sendFrame: async (frame) => {
        if (frame.kind !== 'callback') return
        const payload = frame.payload as { name: string; data: { image_index?: number; offset?: number } }
        if (payload.name === 'admission') {
          runtime?.callbacks.handleResult(frame.id, { ok: true, result: { allow: true } })
          return
        }
        if (payload.name === 'chat.read_image_chunk') {
          runtime?.callbacks.handleResult(frame.id, {
            ok: true,
            result: chunkBody(payload.data.image_index ?? -1, payload.data.offset ?? -1),
          })
          return
        }
        runtime?.callbacks.handleResult(frame.id, { ok: true, result: { accepted: false, code: 'invalid_content', missing: ['final_response', 'record_favorability_delta'], result: null } })
      },
    })
    try {
      await runtime.service.runTask('image-invalid-request', {
        task_id: 'image-invalid-task',
        kind: 'chat',
        task_data: dataWithImages(images),
        budget: BUDGET,
      })
      assert.fail('expected invalid image input')
    } catch (error) {
      assert.equal((error as { message?: string }).message, 'invalid_image_input')
    }
    return { error: 'invalid_image_input', requests: endpoint.requests.length }
  } finally {
    await runtime?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
}

test('malformed image descriptors fail before any provider I/O', async () => {
  const result = await runInvalidImage([descriptor({ media_type: 'image/bmp' })], () => ({
    image_index: 0,
    offset: 0,
    data: '',
    eof: true,
  }))
  assert.equal(result.requests, 0)
})

test('malformed or changing chunks fail before any provider I/O', async () => {
  const malformed = await runInvalidImage([descriptor()], () => ({
    image_index: 0,
    offset: 0,
    data: '!!!not-base64!!!',
    eof: true,
  }))
  assert.equal(malformed.requests, 0)

  const changing = await runInvalidImage([descriptor({ sha256: '0'.repeat(64) })], () => ({
    image_index: 0,
    offset: 0,
    data: PNG_1X1.toString('base64'),
    eof: true,
  }))
  assert.equal(changing.requests, 0)
})


test('official projection failure maps to image_input_failed without provider I/O', async () => {
  const endpoint = await startModelEndpoint([])
  const root = mkdtempSync(join(tmpdir(), 'komari-image-projection-'))
  const profile = writeTestProfile(root, endpoint.baseURL)
  const patch = readFileSync(profile.patchPath, 'utf8').replace(
    '            maxTokens: 128',
    '            maxTokens: 128\n            input: [text, image]',
  )
  writeFileSync(profile.patchPath, patch, 'utf8')
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(root, 'home'),
      attachmentRoot: join(root, 'attachments'),
      sendFrame: async (frame) => {
        if (frame.kind !== 'callback') return
        const payload = frame.payload as { name: string; data: { image_index?: number; offset?: number } }
        if (payload.name === 'admission') {
          runtime?.callbacks.handleResult(frame.id, { ok: true, result: { allow: true } })
          return
        }
        if (payload.name === 'chat.read_image_chunk') {
          runtime?.callbacks.handleResult(frame.id, {
            ok: true,
            result: {
              image_index: payload.data.image_index ?? -1,
              offset: payload.data.offset ?? -1,
              data: PNG_1X1.toString('base64'),
              eof: true,
            },
          })
        }
      },
    })
    const store = runtime.ctx.get('attachments') as TaskAttachmentStore
    store.readImageRequest = async () => {
      throw new LlmError('projection fault', 'IMAGE_INPUT_FAILED')
    }
    await assert.rejects(runtime.service.runTask('projection-failure', {
      task_id: 'projection-failure-task',
      kind: 'chat',
      task_data: dataWithImages([descriptor()]),
      budget: BUDGET,
    }), { message: 'image_input_failed' })
    assert.equal(endpoint.requests.length, 0)
  } finally {
    await runtime?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
})
