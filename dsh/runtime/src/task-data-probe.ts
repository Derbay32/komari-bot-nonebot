/**
 * Test-only trusted probe for the narrow task-data accessor. Production never
 * composes this row; it exposes no model-visible tool or callback.
 */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent-loop'

export const name = 'komari-task-data-probe'
export const inject = ['komariAgent']

export function apply(ctx: Context): void {
  const probeGlobal = globalThis as typeof globalThis & {
    __komariTaskDataProbeReads?: Array<{ readonly [key: string]: unknown } | null>
  }
  probeGlobal.__komariTaskDataProbeReads = []
  ctx.on('agent/turn-stopping', (payload) => {
    probeGlobal.__komariTaskDataProbeReads?.push(
      ctx.komariAgent.getTaskData(payload.agent) ?? null,
    )
  })
  ctx.effect(() => () => {
    delete probeGlobal.__komariTaskDataProbeReads
  }, 'komari-task-data-probe')
}
