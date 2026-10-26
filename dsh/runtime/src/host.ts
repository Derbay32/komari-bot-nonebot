/**
 * Official DSH boot of the private profile and the callback transport between
 * project tools and the Python driver.
 */
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import type { Context } from '@deepseek-ai/cordis'
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'
import {
  boot,
  initProfile,
  loadProfileDirectory,
  type Profile,
} from '@deepseek-ai/dsh-app-boot'
import type { ServerFrame } from './protocol.js'
import {
  callbackFrame,
  deepFreezeJson,
  MAX_PENDING_CALLBACKS,
  type CallbackPayload,
  type JsonValue,
  type TaskKind,
} from './protocol.js'
import KomariAgentRuntime, { TaskFailure, type CallbackTransport } from './business.js'

export const PROFILE_BUNDLE = '@komari-bot/dsh-runtime-bundle'
export const PROFILE_PATCH = 'cordis.patch.yml'
export const PROFILE_MANIFEST = 'package.json'
export const CREDENTIALS_FILE = '.credentials.yaml'

export type FrameSender = (frame: ServerFrame) => Promise<void>

export class HostBootError extends Error {
  readonly code: string

  constructor(code: string) {
    super(code)
    this.name = 'HostBootError'
    this.code = code
  }
}

export interface StartHostOptions {
  /** Private writable profile directory under DATA_DIR/dsh. */
  readonly profileDir: string
  /** Absolute path of this installation's dsh/package.json. */
  readonly installAnchor: string
  /** Absolute path of the shipped empty base config. */
  readonly baseConfig: string
  /** Private harness home. Never the operator's ~/.dsh. */
  readonly dshHome: string
  /** Test-only trusted composition patches. Production passes none. */
  readonly extraPatches?: readonly PatchOptions[]
  /** Protocol frame sink; callback frames travel through it. */
  readonly sendFrame: FrameSender
}

export interface HostRuntime {
  readonly ctx: Context
  readonly service: KomariAgentRuntime
  readonly bindings: Readonly<Record<TaskKind, boolean>>
  readonly callbacks: CallbackDispatcher
  close(): Promise<void>
}

/** Create only absent profile files; never rewrite saved patches or credentials. */
export function bootstrapProfile(profileDir: string): void {
  mkdirSync(profileDir, { recursive: true })
  const manifestPath = join(profileDir, PROFILE_MANIFEST)
  if (!existsSync(manifestPath)) {
    initProfile(profileDir, [PROFILE_BUNDLE])
  } else if (!existsSync(join(profileDir, PROFILE_PATCH))) {
    writeFileSync(join(profileDir, PROFILE_PATCH), '[]\n', 'utf8')
  }
}

function loadPrivateProfile(profileDir: string, installAnchor: string): Profile {
  const profile = loadProfileDirectory('komari-agent', profileDir, installAnchor)
  if (profile.skippedBundles.length > 0) throw new HostBootError('profile_bundle_unavailable')
  if (!profile.layers.some((layer) => layer.packageName === PROFILE_BUNDLE)) {
    throw new HostBootError('profile_bundle_missing')
  }
  return profile
}

interface PendingCallback {
  readonly resolve: (value: JsonValue) => void
  readonly reject: (error: Error) => void
  readonly taskRequestId: string
  readonly signal: AbortSignal
  readonly onAbort: () => void
  readonly timeout: undefined
}

/**
 * Route project-tool callbacks to Python and correlate the results. Unknown,
 * late, and already-cancelled callback IDs never resolve a live tool call.
 */
export class CallbackDispatcher implements CallbackTransport {
  private readonly pending = new Map<string, PendingCallback>()
  private readonly tombstones: string[] = []
  private readonly tombstoneSet = new Set<string>()
  private readonly countsByTask = new Map<string, number>()
  private closed = false

  constructor(private readonly sendFrame: FrameSender) {}

  async requestCallback(payload: CallbackPayload, signal: AbortSignal): Promise<JsonValue> {
    if (this.closed) throw new TaskFailure('host_closed')
    if (signal.aborted) throw new TaskFailure('cancelled')
    const count = this.countsByTask.get(payload.request_id) ?? 0
    if (count >= MAX_PENDING_CALLBACKS) throw new TaskFailure('too_many_callbacks')
    this.countsByTask.set(payload.request_id, count + 1)
    const id = `cb-${randomUUID()}`
    return new Promise<JsonValue>((resolve, reject) => {
      const onAbort = () => {
        if (!this.pending.delete(id)) return
        this.decrement(payload.request_id)
        this.tombstone(id)
        reject(new TaskFailure('cancelled'))
      }
      signal.addEventListener('abort', onAbort, { once: true })
      this.pending.set(id, {
        resolve: (value) => {
          this.cleanup(id)
          resolve(value)
        },
        reject: (error) => {
          this.cleanup(id)
          reject(error)
        },
        taskRequestId: payload.request_id,
        signal,
        onAbort,
        timeout: undefined,
      })
      this.sendFrame(callbackFrame(id, payload)).catch((error: unknown) => {
        const entry = this.pending.get(id)
        if (entry !== undefined) entry.reject(error instanceof Error ? error : new TaskFailure('send_failed'))
      })
    })
  }

  /** Resolve one callback; returns false for an unknown non-tombstoned ID. */
  handleResult(id: string, payload: { readonly ok: true; readonly result: JsonValue } | { readonly ok: false; readonly error: { readonly code: string; readonly message: string } }): boolean {
    const entry = this.pending.get(id)
    if (entry === undefined) return this.tombstoneSet.has(id)
    if (payload.ok) {
      entry.resolve(deepFreezeJson(payload.result))
    } else {
      entry.reject(new TaskFailure('callback_failed', payload.error.code))
    }
    return true
  }

  cancelAll(): void {
    this.closed = true
    for (const [id, entry] of [...this.pending.entries()]) {
      this.pending.delete(id)
      this.decrement(entry.taskRequestId)
      entry.signal.removeEventListener('abort', entry.onAbort)
      this.tombstone(id)
      entry.reject(new TaskFailure('cancelled'))
    }
  }

  private cleanup(id: string): void {
    const entry = this.pending.get(id)
    if (entry === undefined) return
    this.pending.delete(id)
    this.decrement(entry.taskRequestId)
    entry.signal.removeEventListener('abort', entry.onAbort)
  }

  private decrement(taskRequestId: string): void {
    const count = this.countsByTask.get(taskRequestId) ?? 0
    if (count <= 1) this.countsByTask.delete(taskRequestId)
    else this.countsByTask.set(taskRequestId, count - 1)
  }

  private tombstone(id: string): void {
    this.tombstones.push(id)
    this.tombstoneSet.add(id)
    while (this.tombstones.length > 256) {
      const removed = this.tombstones.shift()
      if (removed !== undefined) this.tombstoneSet.delete(removed)
    }
  }
}

function assertComposition(ctx: Context): void {
  for (const forbidden of ['sessionPersistence', 'sessionLog', 'pluginManager', 'sandboxPolicy', 'userApproval', 'jobs', 'skills', 'mcp', 'workingDirectory', 'ptcRuntime']) {
    if (ctx.get(forbidden) !== undefined) throw new HostBootError(`forbidden_service:${forbidden}`)
  }
  const globalTools = ctx.tools.schemas()
  if (globalTools.length > 0) throw new HostBootError('forbidden_global_tools')
}

export async function startHost(options: StartHostOptions): Promise<HostRuntime> {
  if (process.env.DSH_HOME !== options.dshHome) process.env.DSH_HOME = options.dshHome
  bootstrapProfile(options.profileDir)
  const profile = loadPrivateProfile(options.profileDir, options.installAnchor)
  const patches = [
    ...profile.layers.flatMap((layer) => layer.patches),
    ...profile.patches,
    ...(options.extraPatches ?? []),
  ]
  const profileContext = {
    name: 'komari-agent',
    dir: options.profileDir,
    patchPath: join(options.profileDir, PROFILE_PATCH),
    installAnchor: options.installAnchor,
    cwd: process.cwd(),
    home: options.dshHome,
    startedBundles: profile.layers.map((layer) => layer.packageName),
    overlays: [],
    telemetryDisabledEnv: '1',
  }
  let ctx: Context
  try {
    ctx = await boot('komari-agent', options.baseConfig, patches, (prepared) => {
      prepared.provide('profileContext', profileContext)
    }, pathToFileURL(options.installAnchor).href)
  } catch {
    throw new HostBootError('boot_failed')
  }
  try {
    assertComposition(ctx)
    const service = ctx.get('komariAgent')
    if (service === undefined) throw new HostBootError('composition_incomplete')
    const callbacks = new CallbackDispatcher(options.sendFrame)
    service.attachCallbackTransport(callbacks)
    let closed = false
    return {
      ctx,
      service,
      bindings: service.bindingStatus(),
      callbacks,
      close: async () => {
        if (closed) return
        closed = true
        service.cancelAll()
        callbacks.cancelAll()
        await ctx.fiber.dispose()
      },
    }
  } catch (error) {
    await ctx.fiber.dispose().catch(() => undefined)
    throw error
  }
}
