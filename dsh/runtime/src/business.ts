/**
 * The restricted project plugin mounted at the root of the private DSH
 * composition. It owns the live task registry, task admission, the internal
 * completion slot, and the mutation-vs-task-admission gate.
 *
 * It deliberately registers no model-visible tools. TSK-203/TSK-206 own the
 * business tool declarations; TSK-201 supplies this runtime foundation.
 */
import { randomUUID } from 'node:crypto'
import { Context, Service, type Volatile } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
import { isAttachmentError } from '@deepseek-ai/dsh-attachment'
import z from '@deepseek-ai/schemastery'
import type { Agent, AgentHandle } from '@deepseek-ai/dsh-agent'
import type { GenerateOptions, LlmResolvedModelInfo, RetryPolicyConfig, StreamChunk } from '@deepseek-ai/dsh-llm'
import { createUserMessage, LlmError, ReasoningEffortId } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { PERSONA_PREFIX_SECTION } from '@deepseek-ai/dsh-system-prompt'
import { AttachmentPreparationError, type TaskAttachmentSession, type TaskAttachmentStore } from './attachments.js'
import { decodeChatTaskData, InvalidChatImageDescriptor, renderChatTaskInput, type ChatTaskData } from './chat-input.js'
import { TaskMutationGate, TaskMutationConflict } from './gate.js'
import {
  assertCallbackName,
  assertJsonValue,
  deepFreezeJson,
  isPlainObject,
  MAX_CALLBACK_DATA_BYTES,
  MAX_PROMPT_CHARS,
  MAX_RESULT_BYTES,
  type AdmissionPurpose,
  type CallbackPayload,
  type JsonValue,
  type TaskBudget,
  type TaskKind,
  type TaskRequestPayload,
} from './protocol.js'
import { SharedTaskPolicy, type TaskPolicyFailure } from './task-policy.js'

const FIXED_RETRY_CODES = ['RATE_LIMIT', 'TIMEOUT', 'TRANSPORT'] as const

/** The only provider retry policy this runtime accepts for project routes. */
export const FIXED_RETRY_POLICY: RetryPolicyConfig = Object.freeze({
  mode: 'normal' as const,
  maxRetries: 2,
  retryableCodes: [...FIXED_RETRY_CODES],
})
const HANDLE_DISPOSE_TIMEOUT_MS = 10_000

export interface AgentBinding {
  readonly provider?: string
  readonly model?: string
  readonly reasoningEffort?: string
  readonly prompt?: string
}

export interface Config {
  readonly chat: Volatile<AgentBinding>
  readonly summary: Volatile<AgentBinding>
}

const BindingSchema = z.object({
  provider: z.string(),
  model: z.string(),
  reasoningEffort: z.string(),
  prompt: z.string(),
})

const AgentConfig: z<Config> = z.object({
  chat: BindingSchema.default({}).volatile(),
  summary: BindingSchema.default({}).volatile(),
})

export const Config = AgentConfig

export interface CallbackTransport {
  requestCallback(payload: CallbackPayload, signal: AbortSignal): Promise<JsonValue>
}

export class TaskFailure extends Error {
  readonly code: string
  readonly failureCode: string | undefined

  constructor(code: string, failureCode?: string) {
    super(code)
    this.name = 'TaskFailure'
    this.code = code
    this.failureCode = failureCode
  }
}

interface LiveTask {
  readonly requestId: string
  readonly taskId: string
  readonly kind: TaskKind
  readonly budget: TaskBudget
  readonly sessionId: SessionId
  readonly presetId: TaskKind
  readonly provider: string
  readonly model: string
  readonly reasoningEffort: ReasoningEffortId | undefined
  readonly prompt: string
  readonly controller: AbortController
  imageSession: TaskAttachmentSession | undefined
  imageModelFailure: boolean
  imageInputFailure: boolean
  readonly admissionAttempts: Map<AdmissionPurpose, number>
  cancelled: boolean
  result: { readonly [key: string]: JsonValue } | undefined
  failureCode: string | undefined
  terminalFailure: TaskPolicyFailure | undefined
  policy: SharedTaskPolicy | undefined
  handle: AgentHandle | undefined
}

function timeoutSentinel<T>(promise: Promise<T>, ms: number): Promise<T | typeof TIMEOUT> {
  return Promise.race([
    promise,
    new Promise<typeof TIMEOUT>((resolve) => {
      const timer = setTimeout(() => resolve(TIMEOUT), ms)
      timer.unref()
    }),
  ])
}

const TIMEOUT = Symbol('timeout')

function hasAttachmentCause(error: unknown): boolean {
  let current: unknown = error
  for (let depth = 0; depth < 8; depth += 1) {
    if (isAttachmentError(current)) return true
    if (!(current instanceof Error)) return false
    current = current.cause
  }
  return false
}

function hasFixedRetryPolicy(policy: { mode: string; maxRetries?: number; retryableCodes?: readonly string[] }): boolean {
  if (policy.mode !== 'normal' || policy.maxRetries !== 2) return false
  const actual = [...(policy.retryableCodes ?? [])].sort()
  const expected = [...FIXED_RETRY_CODES].sort()
  return actual.length === expected.length && actual.every((code, index) => code === expected[index])
}

/**
 * Root service exposed as `ctx.komariAgent` inside this process only.
 */
export default class KomariAgentRuntime extends Service {
  static inject = ['agents', 'agentPresets', 'llm', 'sessions']
  static Config = AgentConfig

  readonly gate = new TaskMutationGate()
  private readonly liveBySession = new Map<SessionId, LiveTask>()
  private readonly liveByRequest = new Map<string, LiveTask>()
  private transport: CallbackTransport | undefined
  private releaseFailed = false

  constructor(
    ctx: Context,
    private readonly config: Config,
  ) {
    super(ctx, 'komariAgent')
    ctx.on('llm/stream', (options, next) => this.admittedStream(options, next))
    ctx.on('agent/error', (payload) => {
      const task = this.liveBySession.get(payload.agent.id)
      if (task === undefined || task.cancelled) return
      if (!(payload.error instanceof LlmError)) {
        task.failureCode = 'UNKNOWN'
        if (task.imageSession !== undefined && isAttachmentError(payload.error)) {
          task.imageInputFailure = true
        }
        return
      }
      task.failureCode = payload.error.code
      if (
        task.imageSession !== undefined
        && (
          hasAttachmentCause(payload.error)
          || payload.error.code === 'IMAGE_INPUT_FAILED'
          || payload.error.code.startsWith('ATTACHMENT_')
        )
      ) {
        task.imageInputFailure = true
        return
      }
      // Provider admission/cancellation are owned by the task boundary, not
      // by model-image processing. Only a terminal model request failure on an
      // image-bearing task is classified as image_model_failed.
      if (
        task.imageSession !== undefined
        && payload.error.code !== 'ADMISSION_DENIED'
        && payload.error.code !== 'ABORTED'
        && payload.error.code !== 'IMAGE_OFFLOAD_REQUIRED'
        && payload.error.code !== 'CONTEXT_WINDOW_EXCEEDED'
      ) {
        task.imageModelFailure = true
      }
    })
    ctx.on('session/event', (session, event) => {
      const task = this.liveBySession.get(session.id)
      task?.policy?.observeSessionEvent(event)
    })
    ctx.effect(
      () => () => {
        for (const task of this.liveByRequest.values()) this.cancelTaskObject(task)
      },
      'komari-agent.cancel-remaining-tasks',
    )
  }

  attachCallbackTransport(transport: CallbackTransport): void {
    if (this.transport !== undefined) throw new Error('callback transport already attached')
    this.transport = transport
  }

  get activeTaskCount(): number {
    return this.gate.activeTaskCount
  }

  /** Whether each task kind currently has a complete, frozen binding. */
  bindingStatus(): Readonly<Record<TaskKind, boolean>> {
    return Object.freeze({
      chat: this.bindingConfigured('chat'),
      summary: this.bindingConfigured('summary'),
    })
  }

  listLiveTaskIds(): readonly string[] {
    return [...this.liveByRequest.keys()]
  }

  /**
   * Hold every provider-route or credential mutation behind the task-admission
   * boundary. The apply callback owns the official DSH write and its effective
   * verification; this gate only enforces the idle/no-save boundary TSK-202 needs.
   */
  async runConnectionMutation<T>(apply: () => Promise<T>): Promise<T> {
    return this.gate.runExclusiveMutation(apply)
  }

  /**
   * Fill the task's structured business-result slot. This is the narrow
   * internal capability later business tools call after their own validation.
   */
  complete(agent: Agent, result: { readonly [key: string]: JsonValue }): void {
    const task = this.taskForAgent(agent)
    if (task === undefined) throw new TaskFailure('unknown_task')
    if (task.cancelled) throw new TaskFailure('cancelled')
    if (task.result !== undefined) throw new TaskFailure('result_already_completed')
    if (this.taskIsTerminal(task)) throw new TaskFailure('task_terminal')
    if (!isPlainObject(result)) throw new TaskFailure('invalid_result')
    assertJsonValue(result)
    const serialized = JSON.stringify(result)
    if (Buffer.byteLength(serialized, 'utf8') > MAX_RESULT_BYTES) throw new TaskFailure('result_too_large')
    task.result = deepFreezeJson(result)
    task.policy?.markCompleted()
  }

  /**
   * Register the preset-owned missing-requirement names for turn-stopping
   * steering. The policy stores only the callback, never business contents.
   */
  registerCompletionCheck(agent: Agent, checker: () => readonly string[]): void {
    const task = this.taskForAgent(agent)
    if (task === undefined) throw new TaskFailure('unknown_task')
    if (task.policy === undefined) throw new TaskFailure('task_policy_missing')
    task.policy.registerMissingRequirements(checker)
  }

  /** Mark a malformed or failed callback path terminal from trusted preset code. */
  failTask(agent: Agent, code: TaskPolicyFailure): void {
    const task = this.taskForAgent(agent)
    if (task === undefined) throw new TaskFailure('unknown_task')
    this.markTerminal(task, code)
  }

  /**
   * Invoke one frozen Python business callback for a live trusted task. The
   * callback name and payload are supplied by project tool code, never by
   * arbitrary protocol input.
   */
  async invokeBusinessCallback(agent: Agent, name: string, data: { readonly [key: string]: JsonValue }): Promise<JsonValue> {
    const task = this.taskForAgent(agent)
    if (task === undefined) throw new TaskFailure('unknown_task')
    if (task.cancelled) throw new TaskFailure('cancelled')
    assertCallbackName(name)
    if (!isPlainObject(data)) throw new TaskFailure('invalid_callback_data')
    assertJsonValue(data)
    if (Buffer.byteLength(JSON.stringify(data), 'utf8') > MAX_CALLBACK_DATA_BYTES) {
      throw new TaskFailure('callback_data_too_large')
    }
    try {
      return await this.invokeCallback(task, name, data)
    } catch (error) {
      if (error instanceof TaskFailure && error.code === 'callback_failed') {
        this.markTerminal(task, 'callback_failed')
      }
      throw error
    }
  }

  cancelTask(requestId: string): boolean {
    const task = this.liveByRequest.get(requestId)
    if (task === undefined) return false
    this.cancelTaskObject(task)
    return true
  }

  cancelAll(): void {
    for (const task of this.liveByRequest.values()) this.cancelTaskObject(task)
  }

  async runTask(requestId: string, request: TaskRequestPayload): Promise<{ readonly [key: string]: JsonValue }> {
    if (this.releaseFailed) throw new TaskFailure('host_closed')
    const lease = await this.gate.enterTask()
    let task: LiveTask | undefined
    try {
      const chatData: ChatTaskData | undefined = request.kind === 'chat'
        ? this.decodeChatTask(request)
        : undefined
      const renderedTaskInput = chatData === undefined
        ? this.renderTaskInput(request)
        : renderChatTaskInput(chatData)
      const live: LiveTask = await this.createLiveTask(requestId, request)
      task = live
      this.liveBySession.set(live.sessionId, live)
      this.liveByRequest.set(live.requestId, live)

      const handle = await this.ctx.agents.create({
        sessionId: live.sessionId,
        meta: { agentPreset: live.presetId },
        agentOptions: {
          provider: live.provider,
          model: live.model,
          ...(live.reasoningEffort === undefined ? {} : { reasoningEffort: live.reasoningEffort }),
        },
        setup: async (agentCtx) => {
          await this.ctx.agentPresets.mount(agentCtx, live.presetId)
          agentCtx.systemPrompt.section({
            name: PERSONA_PREFIX_SECTION,
            order: 0,
            text: live.prompt,
            interpolate: false,
          })
        },
      })
      live.handle = handle
      const policy = new SharedTaskPolicy({
        budget: live.budget,
        isComplete: () => live.result !== undefined,
        isTerminal: () => this.taskIsTerminal(live),
        recordTerminalFailure: (code) => this.markTerminal(live, code),
      })
      policy.attach(handle.agent.ctx)
      live.policy = policy
      if (live.cancelled) {
        handle.agent.cancel({ kind: 'user' })
        throw new TaskFailure('cancelled')
      }

      // Trusted preparation at the existing initial-input boundary. The
      // text-only path above still creates exactly one partitioned message.
      let imageBlocks: readonly import('@deepseek-ai/dsh-llm').ImageBlock[] = []
      if (chatData !== undefined && chatData.images.length > 0) {
        const attachments = this.ctx.get('attachments') as TaskAttachmentStore
        const imageSession = attachments.createSession()
        live.imageSession = imageSession
        try {
          imageBlocks = await imageSession.prepare(
            chatData.images,
            (imageIndex: number, offset: number, signal: AbortSignal) => this.readImageChunk(live, imageIndex, offset, signal),
            live.controller.signal,
          )
        } catch (error) {
          if (error instanceof AttachmentPreparationError) {
            throw new TaskFailure(error.code)
          }
          throw error
        }
      }
      if (live.cancelled) throw new TaskFailure('cancelled')
      handle.agent.followup(createUserMessage({
        content: [{ type: 'text', text: renderedTaskInput }, ...imageBlocks],
        source: { kind: 'user' },
      }))
      // A live task is bounded by its model/tool budgets, not the shutdown
      // drain window. Cancellation and process shutdown own their deadlines.
      await handle.agent.whenIdle()
      if (live.cancelled) throw new TaskFailure('cancelled')
      // An already-recorded terminal failure always wins over a late result.
      if (live.terminalFailure !== undefined) throw new TaskFailure(live.terminalFailure)
      if (live.failureCode !== undefined) {
        if (live.imageInputFailure) throw new TaskFailure('image_input_failed')
        if (live.imageModelFailure) throw new TaskFailure('image_model_failed')
        throw new TaskFailure('agent_error', live.failureCode)
      }
      if (live.result !== undefined) return live.result
      throw new TaskFailure('incomplete_result')
    } catch (error) {
      throw this.translateFailure(error)
    } finally {
      let releaseFailure: TaskFailure | undefined
      try {
        if (task !== undefined) releaseFailure = await this.disposeTask(task)
      } finally {
        lease.release()
      }
      if (releaseFailure !== undefined) {
        this.releaseFailed = true
        this.cancelAll()
        // Cleanup failure takes precedence: the process must be retired,
        // including when generation had already failed or been cancelled.
        throw releaseFailure
      }
    }
  }

  private async createLiveTask(requestId: string, request: TaskRequestPayload): Promise<LiveTask> {
    const binding = this.resolveBinding(request.kind)
    const prompt = binding.prompt ?? ''
    if (prompt.trim().length === 0) throw new TaskFailure('binding_unconfigured')
    if (prompt.length > MAX_PROMPT_CHARS) throw new TaskFailure('invalid_binding')
    const provider = (binding.provider ?? '').trim()
    const model = (binding.model ?? '').trim()
    if (provider.length === 0 || model.length === 0) throw new TaskFailure('binding_unconfigured')
    if (provider.length > 256 || model.length > 256) throw new TaskFailure('invalid_binding')

    let policy
    try {
      policy = this.ctx.llm.providerRetryPolicy(provider)
    } catch (error) {
      if (error instanceof LlmError) throw new TaskFailure('provider_unavailable', error.code)
      throw new TaskFailure('provider_unavailable')
    }
    if (!hasFixedRetryPolicy(policy)) throw new TaskFailure('retry_policy_invalid')

    let modelInfo: LlmResolvedModelInfo
    try {
      modelInfo = await this.ctx.llm.resolveModelInfo(provider, model)
    } catch (error) {
      if (error instanceof LlmError) throw new TaskFailure('provider_unavailable', error.code)
      throw new TaskFailure('provider_unavailable')
    }
    const requestedEffort = binding.reasoningEffort?.trim() ?? ''
    let reasoningEffort: ReasoningEffortId | undefined
    if (requestedEffort.length > 0) {
      if (requestedEffort.length > 32) throw new TaskFailure('invalid_binding')
      const supported = modelInfo.reasoning?.efforts.some((effort) => String(effort.id) === requestedEffort) ?? false
      if (!supported) throw new TaskFailure('invalid_binding')
      reasoningEffort = ReasoningEffortId(requestedEffort)
    }

    return {
      requestId,
      taskId: request.task_id,
      kind: request.kind,
      budget: request.budget,
      sessionId: SessionId(randomUUID()),
      presetId: request.kind,
      provider,
      model,
      reasoningEffort,
      prompt,
      controller: new AbortController(),
      imageSession: undefined,
      imageModelFailure: false,
      imageInputFailure: false,
      admissionAttempts: new Map(),
      cancelled: false,
      result: undefined,
      failureCode: undefined,
      terminalFailure: undefined,
      policy: undefined,
      handle: undefined,
    }
  }

  private resolveBinding(kind: TaskKind): AgentBinding {
    return kind === 'chat' ? this.config.chat.get() : this.config.summary.get()
  }

  private bindingConfigured(kind: TaskKind): boolean {
    const binding = this.resolveBinding(kind)
    return (binding.provider?.trim().length ?? 0) > 0
      && (binding.model?.trim().length ?? 0) > 0
      && (binding.prompt?.trim().length ?? 0) > 0
  }

  private decodeChatTask(request: TaskRequestPayload): ChatTaskData {
    try {
      return decodeChatTaskData(request.task_data)
    } catch (error) {
      if (error instanceof InvalidChatImageDescriptor) throw new TaskFailure('invalid_image_input')
      throw new TaskFailure('invalid_task_data')
    }
  }

  private async readImageChunk(
    task: LiveTask,
    imageIndex: number,
    offset: number,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    if (task.cancelled || signal.aborted) throw new TaskFailure('cancelled')
    try {
      const result = await this.invokeCallback(task, 'chat.read_image_chunk', {
        image_index: imageIndex,
        offset,
      })
      if (task.cancelled || signal.aborted) throw new TaskFailure('cancelled')
      return result
    } catch (error) {
      if (task.cancelled || signal.aborted) throw new TaskFailure('cancelled')
      if (error instanceof TaskFailure && error.failureCode === 'admission_denied') {
        throw new AttachmentPreparationError('admission_denied')
      }
      if (error instanceof TaskFailure && error.failureCode === 'invalid_image_input') {
        throw new AttachmentPreparationError('invalid_image_input')
      }
      throw error
    }
  }

  private renderTaskInput(request: TaskRequestPayload): string {
    if (request.kind === 'chat') {
      try {
        return renderChatTaskInput(decodeChatTaskData(request.task_data))
      } catch {
        throw new TaskFailure('invalid_task_data')
      }
    }
    return JSON.stringify({
      task_kind: request.kind,
      task_data: request.task_data,
    })
  }

  private taskIsTerminal(task: LiveTask): boolean {
    return task.cancelled || task.terminalFailure !== undefined || task.failureCode !== undefined
  }

  private markTerminal(task: LiveTask, code: TaskPolicyFailure): void {
    if (task.terminalFailure !== undefined) return
    task.terminalFailure = code
    try {
      task.handle?.agent.cancel({ kind: 'user' })
    } catch {
      // The failure code still owns the terminal outcome.
    }
  }

  private admittedStream(options: GenerateOptions, next: () => AsyncIterable<StreamChunk>): AsyncIterable<StreamChunk> {
    const sessionId = options.sessionId
    const task = sessionId === undefined ? undefined : this.liveBySession.get(sessionId)
    if (task === undefined) {
      throw new LlmError('task admission denied: no live task owns this session', 'ADMISSION_DENIED')
    }
    if (task.cancelled || task.controller.signal.aborted) {
      throw new LlmError('task admission denied: task is cancelled', 'ABORTED')
    }
    if (task.result !== undefined || task.terminalFailure !== undefined || task.failureCode !== undefined) {
      throw new LlmError('task admission denied: task is not active', 'ADMISSION_DENIED')
    }
    const purpose: AdmissionPurpose = options.purpose === 'compaction'
      ? 'compaction'
      : options.purpose === 'session-title'
        ? 'session-title'
        : 'conversation'
    const attempt = (task.admissionAttempts.get(purpose) ?? 0) + 1
    task.admissionAttempts.set(purpose, attempt)
    return this.admitThenStream(task, purpose, attempt, options, next)
  }

  private async *admitThenStream(
    task: LiveTask,
    purpose: AdmissionPurpose,
    attempt: number,
    options: GenerateOptions,
    next: () => AsyncIterable<StreamChunk>,
  ): AsyncIterable<StreamChunk> {
    await this.requestAdmission(task, purpose, attempt, options.provider, options.model)
    if (task.cancelled || task.controller.signal.aborted) {
      throw new LlmError('task admission denied: task is cancelled', 'ABORTED')
    }
    yield* next()
  }

  private async requestAdmission(task: LiveTask, purpose: AdmissionPurpose, attempt: number, provider: string, model: string): Promise<void> {
    let decision: JsonValue
    try {
      decision = await this.invokeCallback(task, 'admission', {
        task_id: task.taskId,
        kind: task.kind,
        purpose,
        provider,
        model,
        attempt,
      })
    } catch (error) {
      if (error instanceof TaskFailure && error.code === 'cancelled') {
        throw new LlmError('task admission denied: task is cancelled', 'ABORTED')
      }
      if (error instanceof TaskFailure && error.code === 'callback_failed') {
        this.markTerminal(task, 'callback_failed')
      }
      throw new LlmError('task admission denied', 'ADMISSION_DENIED')
    }
    if (!isPlainObject(decision) || Object.keys(decision).length !== 1 || typeof decision.allow !== 'boolean' || decision.allow !== true) {
      throw new LlmError('task admission denied', 'ADMISSION_DENIED')
    }
  }

  private async invokeCallback(task: LiveTask, name: string, data: { readonly [key: string]: JsonValue }): Promise<JsonValue> {
    const transport = this.transport
    if (transport === undefined) throw new TaskFailure('internal_error')
    try {
      return await transport.requestCallback(
        {
          request_id: task.requestId,
          task_id: task.taskId,
          name,
          data,
        },
        task.controller.signal,
      )
    } catch (error) {
      if (task.cancelled || task.controller.signal.aborted) throw new TaskFailure('cancelled')
      if (error instanceof TaskFailure) throw error
      throw new TaskFailure('callback_failed')
    }
  }

  private taskForAgent(agent: Agent): LiveTask | undefined {
    return this.liveBySession.get(agent.id)
  }

  private cancelTaskObject(task: LiveTask): void {
    if (task.cancelled) return
    task.cancelled = true
    task.controller.abort('task cancelled')
    try {
      task.handle?.agent.cancel({ kind: 'user' })
    } catch {
      // Cancellation is best-effort; disposal below still owns the release.
    }
  }

  private async disposeTask(task: LiveTask): Promise<TaskFailure | undefined> {
    task.controller.abort('task finished')
    let failure: TaskFailure | undefined
    try {
      if (task.handle !== undefined) {
        const disposed = await timeoutSentinel(task.handle.dispose(), HANDLE_DISPOSE_TIMEOUT_MS)
        if (disposed === TIMEOUT) failure = new TaskFailure('release_failed')
      }
      if (this.ctx.agents.get(task.sessionId) !== undefined || this.ctx.sessions.get(task.sessionId) !== undefined) {
        failure = new TaskFailure('release_failed')
      }
    } catch {
      failure = new TaskFailure('release_failed')
    } finally {
      try {
        if (task.imageSession !== undefined) {
          const released = await timeoutSentinel(task.imageSession.release(), HANDLE_DISPOSE_TIMEOUT_MS)
          if (released === TIMEOUT) failure = new TaskFailure('release_failed')
        }
      } catch {
        failure = new TaskFailure('release_failed')
      } finally {
        this.liveBySession.delete(task.sessionId)
        this.liveByRequest.delete(task.requestId)
      }
    }
    return failure
  }

  private translateFailure(error: unknown): TaskFailure {
    if (error instanceof TaskFailure) return error
    if (error instanceof LlmError) {
      const code = error.code === 'ABORTED' ? 'cancelled' : 'agent_error'
      return new TaskFailure(code, error.code)
    }
    if (error instanceof TaskMutationConflict) return new TaskFailure('host_mutating')
    return new TaskFailure('internal_error')
  }
}


declare module '@deepseek-ai/cordis' {
  interface Context {
    /** Restricted komari-bot task runtime, provided by this plugin instance. */
    komariAgent: KomariAgentRuntime
  }
}
