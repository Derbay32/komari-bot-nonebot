import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { startHost } from '../dist/host.js'
import type { JsonValue } from '../dist/protocol.js'
import { fixturePatches, minimalChatTaskData, startModelEndpoint } from './support.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))

interface TestProfile {
  readonly root: string
  readonly profileDir: string
}

function emptyProfile(): TestProfile {
  const root = mkdtempSync(join(tmpdir(), 'komari-management-'))
  const profileDir = join(root, 'profile')
  mkdirSync(profileDir, { recursive: true })
  writeFileSync(join(profileDir, 'package.json'), `${JSON.stringify({
    name: 'komari-management-test',
    private: true,
    dsh: { profile: { bundles: ['@komari-bot/dsh-runtime-bundle'] } },
  }, null, 2)}\n`)
  writeFileSync(join(profileDir, 'cordis.patch.yml'), '[]\n')
  return { root, profileDir }
}

function asObject(value: JsonValue | undefined): Record<string, JsonValue> {
  assert.ok(value !== null && typeof value === 'object' && !Array.isArray(value))
  return value as Record<string, JsonValue>
}

test('an empty profile is configurable through the management surface', async () => {
  const profile = emptyProfile()
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(profile.root, 'home'),
      attachmentRoot: join(profile.root, 'attachments'),
      sendFrame: async () => undefined,
    })

    const empty = asObject(await runtime.management.handle('providers.list', {}))
    assert.deepEqual(empty.items, [])
    assert.ok(Array.isArray(empty.protocols))
    assert.ok(empty.protocols.includes('openai-completions'))
    assert.equal(typeof empty.etag, 'string')

    const created = asObject(await runtime.management.handle('providers.put', {
      provider: 'fixture',
      expectedEtag: empty.etag as string,
      profile: {
        api: 'openai-completions',
        baseURL: 'http://127.0.0.1:9/v1',
        displayName: 'Fixture',
        models: [{ id: 'fixture-model', contextWindow: 8192, maxTokens: 128 }],
        defaultInput: ['text'],
        timeoutMs: 1000,
        streamIdleTimeoutMs: 1000,
      },
    }))
    const provider = asObject(created.provider)
    assert.equal(provider.provider, 'fixture')
    assert.equal(provider.api, 'openai-completions')
    assert.equal(provider.baseURL, 'http://127.0.0.1:9/v1')
    assert.equal(provider.displayName, 'Fixture')
    assert.equal(typeof created.etag, 'string')
    assert.equal(JSON.stringify(created).includes('apiKeyEnv'), false)
    assert.equal(JSON.stringify(created).includes('retryPolicy'), false)

    const fetched = asObject(await runtime.management.handle('providers.get', { provider: 'fixture' }))
    assert.deepEqual(fetched.provider, created.provider)

    await assert.rejects(
      runtime.management.handle('providers.put', {
        provider: 'fixture',
        expectedEtag: empty.etag as string,
        profile: {
          api: 'openai-completions',
          baseURL: 'http://127.0.0.1:9/v1',
          models: [{ id: 'fixture-model' }],
        },
      }),
      (error: unknown) => (error as { code?: string }).code === 'revision_conflict',
    )
  } finally {
    await runtime?.close()
    rmSync(profile.root, { recursive: true, force: true })
  }
})

function providerProfile(modelIds: readonly string[], displayName?: string): Record<string, JsonValue> {
  return {
    api: 'openai-completions',
    baseURL: 'http://127.0.0.1:9/v1',
    ...(displayName === undefined ? {} : { displayName }),
    models: modelIds.map((id) => ({ id, contextWindow: 8192, maxTokens: 128 })),
  }
}

test('provider, credential, binding and prompt operations preserve unrelated state', async () => {
  const profile = emptyProfile()
  const sentinel = 'local-management-secret-9b18'
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(profile.root, 'home'),
      attachmentRoot: join(profile.root, 'attachments'),
      sendFrame: async () => undefined,
    })

    const empty = asObject(await runtime.management.handle('providers.list', {}))
    const alpha = asObject(await runtime.management.handle('providers.put', {
      provider: 'alpha',
      expectedEtag: empty.etag as string,
      profile: providerProfile(['model-a', 'model-b'], 'Alpha'),
    }))
    const beta = asObject(await runtime.management.handle('providers.put', {
      provider: 'beta',
      expectedEtag: alpha.etag as string,
      profile: providerProfile(['model-c'], 'Beta'),
    }))
    const listed = asObject(await runtime.management.handle('providers.list', {}))
    assert.deepEqual((listed.items as JsonValue[]).map((item) => asObject(item).provider), ['alpha', 'beta'])

    const credentialBefore = asObject(await runtime.management.handle('credential.get', { provider: 'alpha' }))
    assert.equal(asObject(credentialBefore.credential).configured, false)
    const credentialSaved = asObject(await runtime.management.handle('credential.put', {
      provider: 'alpha',
      value: sentinel,
      expectedEtag: credentialBefore.etag as string,
    }))
    assert.equal(asObject(credentialSaved.credential).configured, true)
    assert.equal(JSON.stringify(credentialSaved).includes(sentinel), false)

    const chatBefore = asObject(await runtime.management.handle('binding.get', { kind: 'chat' }))
    const chatSaved = asObject(await runtime.management.handle('binding.put', {
      kind: 'chat',
      provider: 'alpha',
      model: 'model-a',
      expectedEtag: chatBefore.etag as string,
    }))
    assert.equal(asObject(chatSaved.binding).provider, 'alpha')
    assert.equal(asObject(chatSaved.binding).model, 'model-a')

    const summaryBefore = asObject(await runtime.management.handle('binding.get', { kind: 'summary' }))
    await runtime.management.handle('binding.put', {
      kind: 'summary',
      provider: 'beta',
      model: 'model-c',
      expectedEtag: summaryBefore.etag as string,
    })

    const promptBefore = asObject(await runtime.management.handle('prompt.get', { kind: 'chat' }))
    assert.equal(asObject(promptBefore.prompt).value, '')
    const promptSaved = asObject(await runtime.management.handle('prompt.put', {
      kind: 'chat',
      value: 'literal administrator prompt',
      expectedEtag: promptBefore.etag as string,
    }))
    assert.equal(asObject(promptSaved.prompt).value, 'literal administrator prompt')
    const summaryPromptBefore = asObject(await runtime.management.handle('prompt.get', { kind: 'summary' }))
    await runtime.management.handle('prompt.put', {
      kind: 'summary',
      value: 'summary prompt',
      expectedEtag: summaryPromptBefore.etag as string,
    })

    // Provider and binding edits must not disturb the other binding or prompt.
    const betaUpdated = asObject(await runtime.management.handle('providers.put', {
      provider: 'beta',
      expectedEtag: beta.etag as string,
      profile: providerProfile(['model-c'], 'Beta renamed'),
    }))
    assert.equal(asObject(betaUpdated.provider).displayName, 'Beta renamed')
    const chatAfterBeta = asObject(await runtime.management.handle('binding.get', { kind: 'chat' }))
    assert.equal(asObject(chatAfterBeta.binding).model, 'model-a')
    const promptAfterBeta = asObject(await runtime.management.handle('prompt.get', { kind: 'chat' }))
    assert.equal(asObject(promptAfterBeta.prompt).value, 'literal administrator prompt')

    await assert.rejects(
      runtime.management.handle('binding.put', {
        kind: 'chat',
        provider: 'alpha',
        model: 'missing-model',
        expectedEtag: chatAfterBeta.etag as string,
      }),
      (error: unknown) => (error as { code?: string }).code === 'invalid_binding',
    )

    await assert.rejects(
      runtime.management.handle('providers.delete', {
        provider: 'alpha',
        expectedEtag: betaUpdated.etag as string,
      }),
      (error: unknown) => (error as { code?: string }).code === 'provider_in_use',
    )

    const chatDeleted = asObject(await runtime.management.handle('binding.delete', {
      kind: 'chat',
      expectedEtag: chatAfterBeta.etag as string,
    }))
    assert.equal(asObject(chatDeleted.binding).provider, undefined)
    const promptAfterBindingDelete = asObject(await runtime.management.handle('prompt.get', { kind: 'chat' }))
    assert.equal(asObject(promptAfterBindingDelete.prompt).value, 'literal administrator prompt')

    const providerListBeforeDelete = asObject(await runtime.management.handle('providers.list', {}))
    const alphaDeleted = asObject(await runtime.management.handle('providers.delete', {
      provider: 'alpha',
      expectedEtag: providerListBeforeDelete.etag as string,
    }))
    assert.equal(alphaDeleted.deleted, true)
    const orphanCredential = asObject(await runtime.management.handle('credential.get', { provider: 'alpha' }))
    assert.equal(asObject(orphanCredential.credential).configured, true)
    const orphanDeleted = asObject(await runtime.management.handle('credential.delete', {
      provider: 'alpha',
      expectedEtag: orphanCredential.etag as string,
    }))
    assert.equal(asObject(orphanDeleted.credential).configured, false)
    assert.equal(JSON.stringify(orphanDeleted).includes(sentinel), false)

    await runtime.management.handle('binding.delete', {
      kind: 'summary',
      expectedEtag: asObject(await runtime.management.handle('binding.get', { kind: 'summary' })).etag as string,
    })
    await runtime.management.handle('providers.delete', {
      provider: 'beta',
      expectedEtag: asObject(await runtime.management.handle('providers.list', {})).etag as string,
    })
    const finalList = asObject(await runtime.management.handle('providers.list', {}))
    assert.deepEqual(finalList.items, [])
  } finally {
    await runtime?.close()
    rmSync(profile.root, { recursive: true, force: true })
  }
})

test('saved management state survives restart and old ETags never match', async () => {
  const profile = emptyProfile()
  const home = join(profile.root, 'home')
  const first = await startHost({
    profileDir: profile.profileDir,
    installAnchor: DSH_PACKAGE,
    baseConfig: DSH_BASE_CONFIG,
    dshHome: home,
    attachmentRoot: join(profile.root, 'attachments'),
    sendFrame: async () => undefined,
  })
  let firstEtag: string
  try {
    const empty = asObject(await first.management.handle('providers.list', {}))
    const created = asObject(await first.management.handle('providers.put', {
      provider: 'fixture',
      expectedEtag: empty.etag as string,
      profile: providerProfile(['fixture-model'], 'Fixture'),
    }))
    firstEtag = created.etag as string
    const credential = asObject(await first.management.handle('credential.get', { provider: 'fixture' }))
    await first.management.handle('credential.put', {
      provider: 'fixture',
      value: 'restart-secret-4c21',
      expectedEtag: credential.etag as string,
    })
    const binding = asObject(await first.management.handle('binding.get', { kind: 'chat' }))
    await first.management.handle('binding.put', {
      kind: 'chat',
      provider: 'fixture',
      model: 'fixture-model',
      expectedEtag: binding.etag as string,
    })
    const prompt = asObject(await first.management.handle('prompt.get', { kind: 'chat' }))
    await first.management.handle('prompt.put', {
      kind: 'chat',
      value: 'durable prompt',
      expectedEtag: prompt.etag as string,
    })
  } finally {
    await first.close()
  }

  const second = await startHost({
    profileDir: profile.profileDir,
    installAnchor: DSH_PACKAGE,
    baseConfig: DSH_BASE_CONFIG,
    dshHome: home,
    attachmentRoot: join(profile.root, 'attachments'),
    sendFrame: async () => undefined,
  })
  try {
    const listed = asObject(await second.management.handle('providers.list', {}))
    assert.notEqual(listed.etag, firstEtag)
    assert.deepEqual((listed.items as JsonValue[]).map((item) => asObject(item).provider), ['fixture'])
    const binding = asObject(await second.management.handle('binding.get', { kind: 'chat' }))
    assert.equal(asObject(binding.binding).model, 'fixture-model')
    const prompt = asObject(await second.management.handle('prompt.get', { kind: 'chat' }))
    assert.equal(asObject(prompt.prompt).value, 'durable prompt')
    const credential = asObject(await second.management.handle('credential.get', { provider: 'fixture' }))
    assert.equal(asObject(credential.credential).configured, true)
    await assert.rejects(
      second.management.handle('providers.put', {
        provider: 'fixture',
        expectedEtag: firstEtag,
        profile: providerProfile(['fixture-model']),
      }),
      (error: unknown) => (error as { code?: string }).code === 'revision_conflict',
    )
  } finally {
    await second.close()
    rmSync(profile.root, { recursive: true, force: true })
  }
})

test('live tasks block connection writes while binding and prompt edits remain live', async () => {
  const profile = emptyProfile()
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(profile.root, 'home'),
      attachmentRoot: join(profile.root, 'attachments'),
      sendFrame: async () => undefined,
    })
    const empty = asObject(await runtime.management.handle('providers.list', {}))
    const created = asObject(await runtime.management.handle('providers.put', {
      provider: 'fixture',
      expectedEtag: empty.etag as string,
      profile: providerProfile(['fixture-model'], 'Fixture'),
    }))
    const lease = await runtime.service.gate.enterTask()
    try {
      const listed = asObject(await runtime.management.handle('providers.list', {}))
      await assert.rejects(
        runtime.management.handle('providers.put', {
          provider: 'fixture',
          expectedEtag: listed.etag as string,
          profile: providerProfile(['fixture-model'], 'Should not save'),
        }),
        (error: unknown) => (error as { code?: string }).code === 'active_tasks',
      )
      const credential = asObject(await runtime.management.handle('credential.get', { provider: 'fixture' }))
      await assert.rejects(
        runtime.management.handle('credential.put', {
          provider: 'fixture',
          value: 'must-not-save',
          expectedEtag: credential.etag as string,
        }),
        (error: unknown) => (error as { code?: string }).code === 'active_tasks',
      )

      const binding = asObject(await runtime.management.handle('binding.get', { kind: 'chat' }))
      await runtime.management.handle('binding.put', {
        kind: 'chat',
        provider: 'fixture',
        model: 'fixture-model',
        expectedEtag: binding.etag as string,
      })
      const prompt = asObject(await runtime.management.handle('prompt.get', { kind: 'chat' }))
      await runtime.management.handle('prompt.put', {
        kind: 'chat',
        value: 'edited while live',
        expectedEtag: prompt.etag as string,
      })
    } finally {
      lease.release()
    }

    const after = asObject(await runtime.management.handle('providers.get', { provider: 'fixture' }))
    assert.equal(asObject(after.provider).displayName, 'Fixture')
    const credentialAfter = asObject(await runtime.management.handle('credential.get', { provider: 'fixture' }))
    assert.equal(asObject(credentialAfter.credential).configured, false)
    assert.equal(created.etag === after.etag, true)
  } finally {
    await runtime?.close()
    rmSync(profile.root, { recursive: true, force: true })
  }
})

test('an official write failure is reported as unavailable without leaking its text', async () => {
  const profile = emptyProfile()
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  const secret = 'fault-injection-secret-29af'
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(profile.root, 'home'),
      attachmentRoot: join(profile.root, 'attachments'),
      sendFrame: async () => undefined,
    })
    const empty = asObject(await runtime.management.handle('providers.list', {}))
    const mutate = runtime.ctx.settings.mutate.bind(runtime.ctx.settings)
    runtime.ctx.settings.mutate = async () => {
      throw new Error(`injected failure ${secret}`)
    }
    try {
      await assert.rejects(
        runtime.management.handle('providers.put', {
          provider: 'fixture',
          expectedEtag: empty.etag as string,
          profile: providerProfile(['fixture-model']),
        }),
        (error: unknown) => {
          assert.equal((error as { code?: string }).code, 'unavailable')
          assert.equal(String((error as Error).message).includes(secret), false)
          return true
        },
      )
    } finally {
      runtime.ctx.settings.mutate = mutate
    }
    const listed = asObject(await runtime.management.handle('providers.list', {}))
    assert.deepEqual(listed.items, [])
  } finally {
    await runtime?.close()
    rmSync(profile.root, { recursive: true, force: true })
  }
})

test('task admission waits for an idle connection mutation to settle', async () => {
  const endpoint = await startModelEndpoint([
    { kind: 'tool', name: 'fixture_complete', args: { value: 'after-idle-write' } },
  ])
  const profile = emptyProfile()
  let runtime: Awaited<ReturnType<typeof startHost>> | undefined
  try {
    runtime = await startHost({
      profileDir: profile.profileDir,
      installAnchor: DSH_PACKAGE,
      baseConfig: DSH_BASE_CONFIG,
      dshHome: join(profile.root, 'home'),
      attachmentRoot: join(profile.root, 'attachments'),
      extraPatches: fixturePatches(),
      sendFrame: async (frame) => {
        if (frame.kind !== 'callback') return
        runtime?.callbacks.handleResult(frame.id, {
          ok: true,
          result: frame.payload.name === 'admission'
            ? { allow: true }
            : { ok: true, value: frame.payload.data.value ?? null },
        })
      },
    })

    const empty = asObject(await runtime.management.handle('providers.list', {}))
    const created = asObject(await runtime.management.handle('providers.put', {
      provider: 'fixture',
      expectedEtag: empty.etag as string,
      profile: {
        api: 'openai-completions',
        baseURL: endpoint.baseURL,
        models: [{ id: 'fixture-model', contextWindow: 8192, maxTokens: 128 }],
      },
    }))
    const credential = asObject(await runtime.management.handle('credential.get', { provider: 'fixture' }))
    await runtime.management.handle('credential.put', {
      provider: 'fixture',
      value: 'idle-write-secret',
      expectedEtag: credential.etag as string,
    })
    const binding = asObject(await runtime.management.handle('binding.get', { kind: 'chat' }))
    await runtime.management.handle('binding.put', {
      kind: 'chat',
      provider: 'fixture',
      model: 'fixture-model',
      expectedEtag: binding.etag as string,
    })
    const prompt = asObject(await runtime.management.handle('prompt.get', { kind: 'chat' }))
    await runtime.management.handle('prompt.put', {
      kind: 'chat',
      value: 'idle-write prompt',
      expectedEtag: prompt.etag as string,
    })

    const originalMutate = runtime.ctx.settings.mutate.bind(runtime.ctx.settings)
    let releaseMutation: (() => void) | undefined
    let signalStarted: (() => void) | undefined
    const mutationStarted = new Promise<void>((resolve) => {
      signalStarted = resolve
    })
    const held = new Promise<void>((resolve) => {
      releaseMutation = resolve
    })
    runtime.ctx.settings.mutate = (async (...args: Parameters<typeof originalMutate>) => {
      signalStarted?.()
      await held
      return await originalMutate(...args)
    }) as typeof runtime.ctx.settings.mutate

    const listed = asObject(await runtime.management.handle('providers.list', {}))
    const mutation = runtime.management.handle('providers.put', {
      provider: 'fixture',
      expectedEtag: listed.etag as string,
      profile: {
        api: 'openai-completions',
        baseURL: endpoint.baseURL,
        displayName: 'After the idle write',
        models: [{ id: 'fixture-model', contextWindow: 8192, maxTokens: 128 }],
      },
    })
    await mutationStarted

    const task = runtime.service.runTask('idle-request', {
      task_id: 'idle-task',
      kind: 'chat',
      task_data: minimalChatTaskData({ text: 'hello' }),
      budget: { max_rounds: 3, max_tool_calls_per_round: 4, max_total_tool_calls: 8 },
    })
    await new Promise((resolve) => setTimeout(resolve, 100))
    assert.equal(endpoint.requests.length, 0, 'a task must not start through an intermediate connection')

    releaseMutation?.()
    const result = await task
    assert.deepEqual(result, { ok: true, value: 'after-idle-write' })
    assert.equal(endpoint.requests.length, 1)
    const after = asObject(await mutation)
    assert.equal(asObject(after.provider).displayName, 'After the idle write')
    assert.ok(after.etag !== listed.etag)
  } finally {
    await runtime?.close()
    await endpoint.close()
    rmSync(profile.root, { recursive: true, force: true })
  }
})
