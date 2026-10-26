#!/usr/bin/env node
/**
 * Test host entry point. Fixture plugin rows are supplied here, in trusted
 * constructor code, and never selected by any protocol message.
 */
import { main } from './runner.js'
import { fixturePresetPatches } from './test-fixtures.js'

void main(process.argv.slice(2), {
  extraPatches: fixturePresetPatches(),
})
