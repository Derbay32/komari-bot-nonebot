/**
 * Test-only completion-race fixture. It exposes a global hook that queues
 * official agent steering while a completing tool is held at the test's
 * callback barrier. Production never mounts this module.
 */
import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-tools'
import type { Agent } from '@deepseek-ai/dsh-agent'

export const name = 'komari-chat-race-fixture'
export const inject = ['tools']

export function apply(ctx: Context): void {
  const raceGlobal = globalThis as typeof globalThis & {
    __komariCompletionRaceSteer?: () => void
  }
  let raceAgent: Agent | undefined
  ctx.on('tools/pre-execute', async (execution, next) => {
    if (execution.name === 'record_favorability_delta' && execution.agent !== undefined) {
      raceAgent = execution.agent
    }
    return next()
  })
  raceGlobal.__komariCompletionRaceSteer = () => {
    raceAgent?.steer(createUserMessage({
      content: [{ type: 'text', text: 'completion-race steering' }],
      source: { kind: 'user' },
    }))
  }
  ctx.effect(() => () => {
    delete raceGlobal.__komariCompletionRaceSteer
  }, 'komari-chat-race-fixture-hook')
}
