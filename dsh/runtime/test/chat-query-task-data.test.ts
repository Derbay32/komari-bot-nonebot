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

function probePatch(): readonly PatchOptions[] {
  return [{
    insert: [{ id: 'komari-task-data-probe', name: '@komari-bot/dsh-runtime/task-data-probe' }],
  }]
}

test('reading a completed but not-yet-disposed task snapshot keeps its successful result', async () => {
  const endpoint = await startModelEndpoint([{
    kind: 'tools',
    calls: [
      { name: 'final_response', args: { content: 'completed reply' }, id: 'call-final' },
      { name: 'record_favorability_delta', args: { delta: 1, reason: 'complete' }, id: 'call-favor' },
    ],
  }])
  const root = mkdtempSync(join(tmpdir(), 'komari-task-data-probe-'))
  const profile = writeTestProfile(root, endpoint.baseURL, { prompt: 'Frozen probe role.' })
  const runtimeRef: { current?: Awaited<ReturnType<typeof startHost>> } = {}
  const simulator = new ChatCallbackSimulator()
  try {
    runtimeRef.current = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(root, 'home'),
      extraPatches: probePatch(),
      sendFrame: chatSendFrame(runtimeRef, simulator),
    })
    const result = await runtimeRef.current.service.runTask('probe-request', {
      task_id: 'probe-task',
      kind: 'chat',
      task_data: minimalChatTaskData({
        text: 'probe input',
        capabilities: { knowledge: true, search: false, fetch: false },
      }),
      budget: { max_rounds: 3, max_tool_calls_per_round: 3, max_total_tool_calls: 5 },
    })
    assert.deepEqual(result, {
      content: 'completed reply',
      favorability_delta: 1,
      favorability_reason: 'complete',
    })
    const reads = (globalThis as typeof globalThis & {
      __komariTaskDataProbeReads?: Array<{ readonly [key: string]: unknown } | null>
    }).__komariTaskDataProbeReads
    assert.ok(reads !== undefined && reads.length > 0, 'probe did not observe turn stopping')
    const snapshot = reads.at(-1)
    assert.ok(snapshot !== null && snapshot !== undefined)
    assert.equal((snapshot.current as { content: string }).content, 'probe input')
    assert.deepEqual(snapshot.capabilities, { knowledge: true, search: false, fetch: false })
  } finally {
    await runtimeRef.current?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
})
