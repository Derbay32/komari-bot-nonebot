/**
 * Test-only fixture tools. This module is mounted exclusively by the test
 * host entry point; the production composition never references it.
 */
import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-settings'
import type {} from '@deepseek-ai/dsh-agent-loop'
import { isPlainObject, type JsonValue } from './protocol.js'

export const name = 'komari-agent-fixture'
export const inject = ['tools', 'komariAgent', 'settings']

export function apply(ctx: Context): void {
  ctx.tools.register(defineTool({
    name: 'fixture_complete',
    description: 'Submit the fixture business result through the project completion slot.',
    parameters: {
      value: { type: 'string', required: true, description: 'Fixture result value.' },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          completed: { type: 'boolean', required: true },
        },
        additionalProperties: false,
      },
      render: () => [{ type: 'text', text: 'fixture result completed' }],
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('fixture tool requires an agent')
      const value = await ctx.komariAgent.invokeBusinessCallback(exec.agent, 'fixture_result', {
        value: args.value,
      })
      if (!isPlainObject(value)) throw new Error('fixture callback did not return an object')
      ctx.komariAgent.complete(exec.agent, value as { readonly [key: string]: JsonValue })
      exec.concludeTurn()
      return { completed: true }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'fixture_update_prompt',
    description: 'Update the project chat prompt through the official settings seam, then queue a follow-up turn.',
    parameters: {
      prompt: { type: 'string', required: true, description: 'New chat prompt.' },
      text: { type: 'string', required: true, description: 'Follow-up text.' },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          updated: { type: 'boolean', required: true },
        },
        additionalProperties: false,
      },
      render: () => [{ type: 'text', text: 'fixture prompt updated' }],
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('fixture tool requires an agent')
      const descriptor = ctx.settings.describe().find((row) => row.ns === 'komari-agent')
      if (descriptor === undefined) throw new Error('missing komari-agent settings namespace')
      await ctx.settings.update('komari-agent', { chat: { prompt: args.prompt } }, descriptor.revision)
      exec.agent.followup(createUserMessage({
        content: [{ type: 'text', text: args.text }],
        source: { kind: 'user' },
      }))
      exec.concludeTurn()
      return { updated: true }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'fixture_followup',
    description: 'Queue one additional fixture user turn without concluding the current turn.',
    parameters: {
      text: { type: 'string', required: true, description: 'Fixture follow-up text.' },
      conclude: { type: 'boolean', description: 'End the current turn after queueing the follow-up.' },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          queued: { type: 'boolean', required: true },
        },
        additionalProperties: false,
      },
      render: () => [{ type: 'text', text: 'fixture follow-up queued' }],
    },
    async execute(args, exec) {
      if (exec.agent === undefined) throw new Error('fixture tool requires an agent')
      exec.agent.followup(createUserMessage({
        content: [{ type: 'text', text: args.text }],
        source: { kind: 'user' },
      }))
      if (args.conclude === true) exec.concludeTurn()
      return { queued: true }
    },
  }))
}


declare module '@deepseek-ai/cordis' {
  interface Context {
    komariAgent: import('./business.js').default
  }
}
