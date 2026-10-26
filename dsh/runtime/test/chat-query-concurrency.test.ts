import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
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
process.env.FIXTURE_KEY = 'fixture-test-key'

function userText(request: { body: { messages?: Array<{ role?: string; content?: unknown }> } }): string {
  return (request.body.messages ?? [])
    .filter((message) => message.role === 'user')
    .map((message) => String(message.content ?? ''))
    .join('\n')
}

test('concurrent tasks receive only their own frozen capability snapshot', async () => {
  const endpoint = await startModelEndpoint([
    { kind: 'tool', name: 'final_response', args: { content: 'reply' } },
    { kind: 'tool', name: 'final_response', args: { content: 'reply' } },
    { kind: 'tool', name: 'record_favorability_delta', args: { delta: 0, reason: 'ok' } },
    { kind: 'tool', name: 'record_favorability_delta', args: { delta: 0, reason: 'ok' } },
  ])
  const root = mkdtempSync(join(tmpdir(), 'komari-chat-query-concurrency-'))
  const profile = writeTestProfile(root, endpoint.baseURL, { prompt: 'Frozen concurrency role.' })
  const runtimeRef: { current?: Awaited<ReturnType<typeof startHost>> } = {}
  const simulator = new ChatCallbackSimulator()
  try {
    runtimeRef.current = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(root, 'home'),
      attachmentRoot: join(root, 'attachments'),
      sendFrame: chatSendFrame(runtimeRef, simulator),
    })
    const budget = { max_rounds: 4, max_tool_calls_per_round: 3, max_total_tool_calls: 8 }
    const taskA = runtimeRef.current.service.runTask('request-a', {
      task_id: 'task-a',
      kind: 'chat',
      task_data: minimalChatTaskData({
        text: 'task-a',
        capabilities: { knowledge: true, search: false, fetch: false },
      }),
      budget,
    })
    const taskB = runtimeRef.current.service.runTask('request-b', {
      task_id: 'task-b',
      kind: 'chat',
      task_data: minimalChatTaskData({
        text: 'task-b',
        capabilities: { knowledge: false, search: true, fetch: true },
      }),
      budget,
    })
    const [resultA, resultB] = await Promise.all([taskA, taskB])

    assert.equal(resultA.content, 'reply')
    assert.equal(resultB.content, 'reply')
    assert.equal(endpoint.requests.length, 4)
    for (const request of endpoint.requests) {
      const text = userText(request)
      const names = request.body.tools?.map((tool) => tool.function?.name).sort() ?? []
      if (text.includes('task-a')) {
        assert.deepEqual(names, [
          'final_response',
          'recall_memory',
          'record_favorability_delta',
          'search_knowledge',
        ])
      } else if (text.includes('task-b')) {
        assert.deepEqual(names, [
          'fetch_page',
          'final_response',
          'recall_memory',
          'record_favorability_delta',
          'search_web',
        ])
      } else {
        assert.fail('request is not attributable to either concurrent task')
      }
    }
  } finally {
    await runtimeRef.current?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
})
