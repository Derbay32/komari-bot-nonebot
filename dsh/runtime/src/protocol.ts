/**
 * Versioned newline-delimited JSON envelopes for the private Python <-> Node
 * pipe. The protocol owns no listening socket and no model-visible methods.
 *
 * Closed kinds: ready, request/result, callback/callback_result, control/
 * control_result, cancel, shutdown. Unknown kinds, malformed frames, mismatched
 * IDs, and oversized frames fail closed.
 */

export const PROTOCOL_VERSION = 1
export const MAX_FRAME_BYTES = 1024 * 1024
export const MAX_ID_LENGTH = 128
export const MAX_TASK_ID_LENGTH = 128
export const MAX_CALLBACK_NAME_LENGTH = 64
export const MAX_TASK_DATA_BYTES = 512 * 1024
export const MAX_CALLBACK_DATA_BYTES = 256 * 1024
export const MAX_RESULT_BYTES = 512 * 1024
export const MAX_ERROR_MESSAGE_LENGTH = 200
export const MAX_REASONING_EFFORT_LENGTH = 32
export const MAX_PROMPT_CHARS = 128 * 1024
export const MAX_PENDING_CALLBACKS = 16
export const MAX_CONTROL_ARGS_BYTES = 64 * 1024
export const MAX_CONTROL_RESULT_BYTES = 512 * 1024
const CONTROL_OP_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/

export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }

export type TaskKind = 'chat' | 'summary'
export type AdmissionPurpose = 'conversation' | 'compaction' | 'session-title'

export interface TaskBudget {
  /** Frozen upper bound for model rounds in one task. */
  readonly max_rounds: number
  /** Frozen upper bound for tool calls in one round. */
  readonly max_tool_calls_per_round: number
  /** Frozen upper bound for tool calls across the task. */
  readonly max_total_tool_calls: number
}

export interface ReadyPayload {
  readonly state: 'ready' | 'unavailable'
  readonly protocol_version: number
  readonly host_version: string
  readonly bindings: Readonly<Record<TaskKind, boolean>>
  readonly reason?: string
}

export interface TaskRequestPayload {
  readonly task_id: string
  readonly kind: TaskKind
  readonly task_data: { readonly [key: string]: JsonValue }
  readonly budget: TaskBudget
}

export interface ResultSuccessPayload {
  readonly ok: true
  readonly result: { readonly [key: string]: JsonValue }
}

export interface ResultErrorPayload {
  readonly ok: false
  readonly error: {
    readonly code: string
    readonly message: string
  }
}

export interface CallbackPayload {
  readonly request_id: string
  readonly task_id: string
  readonly name: string
  readonly data: { readonly [key: string]: JsonValue }
}

export interface CallbackResultSuccessPayload {
  readonly ok: true
  readonly result: JsonValue
}

export interface CallbackResultErrorPayload {
  readonly ok: false
  readonly error: {
    readonly code: string
    readonly message: string
  }
}

export interface ControlRequestPayload {
  readonly op: string
  readonly args: { readonly [key: string]: JsonValue }
}

export interface CancelPayload {
  readonly reason: string
}

export interface ShutdownPayload {
  readonly reason: string
}

export type ClientFrame =
  | { readonly v: 1; readonly kind: 'request'; readonly id: string; readonly payload: TaskRequestPayload }
  | { readonly v: 1; readonly kind: 'callback_result'; readonly id: string; readonly payload: CallbackResultSuccessPayload | CallbackResultErrorPayload }
  | { readonly v: 1; readonly kind: 'control'; readonly id: string; readonly payload: ControlRequestPayload }
  | { readonly v: 1; readonly kind: 'cancel'; readonly id: string; readonly payload: CancelPayload }
  | { readonly v: 1; readonly kind: 'shutdown'; readonly id: string; readonly payload: ShutdownPayload }

export type ServerFrame =
  | { readonly v: 1; readonly kind: 'ready'; readonly id: 'ready'; readonly payload: ReadyPayload }
  | { readonly v: 1; readonly kind: 'result'; readonly id: string; readonly payload: ResultSuccessPayload | ResultErrorPayload }
  | { readonly v: 1; readonly kind: 'callback'; readonly id: string; readonly payload: CallbackPayload }
  | { readonly v: 1; readonly kind: 'callback_result'; readonly id: string; readonly payload: CallbackResultSuccessPayload | CallbackResultErrorPayload }
  | { readonly v: 1; readonly kind: 'control_result'; readonly id: string; readonly payload: ResultSuccessPayload | ResultErrorPayload }

export class ProtocolError extends Error {
  readonly code: string

  constructor(code: string) {
    super(code)
    this.name = 'ProtocolError'
    this.code = code
  }
}

const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/
const TASK_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/
const CALLBACK_NAME_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

export function assertJsonValue(value: unknown, depth = 0): asserts value is JsonValue {
  if (depth > 64) throw new ProtocolError('json_too_deep')
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new ProtocolError('json_non_finite')
    return
  }
  if (Array.isArray(value)) {
    for (const item of value) assertJsonValue(item, depth + 1)
    return
  }
  if (isPlainObject(value)) {
    for (const child of Object.values(value)) assertJsonValue(child, depth + 1)
    return
  }
  throw new ProtocolError('json_unsupported')
}

export function deepFreezeJson<T>(value: T): T {
  if (value === null || typeof value !== 'object') return value
  for (const child of Object.values(value as Record<string, unknown>)) deepFreezeJson(child)
  return Object.freeze(value)
}

export function assertRequestId(id: unknown): asserts id is string {
  if (typeof id !== 'string' || !ID_PATTERN.test(id)) throw new ProtocolError('invalid_id')
}

export function assertTaskId(id: unknown): asserts id is string {
  if (typeof id !== 'string' || !TASK_ID_PATTERN.test(id)) throw new ProtocolError('invalid_task_id')
}

export function assertCallbackName(name: unknown): asserts name is string {
  if (typeof name !== 'string' || name.length > MAX_CALLBACK_NAME_LENGTH || !CALLBACK_NAME_PATTERN.test(name)) {
    throw new ProtocolError('invalid_callback_name')
  }
}

export function assertErrorCode(code: unknown): asserts code is string {
  if (typeof code !== 'string' || !ERROR_CODE_PATTERN.test(code)) throw new ProtocolError('invalid_error_code')
}

export function assertErrorPayload(payload: unknown): asserts payload is { readonly code: string; readonly message: string } {
  if (!isPlainObject(payload)) throw new ProtocolError('invalid_error_payload')
  const keys = Object.keys(payload)
  if (keys.length !== 2 || !keys.includes('code') || !keys.includes('message')) throw new ProtocolError('invalid_error_payload')
  assertErrorCode(payload.code)
  if (typeof payload.message !== 'string' || payload.message.length > MAX_ERROR_MESSAGE_LENGTH) {
    throw new ProtocolError('invalid_error_message')
  }
}

function assertExactKeys(value: Record<string, unknown>, keys: readonly string[], code: string): void {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new ProtocolError(code)
  }
}

function parseBudget(value: unknown): TaskBudget {
  if (!isPlainObject(value)) throw new ProtocolError('invalid_budget')
  assertExactKeys(value, ['max_rounds', 'max_tool_calls_per_round', 'max_total_tool_calls'], 'invalid_budget')
  const rounds = value.max_rounds
  const perRound = value.max_tool_calls_per_round
  const total = value.max_total_tool_calls
  for (const [name, candidate] of [
    ['max_rounds', rounds],
    ['max_tool_calls_per_round', perRound],
    ['max_total_tool_calls', total],
  ] as const) {
    if (typeof candidate !== 'number' || !Number.isSafeInteger(candidate) || candidate < 1 || candidate > 1000) {
      throw new ProtocolError(`invalid_budget_${name}`)
    }
  }
  return Object.freeze({
    max_rounds: rounds as number,
    max_tool_calls_per_round: perRound as number,
    max_total_tool_calls: total as number,
  })
}

export function parseTaskRequest(payload: unknown): TaskRequestPayload {
  if (!isPlainObject(payload)) throw new ProtocolError('invalid_request_payload')
  assertExactKeys(payload, ['task_id', 'kind', 'task_data', 'budget'], 'invalid_request_payload')
  assertTaskId(payload.task_id)
  if (payload.kind !== 'chat' && payload.kind !== 'summary') throw new ProtocolError('invalid_task_kind')
  if (!isPlainObject(payload.task_data)) throw new ProtocolError('invalid_task_data')
  assertJsonValue(payload.task_data)
  const serialized = JSON.stringify(payload.task_data)
  if (Buffer.byteLength(serialized, 'utf8') > MAX_TASK_DATA_BYTES) throw new ProtocolError('task_data_too_large')
  const budget = parseBudget(payload.budget)
  return Object.freeze({
    task_id: payload.task_id,
    kind: payload.kind,
    task_data: deepFreezeJson(payload.task_data),
    budget,
  })
}

export function parseControlRequest(payload: unknown): ControlRequestPayload {
  if (!isPlainObject(payload)) throw new ProtocolError('invalid_control_payload')
  assertExactKeys(payload, ['op', 'args'], 'invalid_control_payload')
  const op = payload.op
  if (typeof op !== 'string' || !CONTROL_OP_PATTERN.test(op)) throw new ProtocolError('invalid_control_op')
  if (!isPlainObject(payload.args)) throw new ProtocolError('invalid_control_args')
  assertJsonValue(payload.args)
  const serialized = JSON.stringify(payload.args)
  if (Buffer.byteLength(serialized, 'utf8') > MAX_CONTROL_ARGS_BYTES) {
    throw new ProtocolError('control_args_too_large')
  }
  return Object.freeze({ op, args: deepFreezeJson(payload.args) })
}

export function parseCallbackResult(payload: unknown): CallbackResultSuccessPayload | CallbackResultErrorPayload {
  if (!isPlainObject(payload)) throw new ProtocolError('invalid_callback_result')
  if (payload.ok === true) {
    assertExactKeys(payload, ['ok', 'result'], 'invalid_callback_result')
    assertJsonValue(payload.result)
    const serialized = JSON.stringify(payload.result)
    if (Buffer.byteLength(serialized, 'utf8') > MAX_CALLBACK_DATA_BYTES) throw new ProtocolError('callback_result_too_large')
    return Object.freeze({ ok: true, result: deepFreezeJson(payload.result) })
  }
  if (payload.ok === false) {
    assertExactKeys(payload, ['ok', 'error'], 'invalid_callback_result')
    assertErrorPayload(payload.error)
    return Object.freeze({ ok: false, error: Object.freeze({ code: payload.error.code, message: payload.error.message }) })
  }
  throw new ProtocolError('invalid_callback_result')
}

export function parseClientFrame(line: string): ClientFrame {
  let parsed: unknown
  try {
    parsed = JSON.parse(line) as unknown
  } catch {
    throw new ProtocolError('invalid_json')
  }
  if (!isPlainObject(parsed)) throw new ProtocolError('invalid_envelope')
  assertExactKeys(parsed, ['v', 'kind', 'id', 'payload'], 'invalid_envelope')
  if (parsed.v !== PROTOCOL_VERSION) throw new ProtocolError('unsupported_protocol_version')
  const id = parsed.id
  assertRequestId(id)
  switch (parsed.kind) {
    case 'request':
      return Object.freeze({ v: 1 as const, kind: 'request' as const, id, payload: parseTaskRequest(parsed.payload) })
    case 'callback_result':
      return Object.freeze({ v: 1 as const, kind: 'callback_result' as const, id, payload: parseCallbackResult(parsed.payload) })
    case 'control':
      return Object.freeze({ v: 1 as const, kind: 'control' as const, id, payload: parseControlRequest(parsed.payload) })
    case 'cancel': {
      if (!isPlainObject(parsed.payload)) throw new ProtocolError('invalid_cancel_payload')
      assertExactKeys(parsed.payload, ['reason'], 'invalid_cancel_payload')
      if (typeof parsed.payload.reason !== 'string' || parsed.payload.reason.length > MAX_ERROR_MESSAGE_LENGTH) {
        throw new ProtocolError('invalid_cancel_payload')
      }
      return Object.freeze({ v: 1 as const, kind: 'cancel' as const, id, payload: Object.freeze({ reason: parsed.payload.reason }) })
    }
    case 'shutdown': {
      if (!isPlainObject(parsed.payload)) throw new ProtocolError('invalid_shutdown_payload')
      assertExactKeys(parsed.payload, ['reason'], 'invalid_shutdown_payload')
      if (typeof parsed.payload.reason !== 'string' || parsed.payload.reason.length > MAX_ERROR_MESSAGE_LENGTH) {
        throw new ProtocolError('invalid_shutdown_payload')
      }
      return Object.freeze({ v: 1 as const, kind: 'shutdown' as const, id, payload: Object.freeze({ reason: parsed.payload.reason }) })
    }
    default:
      throw new ProtocolError('unknown_frame_kind')
  }
}

export function encodeFrame(frame: ServerFrame): string {
  const serialized = JSON.stringify(frame)
  if (Buffer.byteLength(serialized, 'utf8') > MAX_FRAME_BYTES) throw new ProtocolError('frame_too_large')
  return `${serialized}\n`
}

export function readyFrame(payload: ReadyPayload): ServerFrame {
  return Object.freeze({ v: 1 as const, kind: 'ready' as const, id: 'ready' as const, payload })
}

export function resultSuccessFrame(id: string, result: { readonly [key: string]: JsonValue }): ServerFrame {
  return Object.freeze({ v: 1 as const, kind: 'result' as const, id, payload: Object.freeze({ ok: true as const, result }) })
}

export function resultErrorFrame(id: string, code: string, message: string): ServerFrame {
  return Object.freeze({
    v: 1 as const,
    kind: 'result' as const,
    id,
    payload: Object.freeze({ ok: false as const, error: Object.freeze({ code, message }) }),
  })
}

export function callbackFrame(id: string, payload: CallbackPayload): ServerFrame {
  return Object.freeze({ v: 1 as const, kind: 'callback' as const, id, payload })
}

export function controlResultSuccessFrame(id: string, result: { readonly [key: string]: JsonValue }): ServerFrame {
  return Object.freeze({ v: 1 as const, kind: 'control_result' as const, id, payload: Object.freeze({ ok: true as const, result }) })
}

export function controlResultErrorFrame(id: string, code: string, message: string): ServerFrame {
  return Object.freeze({
    v: 1 as const,
    kind: 'control_result' as const,
    id,
    payload: Object.freeze({ ok: false as const, error: Object.freeze({ code, message }) }),
  })
}

export function callbackResultSuccessFrame(id: string, result: JsonValue): ServerFrame {
  return Object.freeze({ v: 1 as const, kind: 'callback_result' as const, id, payload: Object.freeze({ ok: true as const, result }) })
}

export function callbackResultErrorFrame(id: string, code: string, message: string): ServerFrame {
  return Object.freeze({
    v: 1 as const,
    kind: 'callback_result' as const,
    id,
    payload: Object.freeze({ ok: false as const, error: Object.freeze({ code, message }) }),
  })
}
