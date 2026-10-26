import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { AddressInfo } from 'node:net'
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'
import { fixturePresetPatches } from '../dist/test-fixtures.js'

export interface ModelRequestRecord {
  readonly body: {
    readonly model?: string
    readonly messages?: Array<{ role?: string; content?: unknown }>
    readonly tools?: Array<{ function?: { name?: string } }>
  }
}

export type ModelResponse =
  | { readonly kind: 'tool'; readonly name: string; readonly args: Record<string, unknown> }
  | { readonly kind: 'text'; readonly text: string }
  | { readonly kind: 'http-error'; readonly status: number; readonly message: string }

interface Waiter {
  readonly count: number
  readonly resolve: () => void
}

export interface ModelEndpoint {
  readonly baseURL: string
  readonly requests: ModelRequestRecord[]
  enqueue(response: ModelResponse): void
  waitForRequests(count: number, timeoutMs?: number): Promise<void>
  close(): Promise<void>
}

function sseChunk(content: Record<string, unknown>): string {
  return `data: ${JSON.stringify(content)}\n\n`
}

function chunksFor(response: Exclude<ModelResponse, { readonly kind: 'http-error' }>): string {
  const base = (choices: unknown[], usage?: Record<string, number>): Record<string, unknown> => ({
    id: 'chatcmpl-komari',
    object: 'chat.completion.chunk',
    created: 1,
    model: 'fixture-model',
    choices,
    ...(usage === undefined ? {} : { usage }),
  })
  if (response.kind === 'tool') {
    return sseChunk(base([{
      index: 0,
      delta: {
        role: 'assistant',
        tool_calls: [{
          index: 0,
          id: 'call_komari_1',
          type: 'function',
          function: { name: response.name, arguments: JSON.stringify(response.args) },
        }],
      },
      finish_reason: null,
    }])) + sseChunk(base([{ index: 0, delta: {}, finish_reason: 'tool_calls' }], {
      prompt_tokens: 10,
      completion_tokens: 2,
      total_tokens: 12,
    })) + 'data: [DONE]\n\n'
  }
  return sseChunk(base([{
    index: 0,
    delta: { role: 'assistant', content: response.text },
    finish_reason: null,
  }])) + sseChunk(base([{ index: 0, delta: {}, finish_reason: 'stop' }], {
    prompt_tokens: 10,
    completion_tokens: 2,
    total_tokens: 12,
  })) + 'data: [DONE]\n\n'
}

export async function startModelEndpoint(responses: readonly ModelResponse[]): Promise<ModelEndpoint> {
  const pending = [...responses]
  const requests: ModelRequestRecord[] = []
  const waiters: Waiter[] = []
  const server = createServer((request: IncomingMessage, response: ServerResponse) => {
    const bodyChunks: Buffer[] = []
    request.on('data', (chunk: Buffer) => bodyChunks.push(chunk))
    request.on('end', () => {
      const raw = Buffer.concat(bodyChunks).toString('utf8')
      try {
        requests.push({ body: JSON.parse(raw) as ModelRequestRecord['body'] })
      } catch {
        requests.push({ body: {} })
      }
      for (const waiter of [...waiters]) {
        if (requests.length >= waiter.count) {
          waiters.splice(waiters.indexOf(waiter), 1)
          waiter.resolve()
        }
      }
      const next = pending.shift()
      if (next === undefined) {
        response.writeHead(500, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ error: { message: 'no scripted response' } }))
        return
      }
      if (next.kind === 'http-error') {
        const encoded = Buffer.from(JSON.stringify({ error: { message: next.message, type: 'error', code: 'error' } }))
        response.writeHead(next.status, { 'content-type': 'application/json', 'content-length': String(encoded.length) })
        response.end(encoded)
        return
      }
      response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
      response.end(chunksFor(next))
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const address = server.address() as AddressInfo
  const baseURL = `http://127.0.0.1:${address.port}/v1`
  return {
    baseURL,
    requests,
    enqueue: (value: ModelResponse) => {
      pending.push(value)
    },
    waitForRequests: async (count: number, timeoutMs = 15000) => {
      if (requests.length >= count) return
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timed out waiting for ${count} model requests`)), timeoutMs)
        waiters.push({
          count,
          resolve: () => {
            clearTimeout(timer)
            resolve()
          },
        })
      })
    },
    close: async () => {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => (error === undefined ? resolve() : reject(error)))
      })
    },
  }
}

export interface TestProfile {
  readonly dataDir: string
  readonly profileDir: string
  readonly patchPath: string
}

export function writeTestProfile(
  root: string,
  baseURL: string,
  options: { readonly prompt?: string; readonly compaction?: boolean; readonly fixturePresets?: boolean } = {},
): TestProfile {
  const dataDir = join(root, 'data')
  const profileDir = join(dataDir, 'dsh')
  mkdirSync(profileDir, { recursive: true })
  const manifest = {
    name: 'komari-agent-node-test-profile',
    private: true,
    dsh: { profile: { bundles: ['@komari-bot/dsh-runtime-bundle'] } },
  }
  writeFileSync(join(profileDir, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')
  const lines = [
    '- id: llm-pi-ai',
    '  config:',
    '    providers:',
    '      fixture:',
    '        api: openai-completions',
    `        baseURL: ${JSON.stringify(baseURL)}`,
    '        apiKeyEnv: FIXTURE_KEY',
    '        models:',
    '          - id: fixture-model',
    '            contextWindow: 8192',
    '            maxTokens: 128',
    '        retryPolicy:',
    '          mode: normal',
    '          maxRetries: 2',
    '          retryableCodes: [RATE_LIMIT, TIMEOUT, TRANSPORT]',
    '          backoff:',
    '            initialDelayMs: 1',
    '            maxDelayMs: 5',
    '            jitterRatio: 0',
    '- id: komari-agent',
    '  config:',
    '    chat:',
    '      provider: fixture',
    '      model: fixture-model',
    `      prompt: ${JSON.stringify(options.prompt ?? 'You are a fixture.')}`,
    '    summary:',
    '      provider: fixture',
    '      model: fixture-model',
    '      prompt: "You summarize fixtures."',
  ]
  if (options.fixturePresets === true) {
    lines.push(
      '- id: preset-chat',
      '  config:',
      '    id: chat',
      '    name: Fixture chat',
      '    plugins:',
      '      - id: fixture-chat',
      "        name: '@komari-bot/dsh-runtime/fixture'",
      '- id: preset-summary',
      '  config:',
      '    id: summary',
      '    name: Fixture summary',
      '    plugins:',
      '      - id: fixture-summary',
      "        name: '@komari-bot/dsh-runtime/fixture'",
    )
  }
  const patchPath = join(profileDir, 'cordis.patch.yml')
  writeFileSync(patchPath, `${lines.join('\n')}\n`, 'utf8')
  return { dataDir, profileDir, patchPath }
}

export function tempRoot(): string {
  return mkdtempSync(join(tmpdir(), 'komari-dsh-node-'))
}

export function fixturePatches(): readonly PatchOptions[] {
  return fixturePresetPatches()
}
