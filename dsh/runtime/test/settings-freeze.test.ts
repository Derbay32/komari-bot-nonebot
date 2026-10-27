import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { startHost } from '../dist/host.js'
import type { JsonValue } from '../dist/protocol.js'
import { minimalChatTaskData, startModelEndpoint, writeTestProfile } from './support.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))

process.env.FIXTURE_KEY = 'fixture-test-key'

function systemText(body: { messages?: Array<{ role?: string; content?: unknown }> }): string {
  return (body.messages ?? [])
    .filter((message) => message.role === 'system')
    .map((message) => (typeof message.content === 'string' ? message.content : ''))
    .join('\n')
}

test('binding and prompt edits during a task do not affect its frozen snapshot', async () => {
  const endpoint = await startModelEndpoint([
    { kind: 'tool', name: 'fixture_update_prompt', args: { prompt: 'NEW PROMPT', text: 'second turn' } },
    { kind: 'tool', name: 'fixture_complete', args: { value: 'frozen' } },
    { kind: 'tool', name: 'fixture_complete', args: { value: 'new-task' } },
  ])
  const root = mkdtempSync(join(tmpdir(), 'komari-freeze-'))
  const profile = writeTestProfile(root, endpoint.baseURL, { prompt: 'OLD PROMPT', fixturePresets: true })
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  const sendFrame = async (frame: { kind: string; id: string; payload?: unknown }): Promise<void> => {
    if (frame.kind !== 'callback') return
    const payload = frame.payload as { name: string; data: { value?: string } }
    let result: JsonValue
    if (payload.name === 'admission') result = { allow: true }
    else result = { ok: true, value: payload.data.value ?? null }
    runtime?.callbacks.handleResult(frame.id, { ok: true, result })
  }
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(root, 'home'),
      attachmentRoot: join(root, 'attachments'),
      sendFrame,
    })
    const budget = { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 }
    const first = await runtime.service.runTask('req-1', {
      task_id: 'task-1',
      kind: 'chat',
      task_data: minimalChatTaskData({ text: 'first' }),
      budget,
    })
    assert.deepEqual(first, { ok: true, value: 'frozen' })
    assert.equal(endpoint.requests.length, 2)
    assert.equal(systemText(endpoint.requests[0]?.body ?? {}), 'OLD PROMPT')
    assert.equal(systemText(endpoint.requests[1]?.body ?? {}), 'OLD PROMPT')
    assert.match(readFileSync(profile.patchPath, 'utf8'), /NEW PROMPT/)

    endpoint.enqueue({ kind: 'tool', name: 'fixture_complete', args: { value: 'new-task' } })
    const second = await runtime.service.runTask('req-2', {
      task_id: 'task-2',
      kind: 'chat',
      task_data: minimalChatTaskData({ text: 'second' }),
      budget,
    })
    assert.deepEqual(second, { ok: true, value: 'new-task' })
    assert.equal(endpoint.requests.length, 3)
    assert.equal(systemText(endpoint.requests[2]?.body ?? {}), 'NEW PROMPT')
  } finally {
    await runtime?.close()
    await endpoint.close()
    rmSync(root, { recursive: true, force: true })
  }
})
