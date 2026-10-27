/**
 * Task-owned native image attachment store.
 *
 * Python performs the existing safe download and sends only a closed source
 * descriptor. Node pulls exactly those bytes through trusted bounded chunks,
 * hands them to the official rc.2 preparation helper, and retains the
 * normalized bytes only for the owning live task. Official request projection
 * uses one private cache directory per task; no persistent attachment backend
 * is mounted and no path is exposed to the model.
 */
import { createHash, randomUUID } from 'node:crypto'
import { mkdirSync, rmSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import {
  AttachmentError,
  AttachmentStore,
  isImageAdmissionError,
  type ImageAttachmentLimits,
  type ImageAttachmentRef,
  type ImageRequestTarget,
  type RequestImageAttachment,
  type SaveImageAttachment,
  type StoredImageAttachment,
} from '@deepseek-ai/dsh-attachment'
import {
  DEFAULT_IMAGE_COMPRESSION_CONCURRENCY,
  DEFAULT_NORMALIZED_IMAGE_MAX_BYTES,
  DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION,
  DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS,
  prepareImageFile,
  readRequestImageFile,
  type NormalizationPolicy,
  type PreparedImageFile,
} from '@deepseek-ai/dsh-attachment-local'
import z from '@deepseek-ai/schemastery'
import { LlmError, type ImageBlock } from '@deepseek-ai/dsh-llm'
import { MAX_CHAT_IMAGE_BYTES, MAX_CHAT_IMAGE_TOTAL_BYTES, type ChatImageDescriptor } from './chat-input.js'
import { isPlainObject, type JsonValue } from './protocol.js'

export const MAX_TASK_IMAGE_OPERATIONS = 2
export const MAX_RETAINED_NORMALIZED_BYTES = 32 * 1024 * 1024
const MAX_SOURCE_CHUNK_BYTES = 128 * 1024
const MAX_SOURCE_IMAGE_PIXELS = 100_000_000
const MAX_SOURCE_IMAGE_DIMENSION = 100_000_000
const IMAGE_NAME_EXTENSION: Readonly<Record<ChatImageDescriptor['media_type'], string>> = Object.freeze({
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/gif': '.gif',
  'image/webp': '.webp',
})
const IMAGE_MEDIA_TYPES = Object.freeze(['image/jpeg', 'image/png', 'image/gif', 'image/webp'] as const)
const IMAGE_KEYS = Object.freeze(['index', 'origin', 'media_type', 'byte_count', 'sha256'] as const)

export type AttachmentPreparationCode = 'invalid_image_input' | 'image_input_failed' | 'admission_denied'

export class AttachmentPreparationError extends Error {
  readonly code: AttachmentPreparationCode

  constructor(code: AttachmentPreparationCode) {
    super(code)
    this.name = 'AttachmentPreparationError'
    this.code = code
  }
}

class OperationLimiter {
  private active = 0
  private readonly waiters: Array<() => void> = []

  async run<T>(operation: () => Promise<T>): Promise<T> {
    await this.acquire()
    try {
      return await operation()
    } finally {
      this.release()
    }
  }

  private acquire(): Promise<void> {
    if (this.active < MAX_TASK_IMAGE_OPERATIONS) {
      this.active += 1
      return Promise.resolve()
    }
    return new Promise<void>((resolve) => {
      this.waiters.push(() => {
        this.active += 1
        resolve()
      })
    })
  }

  private release(): void {
    this.active -= 1
    const next = this.waiters.shift()
    if (next !== undefined) next()
  }
}

interface OwnedEntry {
  readonly owner: OwnerState
  readonly ref: ImageAttachmentRef
  readonly data: Uint8Array
}

interface OwnerState {
  readonly id: string
  readonly dir: string
  readonly cacheDir: string
  readonly entries: Map<string, OwnedEntry>
  readonly inflight: Set<Promise<unknown>>
  released: boolean
}

export interface ReadImageChunk {
  (imageIndex: number, offset: number, signal: AbortSignal): Promise<JsonValue>
}

export interface Config {
  readonly root: string
}

const ConfigSchema: z<Config> = z.object({
  root: z.string(),
})

function exactKeys(value: Record<string, unknown>, keys: readonly string[], code: AttachmentPreparationCode): void {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new AttachmentPreparationError(code)
  }
}

function integer(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && !Object.is(value, -0) ? value : undefined
}

function validateDescriptors(value: unknown): readonly ChatImageDescriptor[] {
  if (!Array.isArray(value) || value.length > 8) throw new AttachmentPreparationError('invalid_image_input')
  const result: ChatImageDescriptor[] = []
  let previousIndex = -1
  let previousOrigin: ChatImageDescriptor['origin'] | undefined
  let aggregate = 0
  for (const item of value) {
    if (!isPlainObject(item)) throw new AttachmentPreparationError('invalid_image_input')
    exactKeys(item, IMAGE_KEYS, 'invalid_image_input')
    const index = integer(item.index)
    if (index === undefined || index < 0 || index <= previousIndex) throw new AttachmentPreparationError('invalid_image_input')
    const origin = item.origin
    if (origin !== 'quoted' && origin !== 'current') throw new AttachmentPreparationError('invalid_image_input')
    if (previousOrigin === 'current' && origin === 'quoted') throw new AttachmentPreparationError('invalid_image_input')
    const mediaType = item.media_type
    if (typeof mediaType !== 'string' || !(IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType)) {
      throw new AttachmentPreparationError('invalid_image_input')
    }
    const byteCount = integer(item.byte_count)
    if (byteCount === undefined || byteCount <= 0 || byteCount > MAX_CHAT_IMAGE_BYTES) {
      throw new AttachmentPreparationError('invalid_image_input')
    }
    const sha256 = item.sha256
    if (typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)) {
      throw new AttachmentPreparationError('invalid_image_input')
    }
    aggregate += byteCount
    if (aggregate > MAX_CHAT_IMAGE_TOTAL_BYTES) throw new AttachmentPreparationError('invalid_image_input')
    result.push({
      index,
      origin,
      media_type: mediaType as ChatImageDescriptor['media_type'],
      byte_count: byteCount,
      sha256,
    })
    previousIndex = index
    previousOrigin = origin
  }
  return result
}

function canonicalBase64(value: string): Uint8Array | undefined {
  if (value.length === 0 || value.length % 4 !== 0) return undefined
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return undefined
  const decoded = Buffer.from(value, 'base64')
  if (decoded.toString('base64') !== value) return undefined
  return new Uint8Array(decoded)
}

function sameRef(entry: OwnedEntry, ref: ImageAttachmentRef): boolean {
  if (ref.attachmentId !== entry.ref.attachmentId) return false
  if (ref.mediaType !== entry.ref.mediaType) return false
  if (ref.bytes !== entry.ref.bytes) return false
  if (ref.width !== entry.ref.width || ref.height !== entry.ref.height) return false
  if (ref.name !== entry.ref.name) return false
  const actualOriginal = ref.originalDimensions
  const expectedOriginal = entry.ref.originalDimensions
  if (actualOriginal === undefined || expectedOriginal === undefined) return actualOriginal === expectedOriginal
  return actualOriginal.width === expectedOriginal.width && actualOriginal.height === expectedOriginal.height
}

function referenceKey(ref: ImageAttachmentRef): string {
  return `${String(ref.attachmentId)}\u0000${ref.name ?? ''}`
}

function validateRoot(value: string): string {
  if (typeof value !== 'string' || value.length === 0 || !isAbsolute(value)) throw new Error('invalid attachment root')
  return resolve(value)
}

/**
 * One live task's opaque attachment ownership. The store holds normalized
 * source bytes; the session owns only the generated owner token.
 */
export class TaskAttachmentSession {
  private released = false

  constructor(
    private readonly store: TaskAttachmentStore,
    readonly ownerId: string,
  ) {}

  async prepare(
    descriptors: readonly ChatImageDescriptor[],
    readChunk: ReadImageChunk,
    signal: AbortSignal,
  ): Promise<readonly ImageBlock[]> {
    if (this.released) throw new AttachmentPreparationError('invalid_image_input')
    const validated = validateDescriptors(descriptors)
    const blocks: ImageBlock[] = []
    let retained = 0
    for (const descriptor of validated) {
      signal.throwIfAborted()
      const source = await this.readSource(descriptor, readChunk, signal)
      signal.throwIfAborted()
      const name = this.store.imageName(this.ownerId, descriptor)
      let prepared: PreparedImageFile
      try {
        prepared = await this.store.prepare(source, descriptor.media_type, name, signal)
      } catch (error) {
        if (signal.aborted) throw signal.reason
        if (isImageAdmissionError(error)) throw new AttachmentPreparationError('invalid_image_input')
        throw new AttachmentPreparationError('image_input_failed')
      }
      if (
        prepared.ref.bytes !== prepared.data.byteLength
        || prepared.ref.name !== name
        || prepared.ref.mediaType === undefined
      ) {
        throw new AttachmentPreparationError('image_input_failed')
      }
      retained += prepared.data.byteLength
      if (retained > MAX_RETAINED_NORMALIZED_BYTES) throw new AttachmentPreparationError('image_input_failed')
      this.store.register(this.ownerId, prepared)
      blocks.push(Object.freeze({ type: 'image', attachment: prepared.ref }))
    }
    return Object.freeze(blocks)
  }

  async release(): Promise<void> {
    if (this.released) return
    this.released = true
    await this.store.releaseOwner(this.ownerId)
  }

  private async readSource(
    descriptor: ChatImageDescriptor,
    readChunk: ReadImageChunk,
    signal: AbortSignal,
  ): Promise<Uint8Array> {
    let offset = 0
    const chunks: Uint8Array[] = []
    const digest = createHash('sha256')
    while (offset < descriptor.byte_count) {
      signal.throwIfAborted()
      const raw = await readChunk(descriptor.index, offset, signal)
      signal.throwIfAborted()
      if (!isPlainObject(raw)) throw new AttachmentPreparationError('invalid_image_input')
      exactKeys(raw, ['image_index', 'offset', 'data', 'eof'], 'invalid_image_input')
      const imageIndex = integer(raw.image_index)
      const echoedOffset = integer(raw.offset)
      if (imageIndex !== descriptor.index || echoedOffset !== offset) {
        throw new AttachmentPreparationError('invalid_image_input')
      }
      if (typeof raw.data !== 'string' || typeof raw.eof !== 'boolean') {
        throw new AttachmentPreparationError('invalid_image_input')
      }
      const chunk = canonicalBase64(raw.data)
      if (chunk === undefined || chunk.byteLength > MAX_SOURCE_CHUNK_BYTES || chunk.byteLength === 0) {
        throw new AttachmentPreparationError('invalid_image_input')
      }
      if (offset + chunk.byteLength > descriptor.byte_count) {
        throw new AttachmentPreparationError('invalid_image_input')
      }
      const reachesEnd = offset + chunk.byteLength === descriptor.byte_count
      if (raw.eof !== reachesEnd) throw new AttachmentPreparationError('invalid_image_input')
      chunks.push(chunk)
      digest.update(chunk)
      offset += chunk.byteLength
    }
    const data = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), descriptor.byte_count)
    if (data.byteLength !== descriptor.byte_count || digest.digest('hex') !== descriptor.sha256) {
      throw new AttachmentPreparationError('invalid_image_input')
    }
    return new Uint8Array(data)
  }
}

/**
 * Root project AttachmentStore. All mutations/reads are bound to one live task;
 * foreign, fabricated, and released references fail closed.
 */
export class TaskAttachmentStore extends AttachmentStore {
  static Config = ConfigSchema

  readonly imageLimits: ImageAttachmentLimits = Object.freeze({
    maxImageBytes: MAX_CHAT_IMAGE_BYTES,
    maxImagesPerMessage: 8,
    maxMessageImageBytes: MAX_CHAT_IMAGE_TOTAL_BYTES,
    maxImagePixels: MAX_SOURCE_IMAGE_PIXELS,
    // The accepted chat downloader has no independent source-side dimension
    // cap; do not introduce rc.2's 8192px default as a new business rule.
    maxImageDimension: MAX_SOURCE_IMAGE_DIMENSION,
    mediaTypes: IMAGE_MEDIA_TYPES,
  })

  readonly normalizationPolicy: Readonly<NormalizationPolicy> = Object.freeze({
    maxPixels: DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS,
    maxDimension: DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION,
    maxBytes: DEFAULT_NORMALIZED_IMAGE_MAX_BYTES,
  })

  readonly imageCompressionConcurrency = DEFAULT_IMAGE_COMPRESSION_CONCURRENCY

  private readonly root: string
  private readonly owners = new Map<string, OwnerState>()
  private readonly entries = new Map<string, OwnedEntry>()
  private readonly operations = new OperationLimiter()

  constructor(ctx: Context, config: Config) {
    super(ctx)
    this.root = validateRoot(config.root)
    mkdirSync(this.root, { recursive: true, mode: 0o700 })
  }

  createSession(): TaskAttachmentSession {
    const id = randomUUID().replaceAll('-', '')
    const dir = join(this.root, id)
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    const owner: OwnerState = {
      id,
      dir,
      cacheDir: join(dir, 'request-cache'),
      entries: new Map(),
      inflight: new Set(),
      released: false,
    }
    this.owners.set(id, owner)
    return new TaskAttachmentSession(this, id)
  }

  override async validateImage(input: SaveImageAttachment): Promise<void> {
    await this.operations.run(() => prepareImageFile(input, this.imageLimits, this.normalizationPolicy))
  }

  override async saveImages(_inputs: readonly SaveImageAttachment[]): Promise<readonly ImageAttachmentRef[]> {
    throw new AttachmentError('Task attachment writes require an owned image session.', 'INVALID_ATTACHMENT_REF')
  }

  override async saveImage(_input: SaveImageAttachment): Promise<ImageAttachmentRef> {
    throw new AttachmentError('Task attachment writes require an owned image session.', 'INVALID_ATTACHMENT_REF')
  }

  override async readImage(ref: ImageAttachmentRef, signal?: AbortSignal): Promise<StoredImageAttachment> {
    signal?.throwIfAborted()
    const entry = this.entryFor(ref)
    return { ref: entry.ref, data: entry.data }
  }

  override imageHostPath(_ref: ImageAttachmentRef): string | undefined {
    return undefined
  }

  override async readImageRequest(
    ref: ImageAttachmentRef,
    target: ImageRequestTarget,
    signal?: AbortSignal,
  ): Promise<RequestImageAttachment> {
    signal?.throwIfAborted()
    const entry = this.entryFor(ref)
    const operation = this.operations.run(() => readRequestImageFile(
      entry.owner.cacheDir,
      { ref: entry.ref, data: entry.data },
      target,
      signal,
    ))
    return this.track(entry.owner, operation).catch((error: unknown) => {
      if (signal?.aborted === true) throw signal.reason
      // The provider adapter normalizes thrown errors to its own failure
      // vocabulary. A fixed LlmError code keeps a verified image's official
      // projection failure distinct from a terminal provider request failure.
      throw new LlmError(
        'Image input projection failed.',
        'IMAGE_INPUT_FAILED',
        { cause: error },
      )
    })
  }

  /** Called once per task after its agent has been disposed. */
  async releaseOwner(ownerId: string): Promise<void> {
    const owner = this.owners.get(ownerId)
    if (owner === undefined) return
    owner.released = true
    this.owners.delete(ownerId)
    for (const [key, entry] of this.entries) {
      if (entry.owner.id === ownerId) this.entries.delete(key)
    }
    await Promise.allSettled([...owner.inflight])
    rmSync(owner.dir, { recursive: true, force: true })
    owner.entries.clear()
  }

  /** Trusted preparation seam used only by TaskAttachmentSession. */
  async prepare(
    data: Uint8Array,
    mediaType: ChatImageDescriptor['media_type'],
    name: string,
    signal: AbortSignal,
  ): Promise<PreparedImageFile> {
    signal.throwIfAborted()
    return this.operations.run(async () => {
      signal.throwIfAborted()
      return prepareImageFile({ data, mediaType, name }, this.imageLimits, this.normalizationPolicy)
    })
  }

  imageName(ownerId: string, descriptor: ChatImageDescriptor): string {
    return `image-${ownerId.slice(0, 16)}-${descriptor.index}${IMAGE_NAME_EXTENSION[descriptor.media_type]}`
  }

  register(ownerId: string, prepared: PreparedImageFile): void {
    const owner = this.owners.get(ownerId)
    if (owner === undefined || owner.released) throw new AttachmentPreparationError('image_input_failed')
    if (prepared.ref.name === undefined) throw new AttachmentPreparationError('image_input_failed')
    const key = referenceKey(prepared.ref)
    if (this.entries.has(key)) throw new AttachmentPreparationError('image_input_failed')
    const entry: OwnedEntry = { owner, ref: prepared.ref, data: prepared.data }
    owner.entries.set(key, entry)
    this.entries.set(key, entry)
  }

  private entryFor(ref: ImageAttachmentRef): OwnedEntry {
    if (typeof ref !== 'object' || ref === null) {
      throw new AttachmentError('Attachment reference is invalid.', 'INVALID_ATTACHMENT_REF')
    }
    const entry = this.entries.get(referenceKey(ref))
    if (entry === undefined || entry.owner.released || !sameRef(entry, ref)) {
      throw new AttachmentError('Attachment reference is not owned by a live task.', 'INVALID_ATTACHMENT_REF')
    }
    return entry
  }

  private async track<T>(owner: OwnerState, operation: Promise<T>): Promise<T> {
    owner.inflight.add(operation)
    try {
      return await operation
    } finally {
      owner.inflight.delete(operation)
    }
  }
}

export default TaskAttachmentStore
