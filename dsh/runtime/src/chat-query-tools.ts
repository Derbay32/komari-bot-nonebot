/**
 * TSK-204 on-demand chat query tools.
 *
 * The four fixed tools are registered into one agent-local scope from the
 * task-owned capability snapshot. Python validates the business arguments and
 * performs the actual backend effect; this module owns the model-visible
 * schema, narrow callback names, and bounded untrusted-data rendering.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { encodeUntrustedData, type ChatQueryCapabilities } from './chat-input.js'
import { isPlainObject, type JsonValue } from './protocol.js'

export const RECALL_MEMORY_TOOL = 'recall_memory'
export const SEARCH_KNOWLEDGE_TOOL = 'search_knowledge'
export const SEARCH_WEB_TOOL = 'search_web'
export const FETCH_PAGE_TOOL = 'fetch_page'

export const CHAT_QUERY_TOOL_RESULT_LIMIT = 8_000

const RECALL_MEMORY_CALLBACK = 'chat.recall_memory'
const SEARCH_KNOWLEDGE_CALLBACK = 'chat.search_knowledge'
const SEARCH_WEB_CALLBACK = 'chat.search_web'
const FETCH_PAGE_CALLBACK = 'chat.fetch_page'

const INVALID_ARGUMENTS_MESSAGE = 'The query arguments were rejected. Correct them and retry within the task budget.'
const DISABLED_CAPABILITY_MESSAGE = 'This query capability is unavailable for the current task.'
const INVALID_CALLBACK_RESULT_MESSAGE = 'The query callback returned an invalid result.'
const MAX_RESULT_ITEMS = 32
const MAX_SOURCE_ID_CHARS = 256

export type ChatQuerySourceType = 'memory' | 'knowledge' | 'web'

export interface ChatQueryResultItem {
  readonly source_id: string
  readonly content: string
}

export interface ChatQueryResult {
  readonly source_type: ChatQuerySourceType
  readonly items: readonly ChatQueryResultItem[]
  readonly truncated: boolean
  readonly omitted_items: number
}

export type ChatQueryCallbackResult =
  | { readonly ok: true; readonly result: ChatQueryResult }
  | { readonly ok: false; readonly code: 'invalid_arguments' | 'capability_disabled' }

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

function decodeItem(value: unknown): ChatQueryResultItem {
  if (!isPlainObject(value) || !exactKeys(value, ['source_id', 'content'])) throw new Error('invalid_query_result')
  if (
    typeof value.source_id !== 'string'
    || value.source_id.length === 0
    || value.source_id.length > MAX_SOURCE_ID_CHARS
    || typeof value.content !== 'string'
  ) throw new Error('invalid_query_result')
  return { source_id: value.source_id, content: value.content }
}

export function decodeChatQueryResult(value: unknown): ChatQueryResult {
  if (!isPlainObject(value) || !exactKeys(value, ['source_type', 'items', 'truncated', 'omitted_items'])) {
    throw new Error('invalid_query_result')
  }
  if (value.source_type !== 'memory' && value.source_type !== 'knowledge' && value.source_type !== 'web') {
    throw new Error('invalid_query_result')
  }
  if (!Array.isArray(value.items) || value.items.length > MAX_RESULT_ITEMS) throw new Error('invalid_query_result')
  if (typeof value.truncated !== 'boolean') throw new Error('invalid_query_result')
  if (
    typeof value.omitted_items !== 'number'
    || !Number.isSafeInteger(value.omitted_items)
    || value.omitted_items < 0
  ) throw new Error('invalid_query_result')
  return {
    source_type: value.source_type,
    items: value.items.map(decodeItem),
    truncated: value.truncated,
    omitted_items: value.omitted_items,
  }
}

export function decodeChatQueryCallbackResult(value: JsonValue): ChatQueryCallbackResult {
  if (!isPlainObject(value)) throw new Error('invalid_query_callback')
  if (value.ok === true) {
    if (!exactKeys(value, ['ok', 'result'])) throw new Error('invalid_query_callback')
    return { ok: true, result: decodeChatQueryResult(value.result) }
  }
  if (value.ok === false) {
    if (!exactKeys(value, ['ok', 'code'])) throw new Error('invalid_query_callback')
    if (value.code !== 'invalid_arguments' && value.code !== 'capability_disabled') {
      throw new Error('invalid_query_callback')
    }
    return { ok: false, code: value.code }
  }
  throw new Error('invalid_query_callback')
}

interface QueryEnvelope {
  readonly source_type: ChatQuerySourceType
  readonly items: readonly ChatQueryResultItem[]
  readonly truncated: boolean
  readonly omitted_items: number
}

function renderEnvelope(toolName: string, data: QueryEnvelope): string {
  return [
    `<komari_untrusted_tool_result tool="${toolName}" source_type="${data.source_type}" trust_level="untrusted">`,
    encodeUntrustedData({
      source_type: data.source_type,
      items: data.items,
      truncated: data.truncated,
      omitted_items: data.omitted_items,
    }),
    '</komari_untrusted_tool_result>',
  ].join('\n')
}

/**
 * Render one bounded untrusted query result. Dropped items and clipped content
 * are represented by the explicit `truncated` and `omitted_items` data fields;
 * backend text is never interpolated outside the JSON data block.
 */
export function renderChatQueryToolResult(toolName: string, result: ChatQueryResult): string {
  let items = result.items.map((item) => ({ ...item }))
  let truncated = result.truncated
  let omittedItems = result.omitted_items
  let rendered = renderEnvelope(toolName, {
    source_type: result.source_type,
    items,
    truncated,
    omitted_items: omittedItems,
  })
  while (rendered.length > CHAT_QUERY_TOOL_RESULT_LIMIT && items.length > 1) {
    items = items.slice(0, -1)
    omittedItems += 1
    truncated = true
    rendered = renderEnvelope(toolName, {
      source_type: result.source_type,
      items,
      truncated,
      omitted_items: omittedItems,
    })
  }
  if (rendered.length > CHAT_QUERY_TOOL_RESULT_LIMIT && items.length === 1) {
    const points = [...items[0]!.content]
    let lower = 0
    let upper = points.length
    let best: string | undefined
    while (lower <= upper) {
      const middle = Math.floor((lower + upper) / 2)
      const candidate = {
        source_id: items[0]!.source_id,
        content: points.slice(0, middle).join(''),
      }
      const candidateText = renderEnvelope(toolName, {
        source_type: result.source_type,
        items: [candidate],
        truncated: true,
        omitted_items: omittedItems,
      })
      if (candidateText.length <= CHAT_QUERY_TOOL_RESULT_LIMIT) {
        best = candidate.content
        lower = middle + 1
      } else {
        upper = middle - 1
      }
    }
    if (best === undefined) {
      items = []
      omittedItems += 1
    } else {
      items = [{ source_id: items[0]!.source_id, content: best }]
    }
    truncated = true
    rendered = renderEnvelope(toolName, {
      source_type: result.source_type,
      items,
      truncated,
      omitted_items: omittedItems,
    })
  }
  return rendered
}

function closedTool(definition: ToolDefinition, queryMaxLength?: number): ToolDefinition {
  const baseProperties = (definition.parameters.properties ?? {}) as Record<string, Record<string, unknown>>
  const properties: Record<string, unknown> = queryMaxLength === undefined
    ? baseProperties
    : {
        ...baseProperties,
        query: {
          ...(baseProperties.query ?? {}),
          minLength: 1,
          maxLength: queryMaxLength,
        },
      }
  return Object.freeze({
    ...definition,
    parameters: Object.freeze({
      ...definition.parameters,
      properties: Object.freeze(properties),
      additionalProperties: false,
    }),
  })
}

function terminalCallbackFailure(ctx: Context, agent: Agent): void {
  try {
    ctx.komariAgent.failTask(agent, 'callback_failed')
  } catch {
    // The original failure remains the surfaced tool result.
  }
}

function exactArgumentKeys(args: unknown, expected: readonly string[]): void {
  if (!isPlainObject(args) || !exactKeys(args, expected)) throw new Error(INVALID_ARGUMENTS_MESSAGE)
}

async function executeQueryCallback(
  ctx: Context,
  agent: Agent,
  name: string,
  data: { readonly [key: string]: JsonValue },
  expectedKeys: readonly string[],
): Promise<ChatQueryResult> {
  exactArgumentKeys(data, expectedKeys)
  const raw = await ctx.komariAgent.invokeBusinessCallback(agent, name, data)
  let parsed: ChatQueryCallbackResult
  try {
    parsed = decodeChatQueryCallbackResult(raw)
  } catch {
    terminalCallbackFailure(ctx, agent)
    throw new Error(INVALID_CALLBACK_RESULT_MESSAGE)
  }
  if (!parsed.ok) {
    if (parsed.code === 'invalid_arguments') throw new Error(INVALID_ARGUMENTS_MESSAGE)
    throw new Error(DISABLED_CAPABILITY_MESSAGE)
  }
  return parsed.result
}

function queryTool(
  ctx: Context,
  name: string,
  callbackName: string,
  description: string,
  parameterName: 'query',
): ToolDefinition {
  return closedTool(defineTool({
    name,
    description,
    parameters: {
      query: { type: 'string', required: true, description: 'Natural-language query text.' },
    },
    output: {
      schema: { type: 'json' },
      render: (args, value) => {
        void args
        return [{
          type: 'text',
          text: renderChatQueryToolResult(name, decodeChatQueryResult(value)),
        } satisfies ContentBlock]
      },
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('query tool requires an agent')
      exactArgumentKeys(args, [parameterName])
      return await executeQueryCallback(
        ctx,
        exec.agent,
        callbackName,
        { [parameterName]: args.query },
        [parameterName],
      ) as unknown as JsonValue
    },
  }), name === SEARCH_WEB_TOOL ? 200 : undefined)
}

function fetchTool(ctx: Context): ToolDefinition {
  return closedTool(defineTool({
    name: FETCH_PAGE_TOOL,
    description: 'Fetch the readable body of 1-5 explicit HTTP(S) web pages. Pass only pages that need full reading.',
    parameters: {
      urls: {
        type: 'array',
        items: { type: 'string' },
        required: true,
        description: 'One to five nonempty HTTP(S) URLs; no non-string entries or extra fields.',
      },
    },
    output: {
      schema: { type: 'json' },
      render: (args, value) => {
        void args
        return [{
          type: 'text',
          text: renderChatQueryToolResult(FETCH_PAGE_TOOL, decodeChatQueryResult(value)),
        } satisfies ContentBlock]
      },
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('query tool requires an agent')
      exactArgumentKeys(args, ['urls'])
      return await executeQueryCallback(
        ctx,
        exec.agent,
        FETCH_PAGE_CALLBACK,
        { urls: [...args.urls] },
        ['urls'],
      ) as unknown as JsonValue
    },
  }))
}

/**
 * Register the frozen capability subset into the exact agent scope. The caller
 * owns idempotence; each call returns after the scoped registrations are owned
 * by the agent context and therefore disposed with that task.
 */
export function registerChatQueryTools(
  ctx: Context,
  agent: Agent,
  capabilities: ChatQueryCapabilities,
): void {
  const definitions: ToolDefinition[] = [queryTool(
    ctx,
    RECALL_MEMORY_TOOL,
    RECALL_MEMORY_CALLBACK,
    'Recall long-term memory on demand. Supply only a natural-language query; never supply a bank, address, credential, or identity field.',
    'query',
  )]
  if (capabilities.knowledge) {
    definitions.push(queryTool(
      ctx,
      SEARCH_KNOWLEDGE_TOOL,
      SEARCH_KNOWLEDGE_CALLBACK,
      'Search the configured knowledge base with the original conversational query. The result limit is fixed by the task.',
      'query',
    ))
  }
  if (capabilities.search) {
    definitions.push(queryTool(
      ctx,
      SEARCH_WEB_TOOL,
      SEARCH_WEB_CALLBACK,
      'Search the public web. Supply one concise query of at most 200 characters; no provider or credential fields are accepted.',
      'query',
    ))
  }
  if (capabilities.fetch) {
    definitions.push(fetchTool(ctx))
  }

  agent.ctx.effect(() => {
    const disposers: Array<() => void> = []
    try {
      for (const definition of definitions) disposers.push(agent.ctx.tools.register(definition))
    } catch (error) {
      for (const dispose of disposers.reverse()) dispose()
      throw error
    }
    return () => {
      for (const dispose of disposers.reverse()) dispose()
    }
  }, 'komari-chat-query-tools')
}
