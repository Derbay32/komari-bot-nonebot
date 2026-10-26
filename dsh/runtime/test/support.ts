import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { AddressInfo } from 'node:net'
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'
import type { JsonValue } from '../dist/protocol.js'
import { fixturePresetPatches } from '../dist/test-fixtures.js'

export interface ModelRequestRecord {
  readonly body: {
    readonly model?: string
    readonly messages?: Array<{ role?: string; content?: unknown }>
    readonly tools?: Array<{ function?: { name?: string } }>
  }
}

export interface ToolCallSpec {
  readonly name: string
  readonly args: Record<string, unknown>
  readonly id?: string
}

export type ModelResponse =
  | { readonly kind: 'tool'; readonly name: string; readonly args: Record<string, unknown> }
  | { readonly kind: 'raw-tool'; readonly name: string; readonly argumentsText: string; readonly id?: string }
  | { readonly kind: 'tools'; readonly calls: readonly ToolCallSpec[] }
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

type ToolChunkCall = {
  readonly name: string
  readonly id?: string
  readonly args?: Record<string, unknown>
  readonly argumentsText?: string
}

function toolCallChunks(calls: readonly ToolChunkCall[]): string {
  const base = (choices: unknown[], usage?: Record<string, number>): Record<string, unknown> => ({
    id: 'chatcmpl-komari',
    object: 'chat.completion.chunk',
    created: 1,
    model: 'fixture-model',
    choices,
    ...(usage === undefined ? {} : { usage }),
  })
  return sseChunk(base([{
    index: 0,
    delta: {
      role: 'assistant',
      tool_calls: calls.map((call, index) => ({
        index,
        id: call.id ?? `call_komari_${index + 1}`,
        type: 'function',
        function: {
          name: call.name,
          arguments: call.args === undefined ? (call.argumentsText ?? '') : JSON.stringify(call.args),
        },
      })),
    },
    finish_reason: null,
  }])) + sseChunk(base([{ index: 0, delta: {}, finish_reason: 'tool_calls' }], {
    prompt_tokens: 10,
    completion_tokens: 2,
    total_tokens: 12,
  })) + 'data: [DONE]\n\n'
}

function chunksFor(response: Exclude<ModelResponse, { readonly kind: 'http-error' }>): string {
  if (response.kind === 'tool') return toolCallChunks([{ name: response.name, args: response.args }])
  if (response.kind === 'raw-tool') {
    return toolCallChunks([{ name: response.name, argumentsText: response.argumentsText, id: response.id }])
  }
  if (response.kind === 'tools') return toolCallChunks(response.calls.map((call) => ({ name: call.name, args: call.args, id: call.id })))
  const base = (choices: unknown[], usage?: Record<string, number>): Record<string, unknown> => ({
    id: 'chatcmpl-komari',
    object: 'chat.completion.chunk',
    created: 1,
    model: 'fixture-model',
    choices,
    ...(usage === undefined ? {} : { usage }),
  })
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
  options: {
    readonly prompt?: string
    readonly summaryPrompt?: string
    readonly compaction?: boolean
    readonly fixturePresets?: boolean
    readonly contextWindow?: number
  } = {},
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
    `            contextWindow: ${options.contextWindow ?? 8192}`,
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
    `      prompt: ${JSON.stringify(options.summaryPrompt ?? 'You summarize fixtures.')}`,
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

export function minimalChatTaskData(overrides: {
  readonly text?: string
  readonly recent?: readonly string[]
  readonly capabilities?: { readonly knowledge: boolean; readonly search: boolean; readonly fetch: boolean }
} = {}): { [key: string]: JsonValue } {
  const text = overrides.text ?? 'fixture message'
  return {
    schema: 1,
    current: {
      source_type: 'conversation_history',
      source_id: 'm-current',
      trust_level: 'untrusted',
      content: text,
      message_id: 'm-current',
      group_id: 'g-1',
      user_id: 'u-1',
      user_nickname: 'Tester',
      timestamp: 1,
      is_bot: false,
    },
    recent: (overrides.recent ?? []).map((content, index) => ({
      source_type: 'conversation_history',
      source_id: `m-${index + 1}`,
      trust_level: 'untrusted',
      content,
      message_id: `m-${index + 1}`,
      group_id: 'g-1',
      user_id: `u-${index + 2}`,
      user_nickname: `User ${index + 2}`,
      timestamp: index + 1,
      is_bot: false,
    })),
    quote: null,
    memory: [],
    knowledge: [],
    uid_knowledge: [],
    capabilities: overrides.capabilities ?? { knowledge: true, search: true, fetch: true },
    facts: {
      bot_nickname: 'Fixture',
      current_favorability: {
        value: 0,
        stage_index: 0,
        stage_name: 'neutral',
        stage_prompt: 'Neutral.',
      },
      visible_users: [],
      max_favorability_delta: 3,
    },
  }
}

export function minimalSummaryTaskData(overrides: { readonly request?: string } = {}): { [key: string]: JsonValue } {
  return {
    schema: 1,
    request: {
      source_type: 'group_history',
      source_id: 'group-summary-test',
      trust_level: 'untrusted',
      content: overrides.request ?? '总结最近消息',
    },
    limits: {
      default_count: 30,
      min_count: 10,
      max_count: 200,
      scan_limit: 300,
    },
  }
}

export interface SummaryHistoryRow {
  readonly message_id: string | null
  readonly reply_to_message_id: string | null
  readonly timestamp: number
  readonly user_id: string
  readonly nickname: string
  readonly is_bot: boolean
  readonly source_type: 'group_history'
  readonly source_id: string
  readonly trust_level: 'untrusted'
  readonly content: string
}

export function summaryHistoryRow(index: number, overrides: Partial<SummaryHistoryRow> = {}): SummaryHistoryRow {
  return {
    message_id: `m-${index}`,
    reply_to_message_id: null,
    timestamp: index,
    user_id: `u-${index}`,
    nickname: `用户${index}`,
    is_bot: false,
    source_type: 'group_history',
    source_id: 'recent_group_messages',
    trust_level: 'untrusted',
    content: `消息${index}`,
    ...overrides,
  }
}

export interface SummaryFetchCall {
  readonly name: string
  readonly data: Record<string, unknown>
}

export class SummaryCallbackSimulator {
  rows: SummaryHistoryRow[] = []
  pageSize = 64
  generation = 0
  read = false
  completed = false
  resultText: string | undefined
  readonly fetchCalls: SummaryFetchCall[] = []
  readonly pageCalls: Array<{ readonly generation: number; readonly offset: number }> = []
  readonly finalCalls: Array<Record<string, unknown>> = []

  submit(name: string, data: Record<string, unknown>): Record<string, unknown> {
    if (name === 'summary.fetch_recent' || name === 'summary.fetch_user' || name === 'summary.fetch_topic') {
      this.fetchCalls.push({ name, data })
      this.generation += 1
      this.read = true
      const source = name === 'summary.fetch_recent'
        ? 'recent_group_messages'
        : name === 'summary.fetch_user'
          ? 'messages_by_user'
          : 'messages_by_topic'
      return {
        generation: this.generation,
        source,
        matched_count: this.rows.length,
        selected_count: this.rows.length,
        filters: data,
        history_fetch: null,
        first_page_offset: 0,
      }
    }
    if (name === 'summary.history_page') {
      const generation = data.generation
      const offset = data.offset
      if (generation !== this.generation || typeof offset !== 'number') {
        throw new Error('stale summary page generation')
      }
      this.pageCalls.push({ generation: this.generation, offset })
      const rows = this.rows.slice(offset, offset + this.pageSize)
      const next = offset + rows.length < this.rows.length ? offset + rows.length : null
      return {
        generation: this.generation,
        offset,
        next_offset: next,
        total: this.rows.length,
        rows,
      }
    }
    if (name === 'summary.final_summary') {
      this.finalCalls.push(data)
      if (this.completed) {
        return { accepted: false, code: 'already_completed', missing: [], result: { summary_text: this.resultText } }
      }
      if (!this.read) {
        return { accepted: false, code: 'history_required', missing: ['history_read', 'final_summary'], result: null }
      }
      const content = data.content
      if (Object.keys(data).length !== 1 || typeof content !== 'string' || content.trim().length === 0) {
        return { accepted: false, code: 'invalid_content', missing: ['final_summary'], result: null }
      }
      this.resultText = content.trim()
      this.completed = true
      return { accepted: true, code: null, missing: [], result: { summary_text: this.resultText } }
    }
    throw new Error(`unexpected summary callback ${name}`)
  }
}

export interface SummarySendFrameOptions {
  readonly admission?: boolean | (() => boolean)
  readonly failFinalCallback?: boolean
  readonly onCallback?: (name: string) => void
}

export function summarySendFrame(
  runtimeRef: { current?: Awaited<ReturnType<typeof import('../dist/host.js').startHost>> },
  simulator: SummaryCallbackSimulator,
  options: SummarySendFrameOptions = {},
): (frame: { kind: string; id: string; payload?: unknown }) => Promise<void> {
  return async (frame) => {
    if (frame.kind !== 'callback') return
    const payload = frame.payload as { name: string; data: Record<string, unknown> }
    options.onCallback?.(payload.name)
    if (payload.name === 'admission') {
      const allow = typeof options.admission === 'function' ? options.admission() : (options.admission ?? true)
      runtimeRef.current?.callbacks.handleResult(frame.id, { ok: true, result: { allow } })
      return
    }
    if (payload.name === 'summary.final_summary' && options.failFinalCallback === true) {
      runtimeRef.current?.callbacks.handleResult(frame.id, {
        ok: false,
        error: { code: 'callback_failed', message: 'simulated summary callback failure' },
      })
      return
    }
    try {
      const receipt = simulator.submit(payload.name, payload.data)
      runtimeRef.current?.callbacks.handleResult(frame.id, { ok: true, result: receipt as unknown as import('../dist/protocol.js').JsonValue })
    } catch (error) {
      runtimeRef.current?.callbacks.handleResult(frame.id, {
        ok: false,
        error: { code: 'callback_failed', message: error instanceof Error ? error.message : 'summary simulator failed' },
      })
    }
  }
}

export interface ChatSubmissionResult {
  readonly content: string
  readonly favorability_delta: number
  readonly favorability_reason: string
}

export class ChatCallbackSimulator {
  content: string | undefined
  delta: number | undefined
  reason: string | undefined
  readonly submissions: Array<{ readonly name: string; readonly data: Record<string, unknown> }> = []

  private result(): ChatSubmissionResult | null {
    if (this.content === undefined || this.delta === undefined || this.reason === undefined) return null
    return {
      content: this.content,
      favorability_delta: this.delta,
      favorability_reason: this.reason,
    }
  }

  private missing(): string[] {
    const missing: string[] = []
    if (this.content === undefined) missing.push('final_response')
    if (this.delta === undefined || this.reason === undefined) missing.push('record_favorability_delta')
    return missing
  }

  submit(name: string, data: Record<string, unknown>): Record<string, unknown> {
    this.submissions.push({ name, data })
    const result = this.result()
    if (result !== null) {
      return { accepted: false, code: 'already_completed', missing: [], result }
    }
    if (name === 'chat.submit_final_response') {
      const content = data.content
      if (Object.keys(data).length !== 1 || typeof content !== 'string' || content.trim().length === 0 || content.trim().length > 2000) {
        return { accepted: false, code: 'invalid_content', missing: this.missing(), result: null }
      }
      this.content = content.trim()
      return { accepted: true, code: null, missing: this.missing(), result: this.result() }
    }
    const delta = data.delta
    const reason = data.reason
    if (
      Object.keys(data).length !== 2
      || typeof delta !== 'number'
      || !Number.isInteger(delta)
      || delta < -5
      || delta > 3
      || typeof reason !== 'string'
      || reason.trim().length === 0
      || reason.trim().length > 200
    ) {
      return { accepted: false, code: 'invalid_favorability', missing: this.missing(), result: null }
    }
    this.delta = delta
    this.reason = reason.trim()
    return { accepted: true, code: null, missing: this.missing(), result: this.result() }
  }
}

export interface ChatSendFrameOptions {
  readonly admission?: boolean | (() => boolean)
  readonly onCallback?: (name: string) => void
  readonly failFinalCallback?: boolean
}

export function chatSendFrame(
  runtimeRef: { current?: Awaited<ReturnType<typeof import('../dist/host.js').startHost>> },
  simulator: ChatCallbackSimulator,
  options: ChatSendFrameOptions = {},
): (frame: { kind: string; id: string; payload?: unknown }) => Promise<void> {
  return async (frame) => {
    if (frame.kind !== 'callback') return
    const payload = frame.payload as { name: string; data: Record<string, unknown> }
    options.onCallback?.(payload.name)
    if (payload.name === 'admission') {
      const allow = typeof options.admission === 'function' ? options.admission() : (options.admission ?? true)
      runtimeRef.current?.callbacks.handleResult(frame.id, { ok: true, result: { allow } })
      return
    }
    if (payload.name === 'chat.submit_final_response' && options.failFinalCallback === true) {
      runtimeRef.current?.callbacks.handleResult(frame.id, {
        ok: false,
        error: { code: 'callback_failed', message: 'simulated callback failure' },
      })
      return
    }
    const receipt = simulator.submit(payload.name, payload.data)
    runtimeRef.current?.callbacks.handleResult(frame.id, { ok: true, result: receipt as unknown as import('../dist/protocol.js').JsonValue })
  }
}
