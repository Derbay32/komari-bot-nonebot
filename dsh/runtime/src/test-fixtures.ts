/** Trusted fixture composition patches used only by the test host entry point. */
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'

export const FIXTURE_PLUGIN = '@komari-bot/dsh-runtime/fixture'

export function fixturePresetPatches(): readonly PatchOptions[] {
  const preset = (id: 'chat' | 'summary', displayName: string): PatchOptions => ({
    id: id === 'chat' ? 'preset-chat' : 'preset-summary',
    config: {
      id,
      name: displayName,
      plugins: [
        {
          id: `fixture-${id}`,
          name: FIXTURE_PLUGIN,
        },
      ],
    },
  })
  return [preset('chat', 'Fixture chat'), preset('summary', 'Fixture summary')]
}
