/**
 * Dedicated TSK-203 chat preset: the two business submission tools, the
 * immutable safety boundary, and the preset-owned missing-requirement state.
 *
 * Tool schemas are declared only here. Python remains the trusted validator and
 * candidate owner; this module only forwards model arguments and reflects the
 * receipt needed for turn steering.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-agent-loop'
import type {} from '@deepseek-ai/dsh-settings'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { CHAT_SAFETY_BOUNDARY, decodeChatTaskData } from './chat-input.js'
import { registerChatQueryTools } from './chat-query-tools.js'
import { isPlainObject, type JsonValue } from './protocol.js'

export const name = 'komari-chat-preset'
export const inject = ['tools', 'systemPrompt', 'komariAgent']

const FINAL_RESPONSE = 'final_response'
const FAVORABILITY = 'record_favorability_delta'
const REQUIRED = [FINAL_RESPONSE, FAVORABILITY] as const

type Requirement = (typeof REQUIRED)[number]
type SubmissionCode = 'invalid_content' | 'invalid_favorability' | 'already_completed'

interface ChatSubmissionReceipt {
  readonly accepted: boolean
  readonly code: SubmissionCode | null
  readonly missing: readonly Requirement[]
  readonly result: {
    readonly content: string
    readonly favorability_delta: number
    readonly favorability_reason: string
  } | null
}

interface ChatPresetState {
  missing: readonly Requirement[]
  completed: boolean
  cleanupRegistered: boolean
  queryToolsRegistered: boolean
}

const states = new Map<string, ChatPresetState>()

function invalidReceipt(): never {
  throw new Error('invalid_submission_receipt')
}

function parseReceipt(value: JsonValue): ChatSubmissionReceipt {
  if (!isPlainObject(value)) invalidReceipt()
  const keys = Object.keys(value).sort()
  if (keys.length !== 4 || keys[0] !== 'accepted' || keys[1] !== 'code' || keys[2] !== 'missing' || keys[3] !== 'result') {
    invalidReceipt()
  }
  if (typeof value.accepted !== 'boolean') invalidReceipt()
  if (
    value.code !== null
    && value.code !== 'invalid_content'
    && value.code !== 'invalid_favorability'
    && value.code !== 'already_completed'
  ) invalidReceipt()
  if (!Array.isArray(value.missing)) invalidReceipt()
  const missing: Requirement[] = []
  for (const item of value.missing) {
    if (item !== FINAL_RESPONSE && item !== FAVORABILITY) invalidReceipt()
    if (missing.includes(item)) invalidReceipt()
    missing.push(item)
  }
  let result: ChatSubmissionReceipt['result'] = null
  if (value.result !== null) {
    if (!isPlainObject(value.result)) invalidReceipt()
    const resultKeys = Object.keys(value.result).sort()
    if (
      resultKeys.length !== 3
      || resultKeys[0] !== 'content'
      || resultKeys[1] !== 'favorability_delta'
      || resultKeys[2] !== 'favorability_reason'
    ) invalidReceipt()
    if (
      typeof value.result.content !== 'string'
      || typeof value.result.favorability_delta !== 'number'
      || !Number.isSafeInteger(value.result.favorability_delta)
      || typeof value.result.favorability_reason !== 'string'
    ) invalidReceipt()
    result = {
      content: value.result.content,
      favorability_delta: value.result.favorability_delta,
      favorability_reason: value.result.favorability_reason,
    }
  }
  if (value.accepted && value.code !== null) invalidReceipt()
  if (!value.accepted && value.code === null) invalidReceipt()
  return {
    accepted: value.accepted,
    code: value.code,
    missing,
    result,
  }
}

function ensureState(ctx: Context, agent: Agent): ChatPresetState {
  const id = String(agent.id)
  let state = states.get(id)
  if (state === undefined) {
    state = {
      missing: [...REQUIRED],
      completed: false,
      cleanupRegistered: false,
      queryToolsRegistered: false,
    }
    states.set(id, state)
  }
  if (!state.cleanupRegistered) {
    state.cleanupRegistered = true
    agent.ctx.effect(() => () => {
      states.delete(id)
    }, 'komari-chat-preset-state')
  }
  return state
}

function registerMissingRequirements(ctx: Context, agent: Agent, state: ChatPresetState): void {
  ctx.komariAgent.registerCompletionCheck(agent, () => state.missing)
}

/**
 * Read the frozen task snapshot through the trusted agent/session seam and
 * declare only the query capability subset this task froze. Registration is
 * idempotent across the creation event and later pre-step events.
 */
function registerFrozenQueryTools(ctx: Context, agent: Agent, state: ChatPresetState): void {
  if (state.queryToolsRegistered) return
  const rawTaskData = ctx.komariAgent.getTaskData(agent)
  if (rawTaskData === undefined) throw new Error('task_data_unavailable')
  const taskData = decodeChatTaskData(rawTaskData)
  registerChatQueryTools(ctx, agent, taskData.capabilities)
  state.queryToolsRegistered = true
}

function terminalCallbackFailure(ctx: Context, agent: Agent): void {
  try {
    ctx.komariAgent.failTask(agent, 'callback_failed')
  } catch {
    // The original callback failure is still thrown to the tool runtime.
  }
}

export function apply(ctx: Context): void {
  ctx.systemPrompt.section({
    name: 'komari-chat-security-boundary',
    order: 10,
    text: CHAT_SAFETY_BOUNDARY,
    interpolate: false,
  })

  // Register the frozen query-tool capabilities into this agent's scope before
  // its first request. This event is awaited before agent creation resolves,
  // which is earlier than the first assembly/pre-step.
  ctx.on('agent/created', (payload) => {
    try {
      const state = ensureState(ctx, payload.agent)
      registerFrozenQueryTools(ctx, payload.agent, state)
    } catch (error) {
      terminalCallbackFailure(ctx, payload.agent)
      throw error
    }
    return undefined
  })

  // Register before the first model step so a bare-text turn can also be
  // steered. Tool execution refreshes the same registration.
  ctx.on('agent/pre-step', async (payload, next) => {
    try {
      const state = ensureState(ctx, payload.agent)
      registerFrozenQueryTools(ctx, payload.agent, state)
      registerMissingRequirements(ctx, payload.agent, state)
    } catch {
      terminalCallbackFailure(ctx, payload.agent)
      return { kind: 'reject' }
    }
    return next()
  })

  ctx.tools.register(defineTool({
    name: FINAL_RESPONSE,
    description: 'Submit the final chat reply. Content must be 1-2000 characters and is not sent until the task also submits a favorability proposal.',
    parameters: {
      content: { type: 'string', required: true, description: 'Final reply text, 1-2000 characters.' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('chat tool requires an agent')
      const state = ensureState(ctx, exec.agent)
      try {
        registerMissingRequirements(ctx, exec.agent, state)
      } catch (error) {
        terminalCallbackFailure(ctx, exec.agent)
        throw error
      }
      const receipt = completeOrStage(ctx, exec.agent, state, await ctx.komariAgent.invokeBusinessCallback(
        exec.agent,
        'chat.submit_final_response',
        { ...args },
      ))
      if (receipt.result !== null) exec.concludeTurn()
      return receipt as unknown as JsonValue
    },
  }))

  ctx.tools.register(defineTool({
    name: FAVORABILITY,
    description: 'Record the chat reply favorability proposal. Delta must be an integer in the declared -5..3 range and within the task configured cap; reason must be 1-200 characters.',
    parameters: {
      delta: { type: 'integer', enum: [-5, -4, -3, -2, -1, 0, 1, 2, 3], required: true, description: 'Integer delta from -5 through 3, also bounded by the configured absolute cap.' },
      reason: { type: 'string', required: true, description: 'Nonempty reason, at most 200 characters.' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('chat tool requires an agent')
      const state = ensureState(ctx, exec.agent)
      try {
        registerMissingRequirements(ctx, exec.agent, state)
      } catch (error) {
        terminalCallbackFailure(ctx, exec.agent)
        throw error
      }
      const receipt = completeOrStage(ctx, exec.agent, state, await ctx.komariAgent.invokeBusinessCallback(
        exec.agent,
        'chat.submit_favorability',
        { ...args },
      ))
      if (receipt.result !== null) exec.concludeTurn()
      return receipt as unknown as JsonValue
    },
  }))
}

function completeOrStage(
  ctx: Context,
  agent: Agent,
  state: ChatPresetState,
  rawReceipt: JsonValue,
): ChatSubmissionReceipt {
  let receipt: ChatSubmissionReceipt
  try {
    receipt = parseReceipt(rawReceipt)
  } catch (error) {
    terminalCallbackFailure(ctx, agent)
    throw error
  }
  state.missing = [...receipt.missing]
  if (receipt.result !== null && !state.completed) {
    ctx.komariAgent.complete(agent, receipt.result)
    state.completed = true
  }
  return receipt
}

export { FINAL_RESPONSE, FAVORABILITY, REQUIRED }
