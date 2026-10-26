import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
import type {} from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-settings'
import { startHost } from '../dist/host.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))

test('the private composition mounts only the allowlisted runtime services', async () => {
  const root = mkdtempSync(join(tmpdir(), 'komari-composition-'))
  const profile = join(root, 'profile')
  mkdirSync(profile, { recursive: true })
  writeFileSync(join(profile, 'package.json'), `${JSON.stringify({
    name: 'komari-composition-test',
    private: true,
    dsh: { profile: { bundles: ['@komari-bot/dsh-runtime-bundle'] } },
  }, null, 2)}\n`)
  const patch = join(profile, 'cordis.patch.yml')
  writeFileSync(patch, '[]\n')
  const runtime = await startHost({
    profileDir: profile,
    installAnchor: DSH_PACKAGE,
    baseConfig: DSH_BASE_CONFIG,
    dshHome: join(root, 'home'),
    sendFrame: async () => undefined,
  })
  try {
    assert.deepEqual(runtime.bindings, { chat: false, summary: false })
    for (const forbidden of [
      'sessionPersistence',
      'sessionLog',
      'pluginManager',
      'sandboxPolicy',
      'userApproval',
      'jobs',
      'skills',
      'mcp',
      'workingDirectory',
      'ptcRuntime',
    ]) {
      assert.equal(runtime.ctx.get(forbidden as never), undefined, `unexpected service ${forbidden}`)
    }
    assert.deepEqual(runtime.ctx.tools.schemas(), [])
    const presets = await runtime.ctx.agentPresets.list()
    assert.deepEqual(presets.map((preset) => preset.id).sort(), ['chat', 'summary'])

    const settings = runtime.ctx.settings.describe({ redactSecrets: true })
    const project = settings.find((descriptor) => descriptor.ns === 'komari-agent')
    assert.ok(project !== undefined, 'missing komari-agent settings namespace')
    assert.ok(project.value !== null && typeof project.value === 'object')
    const value = project.value as Record<string, unknown>
    assert.ok('chat' in value)
    assert.ok('summary' in value)
    assert.equal(readFileSync(patch, 'utf8'), '[]\n', 'boot must not rewrite the saved user patch')
  } finally {
    await runtime.close()
    rmSync(root, { recursive: true, force: true })
  }
})
