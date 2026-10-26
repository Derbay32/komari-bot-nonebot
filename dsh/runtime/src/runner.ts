/**
 * Process entry logic for the long-lived DSH child.
 *
 * stdout carries protocol frames only. stderr carries ordinary sanitized
 * application logging. No listening port and no shell subprocess are created.
 */
import { parseArgs } from 'node:util'
import { dirname, isAbsolute, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'
import { installFailLoud } from '@deepseek-ai/dsh-app-boot'
import { HostBootError, startHost, type HostRuntime } from './host.js'
import { logTo, sanitizeLogText } from './logging.js'
import {
  controlResultErrorFrame,
  controlResultSuccessFrame,
  encodeFrame,
  parseClientFrame,
  ProtocolError,
  readyFrame,
  resultErrorFrame,
  resultSuccessFrame,
  MAX_FRAME_BYTES,
  type ClientFrame,
  type ServerFrame,
} from './protocol.js'
import { ManagementError } from './management.js'
import { TaskFailure } from './business.js'

const HOST_VERSION = '0.1.0'
const SHUTDOWN_DRAIN_MS = 10_000

export interface RunOptions {
  /** Trusted constructor-only fixture rows; never selected by protocol input. */
  readonly extraPatches?: readonly PatchOptions[]
  readonly stdin?: NodeJS.ReadableStream
  readonly stdout?: NodeJS.WritableStream
  readonly stderr?: NodeJS.WritableStream
}

function encodeError(error: unknown): string {
  if (error instanceof ProtocolError) return `protocol error: ${error.code}`
  if (error instanceof HostBootError) return `host boot error: ${error.code}`
  if (error instanceof TaskFailure) return `task error: ${error.code}`
  return 'internal error'
}

const SAFE_TASK_MESSAGES: Readonly<Record<string, string>> = {
  invalid_request: 'The task request is invalid.',
  binding_unconfigured: 'No chat or summary model binding is configured.',
  invalid_binding: 'The selected binding is not valid for the configured route.',
  provider_unavailable: 'The configured provider route is unavailable.',
  retry_policy_invalid: 'The provider retry policy does not match the fixed runtime policy.',
  admission_denied: 'Task admission was denied before an image effect.',
  invalid_image_input: 'The task image transport data is invalid.',
  image_input_failed: 'A verified task image could not be prepared for the model.',
  image_model_failed: 'The model request for the task image input failed.',
  incomplete_result: 'The task ended without a validated business result.',
  cancelled: 'The task was cancelled.',
  agent_error: 'The agent requested by the task failed.',
  internal_error: 'The task could not be completed.',
  release_failed: 'The task agent could not be released cleanly.',
  drain_timeout: 'The task did not drain within the bounded terminal window.',
  host_closed: 'The DSH host is closed.',
  host_mutating: 'A connection mutation is in progress.',
  result_already_completed: 'The task result was already completed.',
  callback_failed: 'A task callback failed.',
  callback_not_registered: 'The task callback name was not registered.',
  unknown_callback: 'The task callback name is unknown.',
  too_many_callbacks: 'The task has too many outstanding callbacks.',
  send_failed: 'The protocol channel failed.',
  unknown_task: 'The callback is not bound to a live trusted task.',
  invalid_result: 'The business result is invalid.',
  result_too_large: 'The business result is too large.',
  invalid_callback_data: 'The callback data is invalid.',
  callback_data_too_large: 'The callback data is too large.',
}

function safeTaskError(code: string): { code: string; message: string } {
  const safeCode = /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : 'internal_error'
  return { code: safeCode, message: SAFE_TASK_MESSAGES[safeCode] ?? 'The task failed.' }
}

class FrameWriter {
  private tail: Promise<void> = Promise.resolve()
  private failed = false

  constructor(private readonly stream: NodeJS.WritableStream) {}

  send(frame: ServerFrame): Promise<void> {
    if (this.failed) return Promise.reject(new Error('protocol channel closed'))
    const encoded = encodeFrame(frame)
    const next = this.tail.then(async () => {
      const accepted = this.stream.write(encoded)
      if (!accepted) {
        await new Promise<void>((resolveDrain) => {
          this.stream.once('drain', resolveDrain)
        })
      }
    })
    this.tail = next.catch(() => {
      this.failed = true
    })
    return next
  }

  async flush(): Promise<void> {
    await this.tail.catch(() => undefined)
  }
}

async function* readLines(input: NodeJS.ReadableStream): AsyncGenerator<string> {
  const encodingInput = input as NodeJS.ReadableStream & { setEncoding?: (encoding: BufferEncoding) => unknown }
  encodingInput.setEncoding?.('utf8')
  let buffer = ''
  for await (const raw of input) {
    buffer += typeof raw === 'string' ? raw : Buffer.from(raw as Uint8Array).toString('utf8')
    let newline = buffer.indexOf('\n')
    while (newline >= 0) {
      const line = buffer.slice(0, newline)
      buffer = buffer.slice(newline + 1)
      if (Buffer.byteLength(line, 'utf8') > MAX_FRAME_BYTES) throw new ProtocolError('frame_too_large')
      if (line.length > 0) yield line.endsWith('\r') ? line.slice(0, -1) : line
      newline = buffer.indexOf('\n')
    }
    if (Buffer.byteLength(buffer, 'utf8') > MAX_FRAME_BYTES) throw new ProtocolError('frame_too_large')
  }
  if (buffer.length > 0) throw new ProtocolError('unterminated_frame')
}

function resolveArgPath(value: string | undefined, name: string): string {
  if (value === undefined || value.length === 0) throw new HostBootError(`missing_${name}`)
  if (!isAbsolute(value)) throw new HostBootError(`relative_${name}`)
  return resolve(value)
}

function parseCli(argv: readonly string[]): {
  profileDir: string
  installAnchor: string
  baseConfig: string
  dshHome: string
  attachmentRoot: string
} {
  const parsed = parseArgs({
    args: [...argv],
    options: {
      'profile-dir': { type: 'string' },
      'install-anchor': { type: 'string' },
      'base-config': { type: 'string' },
      'dsh-home': { type: 'string' },
      'attachment-root': { type: 'string' },
    },
    strict: true,
    allowPositionals: false,
  })
  return {
    profileDir: resolveArgPath(parsed.values['profile-dir'], 'profile_dir'),
    installAnchor: resolveArgPath(parsed.values['install-anchor'], 'install_anchor'),
    baseConfig: resolveArgPath(parsed.values['base-config'], 'base_config'),
    dshHome: resolveArgPath(parsed.values['dsh-home'], 'dsh_home'),
    attachmentRoot: resolveArgPath(parsed.values['attachment-root'], 'attachment_root'),
  }
}

function shimConsoleToStderr(stderr: NodeJS.WritableStream): void {
  const write = (...values: unknown[]) => {
    logTo(stderr, values.map((value) => (typeof value === 'string' ? value : String(value))).join(' '))
  }
  console.log = write
  console.info = write
  console.debug = write
}

export async function runAgentProcess(argv: readonly string[], options: RunOptions = {}): Promise<number> {
  const stdin = options.stdin ?? process.stdin
  const stdout = options.stdout ?? process.stdout
  const stderr = options.stderr ?? process.stderr
  shimConsoleToStderr(stderr)
  const writer = new FrameWriter(stdout)
  let runtime: HostRuntime | undefined
  const taskPromises = new Map<string, Promise<void>>()
  const controlPromises = new Set<Promise<void>>()
  const seenControlIds = new Set<string>()
  const recentControlIds: string[] = []
  const seenTaskIds = new Set<string>()
  const recentlyCompleted: string[] = []
  const recentlyCompletedSet = new Set<string>()
  let shuttingDown = false

  const send = (frame: ServerFrame): Promise<void> => writer.send(frame)
  const failReady = async (reason: string): Promise<void> => {
    try {
      await send(readyFrame({
        state: 'unavailable',
        protocol_version: 1,
        host_version: HOST_VERSION,
        bindings: { chat: false, summary: false },
        reason,
      }))
    } catch {
      // The parent is gone; exit without stdout writes.
    }
  }

  let cli: ReturnType<typeof parseCli>
  try {
    cli = parseCli(argv)
  } catch (error) {
    logTo(stderr, encodeError(error))
    await failReady(error instanceof HostBootError ? error.code : 'invalid_arguments')
    return 2
  }

  try {
    runtime = await startHost({
      profileDir: cli.profileDir,
      installAnchor: cli.installAnchor,
      baseConfig: cli.baseConfig,
      dshHome: cli.dshHome,
      attachmentRoot: cli.attachmentRoot,
      sendFrame: send,
      ...(options.extraPatches === undefined ? {} : { extraPatches: options.extraPatches }),
    })
  } catch (error) {
    logTo(stderr, encodeError(error))
    await failReady(error instanceof HostBootError ? error.code : 'boot_failed')
    return 1
  }

  const activeRuntime = runtime
  activeRuntime.ctx.logger.exporter({
    levels: { default: 2 },
    export: ({ type, name, args }) => {
      if (type !== 'warn' && type !== 'error') return
      const text = args.map((value) => {
        if (value instanceof Error) {
          const code = (value as { code?: unknown }).code
          return `${value.name}${typeof code === 'string' ? `:${code}` : ''}`
        }
        return typeof value === 'string' ? value : String(value)
      }).join(' ')
      logTo(stderr, `${type} ${name}: ${text}`)
    },
  })

  await send(readyFrame({
    state: 'ready',
    protocol_version: 1,
    host_version: HOST_VERSION,
    bindings: activeRuntime.bindings,
  }))

  const dispatchControl = (frame: ClientFrame & { kind: 'control' }): void => {
    if (seenControlIds.has(frame.id)) throw new ProtocolError('duplicate_control_id')
    seenControlIds.add(frame.id)
    const promise = (async () => {
      try {
        const result = await activeRuntime.management.handle(frame.payload.op, frame.payload.args)
        await send(controlResultSuccessFrame(frame.id, result))
      } catch (error) {
        const failure = error instanceof ManagementError
          ? { code: error.code, message: error.message }
          : { code: 'unavailable', message: 'DSH management is unavailable.' }
        try {
          await send(controlResultErrorFrame(frame.id, failure.code, failure.message))
        } catch {
          // The parent is gone; the reader loop will fail on the next frame.
        }
      } finally {
        seenControlIds.delete(frame.id)
        recentControlIds.push(frame.id)
        while (recentControlIds.length > 128) recentControlIds.shift()
      }
    })()
    controlPromises.add(promise)
    void promise.finally(() => controlPromises.delete(promise))
  }

  const dispatchRequest = (frame: ClientFrame & { kind: 'request' }): void => {
    if (seenTaskIds.has(frame.payload.task_id)) {
      throw new ProtocolError('duplicate_task_id')
    }
    seenTaskIds.add(frame.payload.task_id)
    const promise = (async () => {
      try {
        const result = await activeRuntime.service.runTask(frame.id, frame.payload)
        await send(resultSuccessFrame(frame.id, result))
      } catch (error) {
        const failure = error instanceof TaskFailure ? error : new TaskFailure('internal_error')
        const safe = safeTaskError(failure.code)
        try {
          await send(resultErrorFrame(frame.id, safe.code, safe.message))
        } catch {
          // The parent is gone; the reader loop will fail on the next frame.
        }
        if (failure.code === 'release_failed') {
          // A possibly retained official executor must not coexist with new
          // tasks. Retire this process; Python observes exit and never restarts
          // the failed session. Force exit if official shutdown also stalls.
          const deadline = setTimeout(() => process.exit(1), SHUTDOWN_DRAIN_MS)
          deadline.unref()
          void closeRuntime().catch(() => undefined).finally(() => process.exit(1))
        }
      } finally {
        taskPromises.delete(frame.id)
        recentlyCompleted.push(frame.id)
        recentlyCompletedSet.add(frame.id)
        while (recentlyCompleted.length > 128) {
          const removed = recentlyCompleted.shift()
          if (removed !== undefined) recentlyCompletedSet.delete(removed)
        }
      }
    })()
    taskPromises.set(frame.id, promise)
  }

  const dispatchFrame = (frame: ClientFrame): boolean => {
    switch (frame.kind) {
      case 'request':
        dispatchRequest(frame)
        return true
      case 'callback_result':
        if (!activeRuntime.callbacks.handleResult(frame.id, frame.payload)) {
          throw new ProtocolError('unknown_callback_id')
        }
        return true
      case 'control':
        dispatchControl(frame)
        return true
      case 'cancel':
        // A result and a caller cancellation can cross on the wire. A cancel
        // for an already-terminal request is late and has no effect.
        if (
          !activeRuntime.service.cancelTask(frame.id)
          && !taskPromises.has(frame.id)
          && !recentlyCompletedSet.has(frame.id)
        ) {
          throw new ProtocolError('unknown_cancel_id')
        }
        return true
      case 'shutdown':
        return false
      default:
        throw new ProtocolError('unknown_frame_kind')
    }
  }

  const closeRuntime = async (): Promise<void> => {
    if (shuttingDown) return
    shuttingDown = true
    activeRuntime.service.cancelAll()
    activeRuntime.callbacks.cancelAll()
    const drained = await Promise.race([
      Promise.allSettled([...taskPromises.values(), ...controlPromises]),
      new Promise<null>((resolve) => {
        const timer = setTimeout(() => resolve(null), SHUTDOWN_DRAIN_MS)
        timer.unref()
      }),
    ])
    void drained
    await activeRuntime.close()
    await writer.flush()
  }

  const onSignal = (): void => {
    void closeRuntime().finally(() => process.exit(0))
  }
  process.once('SIGTERM', onSignal)
  process.once('SIGINT', onSignal)

  try {
    for await (const line of readLines(stdin)) {
      const frame = parseClientFrame(line)
      if (!dispatchFrame(frame)) break
    }
  } catch (error) {
    logTo(stderr, encodeError(error))
    await closeRuntime()
    process.off('SIGTERM', onSignal)
    process.off('SIGINT', onSignal)
    return error instanceof ProtocolError ? 3 : 1
  }
  await closeRuntime()
  process.off('SIGTERM', onSignal)
  process.off('SIGINT', onSignal)
  return 0
}

/** Install the official fail-loud guard and exit with the process result. */
export async function main(argv: readonly string[], options: RunOptions = {}): Promise<never> {
  installFailLoud('komari-agent', {
    on: process.on.bind(process),
    off: process.off.bind(process),
    exit: process.exit.bind(process),
    stderr: {
      write: (chunk: string) => process.stderr.write(sanitizeLogText(String(chunk))),
    },
  })
  const code = await runAgentProcess(argv, options)
  process.exit(code)
}
