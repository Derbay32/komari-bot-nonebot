/**
 * Shared task-scoped completion, round, and tool-budget policy for project
 * presets. Chat and summary presets register only their own missing-business
 * requirements here; this module never owns business arguments or schemas.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import type { SessionEvent } from '@deepseek-ai/dsh-session'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { TaskBudget } from './protocol.js'

/** Fixed terminal reasons emitted by the shared policy. */
export type TaskPolicyFailure = 'budget_exhausted' | 'callback_failed'

/** The live task facts the policy may inspect without receiving the task object. */
export interface TaskPolicyOwner {
  readonly budget: TaskBudget
  isComplete(): boolean
  isTerminal(): boolean
  recordTerminalFailure(code: TaskPolicyFailure): void
}

const MISSING_REQUIREMENTS_PREFIX = 'The task is not complete. Submit the missing required business result(s) with their declared tools: '

/**
 * One policy instance per live DSH task. It is attached to the agent scope, so
 * all event/guard listeners disappear with that agent and cannot observe a
 * later task.
 */
export class SharedTaskPolicy {
  private roundsUsed = 0
  private toolCallsUsed = 0
  private batchRejected = false
  private completed = false
  private terminalCode: TaskPolicyFailure | undefined
  private missingRequirements: (() => readonly string[]) | undefined
  private readonly seenAssistantEvents = new Set<number>()

  constructor(private readonly owner: TaskPolicyOwner) {}

  attach(ctx: Context): void {
    ctx.on('agent/pre-step', async (_payload, next) => {
      if (this.owner.isComplete()) return { kind: 'reject' } satisfies PreStepDecision
      if (this.isTerminal()) return { kind: 'reject' } satisfies PreStepDecision
      this.roundsUsed += 1
      if (this.roundsUsed > this.owner.budget.max_rounds) {
        this.terminate('budget_exhausted')
        return { kind: 'reject' } satisfies PreStepDecision
      }
      // A rejected batch is a step-local condition. The next admitted step is
      // the correction opportunity.
      this.batchRejected = false
      return next()
    })

    ctx.on('agent/turn-stopping', async (payload) => {
      if (this.owner.isComplete() || this.isTerminal()) return
      const missing = this.missingRequirements?.() ?? []
      if (missing.length === 0) return
      payload.agent.steer(createUserMessage({
        content: [{ type: 'text', text: `${MISSING_REQUIREMENTS_PREFIX}${missing.join(', ')}` }],
        source: { kind: 'user' },
      }))
    })

    ctx.tools.guard(() => {
      if (this.owner.isComplete()) return 'task_completed'
      if (this.isTerminal()) return this.terminalCode ?? 'task_terminal'
      if (this.batchRejected) return 'batch_rejected'
      return undefined
    })
  }

  /**
   * Observe one committed session event. Only the committed assistant/message
   * event proposes tool calls; stream chunks and retries do not charge another
   * round. The sequence set makes replay/duplicate observation harmless.
   */
  observeSessionEvent(event: SessionEvent): void {
    if (this.owner.isComplete() || this.isTerminal()) return
    if (event.type !== 'assistant/message') return
    if (this.seenAssistantEvents.has(event.seq)) return
    this.seenAssistantEvents.add(event.seq)
    const proposed = event.data.message.content.filter((block) => block.type === 'tool-call').length
    if (proposed === 0) return

    this.toolCallsUsed += proposed
    if (this.toolCallsUsed > this.owner.budget.max_total_tool_calls) {
      this.terminate('budget_exhausted')
      return
    }
    if (proposed > this.owner.budget.max_tool_calls_per_round) {
      // Deny every call in this batch. The next admitted step may correct the
      // submission within the remaining total budget.
      this.batchRejected = true
    }
  }

  registerMissingRequirements(checker: () => readonly string[]): void {
    this.missingRequirements = checker
  }

  markCompleted(): void {
    this.completed = true
  }

  get rounds(): number {
    return this.roundsUsed
  }

  get toolCalls(): number {
    return this.toolCallsUsed
  }

  private isTerminal(): boolean {
    return this.terminalCode !== undefined || this.owner.isTerminal()
  }

  private terminate(code: TaskPolicyFailure): void {
    if (this.terminalCode !== undefined) return
    this.terminalCode = code
    this.owner.recordTerminalFailure(code)
  }
}

export { MISSING_REQUIREMENTS_PREFIX }
