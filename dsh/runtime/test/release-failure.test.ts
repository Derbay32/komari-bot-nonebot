import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { startHost } from '../dist/host.js'
import { minimalChatTaskData, startModelEndpoint, writeTestProfile } from './support.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))
process.env.FIXTURE_KEY = 'fixture-test-key'

for (const mode of ['reject', 'hang'] as const) {
  test(`executor release ${mode} fails closed and releases task admission`, async () => {
    const endpoint = await startModelEndpoint([
      { kind: 'tool', name: 'fixture_complete', args: { value: 'done' } },
    ])
    const root = mkdtempSync(join(tmpdir(), 'komari-release-'))
    const profile = writeTestProfile(root, endpoint.baseURL, { fixturePresets: true })
    let runtime: Awaited<ReturnType<typeof startHost>> | undefined
    try {
      runtime = await startHost({
        profileDir: profile.profileDir,
        installAnchor: DSH_PACKAGE,
        baseConfig: DSH_BASE_CONFIG,
        dshHome: join(root, 'home'),
        sendFrame: async (frame) => {
          if (frame.kind !== 'callback') return
          runtime?.callbacks.handleResult(frame.id, {
            ok: true,
            result: frame.payload.name === 'admission' ? { allow: true } : { ok: true },
          })
        },
      })
      // Fault-inject only the external executor's release boundary. Execution,
      // model I/O and tool dispatch still use the real official DSH runtime.
      const create = runtime.ctx.agents.create.bind(runtime.ctx.agents)
      runtime.ctx.agents.create = async (...args) => {
        const handle = await create(...args)
        return {
          ...handle,
          dispose: async () => {
            if (mode === 'hang') await new Promise<void>(() => {})
            await handle.dispose()
            throw new Error('release fault must not leak')
          },
        }
      }
      const request = {
        task_id: 'release-task',
        kind: 'chat' as const,
        task_data: minimalChatTaskData({ text: 'hello' }),
        budget: { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 },
      }
      await assert.rejects(runtime.service.runTask('release-request', request), { message: 'release_failed' })
      assert.equal(runtime.service.activeTaskCount, 0)
      assert.deepEqual(runtime.service.listLiveTaskIds(), [])
      await assert.rejects(runtime.service.runTask('next-request', { ...request, task_id: 'next-task' }), {
        message: 'host_closed',
      })
      assert.equal(endpoint.requests.length, 1)
    } finally {
      await runtime?.close()
      await endpoint.close()
      rmSync(root, { recursive: true, force: true })
    }
  })
}
