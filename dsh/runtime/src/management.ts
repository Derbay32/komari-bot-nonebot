/**
 * Restricted management of the private DSH profile.
 *
 * Providers, credentials, bindings, and preset prompts are read and written
 * only through the official Settings and Credentials services. This module
 * exposes exactly the resources TSK-202 owns; it never exposes arbitrary
 * settings paths, patches, component insertion, or a network listener.
 */
import { createHash, randomUUID } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-credentials'
import { credentialRef, type CredentialInfo } from '@deepseek-ai/dsh-credentials'
import type {} from '@deepseek-ai/dsh-settings'
import {
  SettingsConflictError,
  type SettingsDescriptor,
  type SettingsPathOp,
} from '@deepseek-ai/dsh-settings'
import { supportedProtocols } from '@deepseek-ai/dsh-llm-pi-ai'
import { FIXED_RETRY_POLICY } from './business.js'
import type KomariAgentRuntime from './business.js'
import { TaskMutationConflict } from './gate.js'
import {
  isPlainObject,
  MAX_PROMPT_CHARS,
  MAX_RESULT_BYTES,
  type JsonValue,
  type TaskKind,
} from './protocol.js'

const LLM_SETTINGS_NS = 'llm-pi-ai'
const AGENT_SETTINGS_NS = 'komari-agent'
const PROVIDER_ID_PATTERN = /^[a-z][a-z0-9-]{0,63}$/
const ETAG_PATTERN = /^[A-Za-z0-9._~-]{1,256}$/
const PROVIDER_FIELDS = [
  'api',
  'baseURL',
  'displayName',
  'models',
  'defaultContextWindow',
  'defaultMaxTokens',
  'defaultInput',
  'timeoutMs',
  'streamIdleTimeoutMs',
] as const
const MODEL_FIELDS = [
  'id',
  'name',
  'contextWindow',
  'maxTokens',
  'input',
  'reasoningEfforts',
] as const
const THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const
const INPUT_MODALITIES = ['text', 'image'] as const
const MAX_PROVIDER_MODELS = 100
const MAX_CREDENTIAL_CHARS = 12_000
const MAX_BASE_URL_CHARS = 2_048
const MAX_MODEL_ID_CHARS = 128
const MAX_MODEL_NAME_CHARS = 128
const MAX_TIMEOUT_MS = 2_147_483_647

export type ManagementErrorCode =
  | 'invalid_request'
  | 'invalid_provider'
  | 'invalid_provider_id'
  | 'invalid_binding'
  | 'invalid_prompt'
  | 'invalid_credential'
  | 'not_found'
  | 'revision_conflict'
  | 'active_tasks'
  | 'provider_in_use'
  | 'unavailable'

const SAFE_MESSAGES: Readonly<Record<ManagementErrorCode, string>> = {
  invalid_request: 'The management request is invalid.',
  invalid_provider: 'The provider definition is invalid.',
  invalid_provider_id: 'The provider identifier is invalid.',
  invalid_binding: 'The model binding is invalid.',
  invalid_prompt: 'The preset prompt is invalid.',
  invalid_credential: 'The credential is invalid.',
  not_found: 'The requested resource was not found.',
  revision_conflict: 'The resource changed since it was read; read it again.',
  active_tasks: 'A connection mutation cannot run while a task is live.',
  provider_in_use: 'The provider is still selected by a model binding.',
  unavailable: 'DSH management is unavailable.',
}

export class ManagementError extends Error {
  readonly code: ManagementErrorCode

  constructor(code: ManagementErrorCode) {
    super(SAFE_MESSAGES[code])
    this.name = 'ManagementError'
    this.code = code
  }
}

export type ControlResult = { readonly [key: string]: JsonValue }
type JsonObject = { readonly [key: string]: JsonValue }

function objectOrEmpty(value: unknown): Record<string, unknown> {
  return isPlainObject(value) ? value : {}
}

function stringField(value: unknown, code: ManagementErrorCode, max: number): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) {
    throw new ManagementError(code)
  }
  return value
}

function boundedText(value: unknown, code: ManagementErrorCode, max: number): string {
  const text = stringField(value, code, max)
  if (text.trim().length === 0 || /[\u0000-\u001f\u007f]/.test(text)) {
    throw new ManagementError(code)
  }
  return text
}

function providerId(value: unknown): string {
  if (typeof value !== 'string' || PROVIDER_ID_PATTERN.test(value) === false) {
    throw new ManagementError('invalid_provider_id')
  }
  return value
}

function taskKind(value: unknown): TaskKind {
  if (value !== 'chat' && value !== 'summary') throw new ManagementError('invalid_request')
  return value
}

function expectedEtag(value: unknown): string {
  if (typeof value !== 'string' || ETAG_PATTERN.test(value) === false) {
    throw new ManagementError('invalid_request')
  }
  return value
}

function allowedKeys(value: Record<string, unknown>, keys: readonly string[], code: ManagementErrorCode): void {
  for (const key of Object.keys(value)) {
    if (!keys.includes(key)) throw new ManagementError(code)
  }
}

function requiredString(value: unknown, code: ManagementErrorCode, max: number): string {
  return stringField(value, code, max)
}

function positiveInteger(value: unknown, code: ManagementErrorCode): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1 || value > MAX_TIMEOUT_MS) {
    throw new ManagementError(code)
  }
  return value
}

function inputModalities(value: unknown, code: ManagementErrorCode): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > INPUT_MODALITIES.length) {
    throw new ManagementError(code)
  }
  const result: string[] = []
  for (const entry of value) {
    if (typeof entry !== 'string' || !INPUT_MODALITIES.includes(entry as (typeof INPUT_MODALITIES)[number])) {
      throw new ManagementError(code)
    }
    if (result.includes(entry)) throw new ManagementError(code)
    result.push(entry)
  }
  return result
}

function reasoningEfforts(value: unknown, code: ManagementErrorCode): false | Record<string, string | null> {
  if (value === false) return false
  if (!isPlainObject(value)) throw new ManagementError(code)
  const keys = Object.keys(value)
  if (keys.length === 0) throw new ManagementError(code)
  const result: Record<string, string | null> = {}
  for (const level of keys) {
    if (!THINKING_LEVELS.includes(level as (typeof THINKING_LEVELS)[number])) {
      throw new ManagementError(code)
    }
    const wire = value[level]
    if (level === 'off') {
      if (wire !== null && (typeof wire !== 'string' || wire.length > MAX_MODEL_NAME_CHARS)) {
        throw new ManagementError(code)
      }
      result[level] = wire
      continue
    }
    if (typeof wire !== 'string' || wire.length === 0 || wire.length > MAX_MODEL_NAME_CHARS) {
      throw new ManagementError(code)
    }
    result[level] = wire
  }
  return result
}

function normalizeModel(value: unknown): Record<string, unknown> {
  if (!isPlainObject(value)) throw new ManagementError('invalid_provider')
  allowedKeys(value, MODEL_FIELDS, 'invalid_provider')
  const id = stringField(value.id, 'invalid_provider', MAX_MODEL_ID_CHARS)
  if (/\s/.test(id) || /[\u0000-\u001f\u007f]/.test(id)) throw new ManagementError('invalid_provider')
  const model: Record<string, unknown> = { id }
  if (value.name !== undefined) {
    model.name = boundedText(value.name, 'invalid_provider', MAX_MODEL_NAME_CHARS)
  }
  if (value.contextWindow !== undefined) {
    model.contextWindow = positiveInteger(value.contextWindow, 'invalid_provider')
  }
  if (value.maxTokens !== undefined) {
    model.maxTokens = positiveInteger(value.maxTokens, 'invalid_provider')
  }
  if (value.input !== undefined) {
    model.input = inputModalities(value.input, 'invalid_provider')
  }
  if (value.reasoningEfforts !== undefined) {
    model.reasoningEfforts = reasoningEfforts(value.reasoningEfforts, 'invalid_provider')
  }
  return model
}

function normalizeBaseUrl(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > MAX_BASE_URL_CHARS) {
    throw new ManagementError('invalid_provider')
  }
  if (/[\u0000-\u0020\u007f]/.test(value)) throw new ManagementError('invalid_provider')
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new ManagementError('invalid_provider')
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new ManagementError('invalid_provider')
  if (url.username.length > 0 || url.password.length > 0) throw new ManagementError('invalid_provider')
  if (url.search.length > 0 || url.hash.length > 0) throw new ManagementError('invalid_provider')
  if (url.hostname.length === 0) throw new ManagementError('invalid_provider')
  return value
}

function normalizeProviderProfile(value: unknown): Record<string, unknown> {
  if (!isPlainObject(value)) throw new ManagementError('invalid_provider')
  allowedKeys(value, PROVIDER_FIELDS, 'invalid_provider')
  const api = stringField(value.api, 'invalid_provider', 64)
  if (!supportedProtocols().includes(api)) throw new ManagementError('invalid_provider')
  const baseURL = normalizeBaseUrl(value.baseURL)
  if (!Array.isArray(value.models) || value.models.length === 0 || value.models.length > MAX_PROVIDER_MODELS) {
    throw new ManagementError('invalid_provider')
  }
  const profile: Record<string, unknown> = {
    api,
    baseURL,
    models: value.models.map(normalizeModel),
  }
  if (value.displayName !== undefined) {
    profile.displayName = boundedText(value.displayName, 'invalid_provider', MAX_MODEL_NAME_CHARS)
  }
  if (value.defaultContextWindow !== undefined) {
    profile.defaultContextWindow = positiveInteger(value.defaultContextWindow, 'invalid_provider')
  }
  if (value.defaultMaxTokens !== undefined) {
    profile.defaultMaxTokens = positiveInteger(value.defaultMaxTokens, 'invalid_provider')
  }
  if (value.defaultInput !== undefined) {
    profile.defaultInput = inputModalities(value.defaultInput, 'invalid_provider')
  }
  if (value.timeoutMs !== undefined) {
    profile.timeoutMs = positiveInteger(value.timeoutMs, 'invalid_provider')
  }
  if (value.streamIdleTimeoutMs !== undefined) {
    profile.streamIdleTimeoutMs = positiveInteger(value.streamIdleTimeoutMs, 'invalid_provider')
  }
  return profile
}

function configuredValueMatches(actual: unknown, expected: unknown): boolean {
  if (Array.isArray(expected)) {
    if (!Array.isArray(actual) || actual.length !== expected.length) return false
    return expected.every((entry, index) => configuredValueMatches(actual[index], entry))
  }
  if (isPlainObject(expected)) {
    if (!isPlainObject(actual)) return false
    return Object.keys(expected).every((key) => configuredValueMatches(actual[key], expected[key]))
  }
  return isDeepStrictEqual(actual, expected)
}

function projectModelView(model: Record<string, unknown>): JsonObject {
  const view: Record<string, JsonValue> = {}
  for (const field of MODEL_FIELDS) {
    const value = model[field]
    if (value === undefined) continue
    if (field === 'input' && Array.isArray(value) && value.length === 0) continue
    view[field] = value as JsonValue
  }
  return view
}

function providerView(provider: string, profile: Record<string, unknown>): JsonObject {
  const view: Record<string, JsonValue> = { provider }
  for (const field of PROVIDER_FIELDS) {
    const value = profile[field]
    if (value === undefined) continue
    if (field === 'models' && Array.isArray(value)) {
      view.models = value
        .filter((entry): entry is Record<string, unknown> => isPlainObject(entry))
        .map(projectModelView)
      continue
    }
    if (Array.isArray(value) && value.length === 0) continue
    view[field] = value as JsonValue
  }
  return view
}

function retryPolicyMatches(value: unknown): boolean {
  if (!isPlainObject(value)) return false
  const fixed = FIXED_RETRY_POLICY
  if (fixed.mode !== 'normal') return false
  const codes = Array.isArray(value.retryableCodes) ? [...value.retryableCodes].sort() : []
  const expected = [...(fixed.retryableCodes ?? [])].sort()
  return value.mode === fixed.mode
    && value.maxRetries === fixed.maxRetries
    && codes.length === expected.length
    && codes.every((code, index) => code === expected[index])
}

function credentialRefFor(provider: string): ReturnType<typeof credentialRef> {
  return credentialRef(`KOMARI_DSH_${provider.replaceAll('-', '_').toUpperCase()}_API_KEY`)
}

function credentialView(provider: string, info: CredentialInfo): JsonObject {
  const view: Record<string, JsonValue> = {
    provider,
    configured: info.configured,
    writable: info.writable,
  }
  if (info.source !== undefined) view.source = info.source
  return view
}

function bindingView(kind: TaskKind, binding: Record<string, unknown>): JsonObject {
  const view: Record<string, JsonValue> = { kind }
  if (typeof binding.provider === 'string' && binding.provider.length > 0) view.provider = binding.provider
  if (typeof binding.model === 'string' && binding.model.length > 0) view.model = binding.model
  if (typeof binding.reasoningEffort === 'string' && binding.reasoningEffort.length > 0) {
    view.reasoningEffort = binding.reasoningEffort
  }
  return view
}

function promptValue(binding: Record<string, unknown>): string {
  return typeof binding.prompt === 'string' ? binding.prompt : ''
}

/**
 * One management operation queue shared by reads and writes. It serializes an
 * ETag check, the one official mutation, and the effective readback so two
 * callers cannot interleave a stale check with a committed write.
 */
export class DshManagementService {
  private readonly epoch = randomUUID()
  private readonly credentialRevisions = new Map<string, number>()
  private queue: Promise<unknown> = Promise.resolve()

  constructor(
    private readonly ctx: Context,
    private readonly runtime: KomariAgentRuntime,
  ) {
    ctx.on('credentials/reference-updated', (ref) => {
      const name = String(ref)
      this.credentialRevisions.set(name, (this.credentialRevisions.get(name) ?? 0) + 1)
    })
  }

  async handle(op: string, args: Readonly<Record<string, JsonValue>>): Promise<ControlResult> {
    let result: ControlResult
    try {
      result = await this.serialized(async () => {
        if (!isPlainObject(args)) throw new ManagementError('invalid_request')
        switch (op) {
        case 'providers.list':
          return await this.listProviders(args)
        case 'providers.get':
          return await this.getProvider(args)
        case 'providers.put':
          return await this.putProvider(args)
        case 'providers.delete':
          return await this.deleteProvider(args)
        case 'credential.get':
          return await this.getCredential(args)
        case 'credential.put':
          return await this.putCredential(args)
        case 'credential.delete':
          return await this.deleteCredential(args)
        case 'binding.get':
          return await this.getBinding(args)
        case 'binding.put':
          return await this.putBinding(args)
        case 'binding.delete':
          return await this.deleteBinding(args)
        case 'prompt.get':
          return await this.getPrompt(args)
        case 'prompt.put':
          return await this.putPrompt(args)
          default:
            throw new ManagementError('invalid_request')
        }
      })
    } catch (error) {
      if (error instanceof TaskMutationConflict) {
        throw new ManagementError(
          error.code === 'active_tasks' ? 'active_tasks' : 'unavailable',
        )
      }
      throw error
    }
    return { ...result, bindings: this.currentBindings() }
  }

  private currentBindings(): Record<TaskKind, boolean> {
    const bindings = objectOrEmpty(this.namespace(AGENT_SETTINGS_NS).value)
    const configured = (kind: TaskKind): boolean => {
      const binding = objectOrEmpty(bindings[kind])
      return (typeof binding.provider === 'string' && binding.provider.trim().length > 0)
        && (typeof binding.model === 'string' && binding.model.trim().length > 0)
        && (typeof binding.prompt === 'string' && binding.prompt.trim().length > 0)
    }
    return { chat: configured('chat'), summary: configured('summary') }
  }

  private async serialized<T>(operation: () => Promise<T>): Promise<T> {
    const run = this.queue.then(operation, operation)
    this.queue = run.then(
      () => undefined,
      () => undefined,
    )
    return await run
  }

  private etagFor(state: JsonValue): string {
    return createHash('sha256')
      .update(JSON.stringify([this.epoch, state]))
      .digest('base64url')
  }

  private namespace(ns: string): SettingsDescriptor {
    const descriptor = this.ctx.settings.describe().find((row) => row.ns === ns)
    if (descriptor === undefined) throw new ManagementError('unavailable')
    return descriptor
  }

  private providers(descriptor: SettingsDescriptor): Record<string, unknown> {
    const value = objectOrEmpty(descriptor.value)
    return objectOrEmpty(value.providers)
  }

  private async mutate(ns: string, ops: readonly SettingsPathOp[], expectedRevision: number): Promise<void> {
    try {
      await this.ctx.settings.mutate(ns, ops, expectedRevision)
    } catch (error) {
      if (error instanceof SettingsConflictError) throw new ManagementError('revision_conflict')
      throw new ManagementError('unavailable')
    }
  }

  private assertEtag(actual: string, expected: string): void {
    if (actual !== expected) throw new ManagementError('revision_conflict')
  }

  private providerListResult(descriptor: SettingsDescriptor): ControlResult {
    const providers = this.providers(descriptor)
    const items = Object.keys(providers)
      .sort()
      .map((provider) => providerView(provider, objectOrEmpty(providers[provider])))
    const protocols = [...supportedProtocols()]
    const etag = this.etagFor({ revision: descriptor.revision, items, protocols })
    const result: ControlResult = { items, protocols, etag }
    this.assertResultBound(result)
    return result
  }

  private providerResult(descriptor: SettingsDescriptor, provider: string): ControlResult {
    const view = providerView(provider, objectOrEmpty(this.providers(descriptor)[provider]))
    const result: ControlResult = {
      provider: view,
      etag: this.providerListResult(descriptor).etag as string,
    }
    this.assertResultBound(result)
    return result
  }

  private assertResultBound(result: ControlResult): void {
    if (Buffer.byteLength(JSON.stringify(result), 'utf8') > MAX_RESULT_BYTES) {
      throw new ManagementError('unavailable')
    }
  }

  private async listProviders(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, [], 'invalid_request')
    return this.providerListResult(this.namespace(LLM_SETTINGS_NS))
  }

  private async getProvider(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['provider'], 'invalid_request')
    const provider = providerId(args.provider)
    const descriptor = this.namespace(LLM_SETTINGS_NS)
    if (!(provider in this.providers(descriptor))) throw new ManagementError('not_found')
    return this.providerResult(descriptor, provider)
  }

  private async putProvider(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['provider', 'profile', 'expectedEtag'], 'invalid_request')
    const provider = providerId(args.provider)
    const profile = normalizeProviderProfile(args.profile)
    const expected = expectedEtag(args.expectedEtag)
    return await this.runtime.runConnectionMutation(async () => {
      const descriptor = this.namespace(LLM_SETTINGS_NS)
      const providers = this.providers(descriptor)
      this.assertEtag(this.providerListResult({ ...descriptor, value: { providers } }).etag as string, expected)
      const internal = {
        ...profile,
        apiKeyEnv: credentialRefFor(provider),
        retryPolicy: FIXED_RETRY_POLICY,
      }
      await this.mutate(
        LLM_SETTINGS_NS,
        [{ op: 'set', path: ['providers', provider], value: internal }],
        descriptor.revision,
      )
      const after = this.namespace(LLM_SETTINGS_NS)
      const saved = objectOrEmpty(this.providers(after)[provider])
      if (
        !Object.keys(profile).every((field) => configuredValueMatches(saved[field], profile[field]))
        || saved.apiKeyEnv !== credentialRefFor(provider)
        || !retryPolicyMatches(saved.retryPolicy)
      ) {
        throw new ManagementError('unavailable')
      }
      return this.providerResult(after, provider)
    })
  }

  private async deleteProvider(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['provider', 'expectedEtag'], 'invalid_request')
    const provider = providerId(args.provider)
    const expected = expectedEtag(args.expectedEtag)
    return await this.runtime.runConnectionMutation(async () => {
      const descriptor = this.namespace(LLM_SETTINGS_NS)
      const providers = this.providers(descriptor)
      if (!(provider in providers)) throw new ManagementError('not_found')
      this.assertEtag(this.providerListResult({ ...descriptor, value: { providers } }).etag as string, expected)
      const agent = objectOrEmpty(this.namespace(AGENT_SETTINGS_NS).value)
      for (const kind of ['chat', 'summary'] as const) {
        const binding = objectOrEmpty(agent[kind])
        if (binding.provider === provider) throw new ManagementError('provider_in_use')
      }
      await this.mutate(
        LLM_SETTINGS_NS,
        [{ op: 'unset', path: ['providers', provider] }],
        descriptor.revision,
      )
      const after = this.namespace(LLM_SETTINGS_NS)
      if (provider in this.providers(after)) throw new ManagementError('unavailable')
      const result: ControlResult = {
        deleted: true,
        etag: this.providerListResult(after).etag as string,
      }
      this.assertResultBound(result)
      return result
    })
  }

  private credentialEtag(provider: string, view: JsonObject): string {
    const ref = String(credentialRefFor(provider))
    return this.etagFor({
      provider,
      revision: this.credentialRevisions.get(ref) ?? 0,
      credential: view,
    })
  }

  private async credentialResult(provider: string): Promise<ControlResult> {
    const info = await this.ctx.credentials.describe(credentialRefFor(provider))
    const view = credentialView(provider, info)
    return { credential: view, etag: this.credentialEtag(provider, view) }
  }

  private async getCredential(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['provider'], 'invalid_request')
    return await this.credentialResult(providerId(args.provider))
  }

  private async putCredential(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['provider', 'value', 'expectedEtag'], 'invalid_request')
    const provider = providerId(args.provider)
    const expected = expectedEtag(args.expectedEtag)
    if (typeof args.value !== 'string' || args.value.length === 0 || args.value.length > MAX_CREDENTIAL_CHARS) {
      throw new ManagementError('invalid_credential')
    }
    const value = args.value
    return await this.runtime.runConnectionMutation(async () => {
      const current = await this.credentialResult(provider)
      this.assertEtag(current.etag as string, expected)
      try {
        await this.ctx.credentials.set(credentialRefFor(provider), value)
      } catch {
        throw new ManagementError('unavailable')
      }
      const info = await this.ctx.credentials.describe(credentialRefFor(provider))
      if (!info.configured) throw new ManagementError('unavailable')
      return await this.credentialResult(provider)
    })
  }

  private async deleteCredential(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['provider', 'expectedEtag'], 'invalid_request')
    const provider = providerId(args.provider)
    const expected = expectedEtag(args.expectedEtag)
    return await this.runtime.runConnectionMutation(async () => {
      const current = await this.credentialResult(provider)
      this.assertEtag(current.etag as string, expected)
      try {
        await this.ctx.credentials.unset(credentialRefFor(provider))
      } catch {
        throw new ManagementError('unavailable')
      }
      return await this.credentialResult(provider)
    })
  }

  private agentNamespace(): { descriptor: SettingsDescriptor; bindings: Record<string, unknown> } {
    const descriptor = this.namespace(AGENT_SETTINGS_NS)
    return { descriptor, bindings: objectOrEmpty(descriptor.value) }
  }

  private bindingEtag(descriptor: SettingsDescriptor, kind: TaskKind, view: JsonObject): string {
    return this.etagFor({ revision: descriptor.revision, kind, binding: view })
  }

  private bindingResult(
    descriptor: SettingsDescriptor,
    kind: TaskKind,
    bindings: Record<string, unknown>,
  ): ControlResult {
    const view = bindingView(kind, objectOrEmpty(bindings[kind]))
    return { binding: view, etag: this.bindingEtag(descriptor, kind, view) }
  }

  private async getBinding(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['kind'], 'invalid_request')
    const kind = taskKind(args.kind)
    const { descriptor, bindings } = this.agentNamespace()
    return this.bindingResult(descriptor, kind, bindings)
  }

  private async putBinding(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['kind', 'provider', 'model', 'reasoningEffort', 'expectedEtag'], 'invalid_request')
    const kind = taskKind(args.kind)
    const provider = providerId(args.provider)
    const model = boundedText(args.model, 'invalid_binding', 256)
    const reasoningEffort = args.reasoningEffort === undefined
      ? undefined
      : stringField(args.reasoningEffort, 'invalid_binding', 32)
    const expected = expectedEtag(args.expectedEtag)

    let modelInfo
    try {
      modelInfo = await this.ctx.llm.resolveModelInfo(provider, model)
    } catch {
      throw new ManagementError('invalid_binding')
    }
    if (
      reasoningEffort !== undefined
      && !(modelInfo.reasoning?.efforts.some((effort) => String(effort.id) === reasoningEffort) ?? false)
    ) {
      throw new ManagementError('invalid_binding')
    }

    const { descriptor, bindings } = this.agentNamespace()
    const before = objectOrEmpty(bindings[kind])
    const current = bindingView(kind, before)
    this.assertEtag(this.bindingEtag(descriptor, kind, current), expected)
    const ops: SettingsPathOp[] = [
      { op: 'set', path: [kind, 'provider'], value: provider },
      { op: 'set', path: [kind, 'model'], value: model },
      reasoningEffort === undefined
        ? { op: 'unset', path: [kind, 'reasoningEffort'] }
        : { op: 'set', path: [kind, 'reasoningEffort'], value: reasoningEffort },
    ]
    await this.mutate(AGENT_SETTINGS_NS, ops, descriptor.revision)
    const after = this.agentNamespace()
    const saved = objectOrEmpty(after.bindings[kind])
    const savedView = bindingView(kind, saved)
    if (
      savedView.provider !== provider
      || savedView.model !== model
      || savedView.reasoningEffort !== reasoningEffort
      || promptValue(saved) !== promptValue(before)
    ) {
      throw new ManagementError('unavailable')
    }
    return this.bindingResult(after.descriptor, kind, after.bindings)
  }

  private async deleteBinding(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['kind', 'expectedEtag'], 'invalid_request')
    const kind = taskKind(args.kind)
    const expected = expectedEtag(args.expectedEtag)
    const { descriptor, bindings } = this.agentNamespace()
    const before = objectOrEmpty(bindings[kind])
    this.assertEtag(this.bindingEtag(descriptor, kind, bindingView(kind, before)), expected)
    const ops: SettingsPathOp[] = [
      { op: 'unset', path: [kind, 'provider'] },
      { op: 'unset', path: [kind, 'model'] },
      { op: 'unset', path: [kind, 'reasoningEffort'] },
    ]
    await this.mutate(AGENT_SETTINGS_NS, ops, descriptor.revision)
    const after = this.agentNamespace()
    const saved = objectOrEmpty(after.bindings[kind])
    const savedView = bindingView(kind, saved)
    if (
      savedView.provider !== undefined
      || savedView.model !== undefined
      || savedView.reasoningEffort !== undefined
      || promptValue(saved) !== promptValue(before)
    ) {
      throw new ManagementError('unavailable')
    }
    return this.bindingResult(after.descriptor, kind, after.bindings)
  }

  private async getPrompt(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['kind'], 'invalid_request')
    const kind = taskKind(args.kind)
    const { descriptor, bindings } = this.agentNamespace()
    const binding = objectOrEmpty(bindings[kind])
    const value = promptValue(binding)
    return {
      prompt: { kind, value },
      etag: this.etagFor({ revision: descriptor.revision, kind, prompt: value }),
    }
  }

  private async putPrompt(args: Record<string, JsonValue>): Promise<ControlResult> {
    allowedKeys(args, ['kind', 'value', 'expectedEtag'], 'invalid_request')
    const kind = taskKind(args.kind)
    const value = stringField(args.value, 'invalid_prompt', MAX_PROMPT_CHARS)
    if (value.trim().length === 0) throw new ManagementError('invalid_prompt')
    const expected = expectedEtag(args.expectedEtag)
    const { descriptor, bindings } = this.agentNamespace()
    const before = objectOrEmpty(bindings[kind])
    const current = promptValue(before)
    this.assertEtag(this.etagFor({ revision: descriptor.revision, kind, prompt: current }), expected)
    await this.mutate(
      AGENT_SETTINGS_NS,
      [{ op: 'set', path: [kind, 'prompt'], value }],
      descriptor.revision,
    )
    const after = this.agentNamespace()
    const saved = objectOrEmpty(after.bindings[kind])
    if (
      promptValue(saved) !== value
      || saved.provider !== before.provider
      || saved.model !== before.model
      || saved.reasoningEffort !== before.reasoningEffort
    ) {
      throw new ManagementError('unavailable')
    }
    return {
      prompt: { kind, value },
      etag: this.etagFor({ revision: after.descriptor.revision, kind, prompt: value }),
    }
  }
}

export { LLM_SETTINGS_NS, AGENT_SETTINGS_NS, SAFE_MESSAGES }
