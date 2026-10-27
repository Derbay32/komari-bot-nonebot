import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { startHost } from '../dist/host.js'
import {
  ChatCallbackSimulator,
  chatSendFrame,
  minimalChatTaskData,
  startModelEndpoint,
  writeTestProfile,
} from './support.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)
const BUDGET = { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 }
process.env.FIXTURE_KEY = 'fixture-test-key'

function imageTaskData(): { [key: string]: import('../dist/protocol.js').JsonValue } {
  return {
    ...minimalChatTaskData({ text: 'image release task' }),
    images: [{
      index: 0,
      origin: 'current',
      media_type: 'image/png',
      byte_count: PNG_1X1.byteLength,
      sha256: createHash('sha256').update(PNG_1X1).digest('hex'),
    }],
  } as unknown as { [key: string]: import('../dist/protocol.js').JsonValue }
}

async function runReleaseFailure(mode: 'throw' | 'hang'): Promise<void> {
  const endpoint = await startModelEndpoint([
    { kind: 'tool', name: 'final_response', args: { content: 'release reply' } },
    { kind: 'tool', name: 'record_favorability_delta', args: { delta: 0, reason: 'release' } },
  ])
  const root = mkdtempSync(join(tmpdir(), 'komari-attachment-release-'))
  const profile = writeTestProfile(root, endpoint.baseURL)
  const patch = readFileSync(profile.patchPath, 'utf8').replace(
    '            maxTokens: 128',
    '            maxTokens: 128\n            input: [text, image]',
  )
  writeFileSync(profile.patchPath, patch, 'utf8')
  const runtimeRef: { current?: Awaited<ReturnType<typeof startHost>> } = {}
  const simulator = new ChatCallbackSimulator()
  const baseSend = chatSendFrame(runtimeRef, simulator)
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
          return
        }
        await baseSend(frame)
      },
    })
    runtimeRef.current = runtime
    const create = runtime.ctx.agents.create.bind(runtime.ctx.agents)
    runtime.ctx.agents.create = async (...args) => {
      const handle = await create(...args)
      return {
        ...handle,
        dispose: async () => {
          if (mode === 'hang') await new Promise<void>(() => {})
          await handle.dispose()
          throw new Error('disposal fault')
        },
      }
    }

    await assert.rejects(runtime.service.runTask(`release-${mode}`, {
      task_id: `release-${mode}-task`,
      kind: 'chat',
      task_data: imageTaskData(),
      budget: BUDGET,
    }), { message: 'release_failed' })
    assert.deepEqual(runtime.service.listLiveTaskIds(), [])
    const owners = readdirSync(join(root, 'attachments'))
    assert.ok(owners.length > 0, 'attachments must remain owned until process retirement')
  } finally {
    await runtime?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
}

test('thrown executor disposal retains task attachments for process retirement', async () => {
  await runReleaseFailure('throw')
})

test('timed-out executor disposal retains task attachments for process retirement', async () => {
  await runReleaseFailure('hang')
})

test('normal executor disposal releases task attachments before close', async () => {
  const endpoint = await startModelEndpoint([
    { kind: 'tool', name: 'final_response', args: { content: 'normal release' } },
    { kind: 'tool', name: 'record_favorability_delta', args: { delta: 0, reason: 'normal' } },
  ])
  const root = mkdtempSync(join(tmpdir(), 'komari-attachment-release-ok-'))
  const profile = writeTestProfile(root, endpoint.baseURL)
  const patch = readFileSync(profile.patchPath, 'utf8').replace(
    '            maxTokens: 128',
    '            maxTokens: 128\n            input: [text, image]',
  )
  writeFileSync(profile.patchPath, patch, 'utf8')
  const runtimeRef: { current?: Awaited<ReturnType<typeof startHost>> } = {}
  const simulator = new ChatCallbackSimulator()
  const baseSend = chatSendFrame(runtimeRef, simulator)
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
          return
        }
        await baseSend(frame)
      },
    })
    runtimeRef.current = runtime
    const result = await runtime.service.runTask('normal-release', {
      task_id: 'normal-release-task',
      kind: 'chat',
      task_data: imageTaskData(),
      budget: BUDGET,
    })
    assert.equal(result.content, 'normal release')
    assert.deepEqual(readdirSync(join(root, 'attachments')), [])
  } finally {
    await runtime?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
})
