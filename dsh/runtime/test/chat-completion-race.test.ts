import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'
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
process.env.FIXTURE_KEY = 'fixture-test-key'

function racePatches(): readonly PatchOptions[] {
  return [{
    insert: [{ id: 'komari-race-fixture', name: '@komari-bot/dsh-runtime/race-fixture' }],
  }]
}

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: (() => void) | undefined
  const promise = new Promise<void>((done) => {
    resolve = done
  })
  return { promise, resolve: () => resolve?.() }
}

test('queued steering drains after same-batch completion without another request or callback', async () => {
  const endpoint = await startModelEndpoint([{
    kind: 'tools',
    calls: [
      { name: 'final_response', args: { content: 'race reply' }, id: 'call-final' },
      {
        name: 'record_favorability_delta',
        args: { delta: 1, reason: 'race favor' },
        id: 'call-favor',
      },
    ],
  }])
  const root = mkdtempSync(join(tmpdir(), 'komari-chat-race-'))
  const profile = writeTestProfile(root, endpoint.baseURL, { prompt: 'Frozen race role.' })
  const runtimeRef: { current?: Awaited<ReturnType<typeof startHost>> } = {}
  const simulator = new ChatCallbackSimulator()
  const favorStarted = deferred()
  const releaseFavor = deferred()
  const callbackNames: string[] = []
  const baseSendFrame = chatSendFrame(runtimeRef, simulator, {
    onCallback: (name) => callbackNames.push(name),
  })

  try {
    runtimeRef.current = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(root, 'home'),
      extraPatches: racePatches(),
      sendFrame: async (frame) => {
        if (frame.kind !== 'callback') return
        const payload = frame.payload as { name: string; data: Record<string, unknown> }
        if (payload.name !== 'chat.submit_favorability') {
          await baseSendFrame(frame)
          return
        }
        callbackNames.push(payload.name)
        favorStarted.resolve()
        await releaseFavor.promise
        const receipt = simulator.submit(payload.name, payload.data)
        runtimeRef.current?.callbacks.handleResult(frame.id, {
          ok: true,
          result: receipt as unknown as import('../dist/protocol.js').JsonValue,
        })
      },
    })

    const task = runtimeRef.current.service.runTask('race-request', {
      task_id: 'race-task',
      kind: 'chat',
      task_data: minimalChatTaskData({ text: 'race input' }),
      budget: { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 6 },
    })
    await favorStarted.promise
    const steer = (globalThis as typeof globalThis & {
      __komariCompletionRaceSteer?: () => void
    }).__komariCompletionRaceSteer
    assert.ok(steer, 'race steering hook is installed')
    steer()
    releaseFavor.resolve()
    const result = await task

    assert.deepEqual(result, {
      content: 'race reply',
      favorability_delta: 1,
      favorability_reason: 'race favor',
    })
    assert.equal(endpoint.requests.length, 1)
    assert.deepEqual(callbackNames, ['admission', 'chat.submit_final_response', 'chat.submit_favorability'])
  } finally {
    releaseFavor.resolve()
    await runtimeRef.current?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
})
