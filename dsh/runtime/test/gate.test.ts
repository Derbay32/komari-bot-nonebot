import assert from 'node:assert/strict'
import test from 'node:test'
import { TaskMutationConflict, TaskMutationGate } from '../dist/gate.js'

test('a live task rejects a connection mutation with active_tasks', async () => {
  const gate = new TaskMutationGate()
  const lease = await gate.enterTask()
  await assert.rejects(
    gate.runExclusiveMutation(async () => 'saved'),
    (error: unknown) => error instanceof TaskMutationConflict && error.code === 'active_tasks',
  )
  lease.release()
  assert.equal(await gate.runExclusiveMutation(async () => 'saved'), 'saved')
})

test('task admission waits for an in-flight connection mutation', async () => {
  const gate = new TaskMutationGate()
  let releaseMutation: (() => void) | undefined
  const mutation = gate.runExclusiveMutation(async () => {
    await new Promise<void>((resolve) => {
      releaseMutation = resolve
    })
  })
  let admitted = false
  const admission = gate.enterTask().then((lease) => {
    admitted = true
    return lease
  })
  await Promise.resolve()
  assert.equal(admitted, false)
  releaseMutation?.()
  await mutation
  const lease = await admission
  assert.equal(admitted, true)
  lease.release()
})

test('concurrent connection mutations fail closed', async () => {
  const gate = new TaskMutationGate()
  let releaseMutation: (() => void) | undefined
  const mutation = gate.runExclusiveMutation(async () => {
    await new Promise<void>((resolve) => {
      releaseMutation = resolve
    })
  })
  await assert.rejects(
    gate.runExclusiveMutation(async () => 'second'),
    (error: unknown) => error instanceof TaskMutationConflict && error.code === 'mutation_in_progress',
  )
  releaseMutation?.()
  await mutation
})
