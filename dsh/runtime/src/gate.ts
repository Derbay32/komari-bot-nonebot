/**
 * Synchronization between connection-level mutations and task admission.
 *
 * TSK-202 will edit provider routes and credentials through official DSH
 * services. Those writes cannot race a live task: a mutation is refused while
 * any task is live, and a task may not start while an idle mutation is being
 * applied. Binding/prompt edits are intentionally not covered here — a task
 * snapshots them at creation and the snapshot is frozen.
 */
export class TaskMutationConflict extends Error {
  readonly code: string

  constructor(code: 'active_tasks' | 'mutation_in_progress') {
    super(code === 'active_tasks' ? 'cannot apply a connection mutation while tasks are live' : 'another connection mutation is already in progress')
    this.name = 'TaskMutationConflict'
    this.code = code
  }
}

interface TaskLease {
  readonly release: () => void
}

export class TaskMutationGate {
  private readonly active = new Set<symbol>()
  private mutationActive = false
  private readonly taskWaiters: Array<() => void> = []

  get activeTaskCount(): number {
    return this.active.size
  }

  get isMutating(): boolean {
    return this.mutationActive
  }

  /**
   * Admit one task and hold its liveness until `release` is called. Waits for
   * an in-flight mutable configuration apply, then registers atomically so a
   * mutation that starts afterwards observes this task.
   */
  async enterTask(): Promise<TaskLease> {
    while (this.mutationActive) {
      await new Promise<void>((resolve) => {
        this.taskWaiters.push(resolve)
      })
    }
    const token = Symbol('live-task')
    this.active.add(token)
    return {
      release: () => {
        this.active.delete(token)
      },
    }
  }

  /**
   * Apply one provider/credential mutation while no task is live. The caller
   * owns the official DSH write and must confirm the effective state inside
   * `apply`; this gate only guarantees the admission boundary.
   */
  async runExclusiveMutation<T>(apply: () => Promise<T>): Promise<T> {
    if (this.mutationActive) throw new TaskMutationConflict('mutation_in_progress')
    if (this.active.size > 0) throw new TaskMutationConflict('active_tasks')
    this.mutationActive = true
    try {
      return await apply()
    } finally {
      this.mutationActive = false
      for (const resolve of this.taskWaiters.splice(0)) resolve()
    }
  }
}
