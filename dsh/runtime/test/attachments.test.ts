import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { startHost } from '../dist/host.js'
import { AttachmentPreparationError, type TaskAttachmentStore } from '../dist/attachments.js'
import type { ChatImageDescriptor } from '../dist/chat-input.js'
import type { JsonValue } from '../dist/protocol.js'

const DSH_PACKAGE = fileURLToPath(new URL('../../package.json', import.meta.url))
const DSH_BASE_CONFIG = fileURLToPath(new URL('../../compose/cordis.yml', import.meta.url))
const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)

async function withStore(fn: (store: TaskAttachmentStore, root: string) => Promise<void>): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'komari-attachments-test-'))
  const profile = join(root, 'profile')
  mkdirSync(profile, { recursive: true })
  writeFileSync(join(profile, 'package.json'), `${JSON.stringify({
    name: 'komari-attachments-test',
    private: true,
    dsh: { profile: { bundles: ['@komari-bot/dsh-runtime-bundle'] } },
  }, null, 2)}\n`)
  writeFileSync(join(profile, 'cordis.patch.yml'), '[]\n')
  const runtime = await startHost({
    profileDir: profile,
    installAnchor: DSH_PACKAGE,
    baseConfig: DSH_BASE_CONFIG,
    dshHome: join(root, 'home'),
    attachmentRoot: join(root, 'attachments'),
    sendFrame: async () => undefined,
  })
  try {
    await fn(runtime.ctx.get('attachments') as TaskAttachmentStore, root)
  } finally {
    await runtime.close()
    rmSync(root, { recursive: true, force: true })
  }
}

function descriptor(data: Buffer, overrides: Partial<ChatImageDescriptor> = {}): ChatImageDescriptor {
  return {
    index: 0,
    origin: 'current',
    media_type: 'image/png',
    byte_count: data.byteLength,
    sha256: createHash('sha256').update(data).digest('hex'),
    ...overrides,
  }
}

function chunksFor(data: Buffer, options: { readonly offsetShift?: number; readonly digestOverride?: string } = {}) {
  return async (imageIndex: number, offset: number): Promise<JsonValue> => {
    const start = offset
    const end = Math.min(data.byteLength, start + 128 * 1024)
    if (start >= end) throw new Error('bad test offset')
    return {
      image_index: imageIndex,
      offset: offset + (options.offsetShift ?? 0),
      data: data.subarray(start, end).toString('base64'),
      eof: end === data.byteLength,
    }
  }
}

test('task attachment store prepares, projects, and isolates equal-content owners', async () => {
  await withStore(async (store, root) => {
    const first = store.createSession()
    const second = store.createSession()
    const blocksA = await first.prepare(
      [descriptor(PNG_1X1)],
      chunksFor(PNG_1X1),
      new AbortController().signal,
    )
    const blocksB = await second.prepare(
      [descriptor(PNG_1X1)],
      chunksFor(PNG_1X1),
      new AbortController().signal,
    )
    const refA = blocksA[0]?.attachment
    const refB = blocksB[0]?.attachment
    assert.ok(refA !== undefined && refB !== undefined)
    assert.notEqual(refA.name, refB.name)
    assert.equal(store.imageHostPath(refA), undefined)

    const projectedA = await store.readImageRequest(refA, { width: 64, height: 64, maxBytes: 1024 * 1024 })
    const projectedB = await store.readImageRequest(refB, { width: 64, height: 64, maxBytes: 1024 * 1024 })
    assert.equal(projectedA.attachment.attachmentId, projectedB.attachment.attachmentId)
    assert.equal(projectedA.mediaType, 'image/png')
    assert.ok(existsSync(join(root, 'attachments', first.ownerId)))

    await first.release()
    assert.equal(existsSync(join(root, 'attachments', first.ownerId)), false)
    await assert.rejects(
      store.readImageRequest(refA, { width: 64, height: 64, maxBytes: 1024 * 1024 }),
      { code: 'INVALID_ATTACHMENT_REF' },
    )
    await assert.rejects(store.readImage(refA), { code: 'INVALID_ATTACHMENT_REF' })
    const stillB = await store.readImageRequest(refB, { width: 64, height: 64, maxBytes: 1024 * 1024 })
    assert.ok(stillB.bytes > 0)
    await second.release()
  })
})

test('malformed chunk echo, base64, EOF, and digest fail closed', async () => {
  await withStore(async (store) => {
    const session = store.createSession()
    const signal = new AbortController().signal
    await assert.rejects(
      session.prepare([descriptor(PNG_1X1)], chunksFor(PNG_1X1, { offsetShift: 1 }), signal),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    const badDigest = descriptor(PNG_1X1, { sha256: '0'.repeat(64) })
    await assert.rejects(
      session.prepare([badDigest], chunksFor(PNG_1X1), signal),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    await assert.rejects(
      session.prepare(
        [descriptor(PNG_1X1, { media_type: 'image/jpeg' })],
        chunksFor(PNG_1X1),
        signal,
      ),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    await assert.rejects(
      session.prepare([descriptor(PNG_1X1)], async (imageIndex, offset) => ({
        image_index: imageIndex,
        offset,
        data: '!!!not-base64!!!',
        eof: true,
      }), signal),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    await assert.rejects(
      session.prepare([descriptor(PNG_1X1)], async (imageIndex, offset) => {
        const value = {
          image_index: imageIndex,
          offset,
          data: PNG_1X1.toString('base64'),
          eof: true,
        }
        return { ...value, data: value.data.slice(0, -4) }
      }, signal),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    await session.release()
  })
})

test('closed decoder shape rejects duplicate and misordered descriptors', async () => {
  await withStore(async (store) => {
    const session = store.createSession()
    const signal = new AbortController().signal
    const duplicate = [
      descriptor(PNG_1X1, { index: 0 }),
      descriptor(PNG_1X1, { index: 0 }),
    ]
    await assert.rejects(
      session.prepare(duplicate, chunksFor(PNG_1X1), signal),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    const misordered = [
      descriptor(PNG_1X1, { index: 0, origin: 'current' }),
      descriptor(PNG_1X1, { index: 1, origin: 'quoted' }),
    ]
    await assert.rejects(
      session.prepare(misordered, chunksFor(PNG_1X1), signal),
      (error: unknown) => error instanceof AttachmentPreparationError && error.code === 'invalid_image_input',
    )
    await session.release()
  })
})


test('cancelled chunk transfer releases the owner without leaving a late cache', async () => {
  await withStore(async (store) => {
    const session = store.createSession()
    const controller = new AbortController()
    let markStarted: (() => void) | undefined
    const started = new Promise<void>((resolve) => {
      markStarted = resolve
    })
    const preparation = session.prepare(
      [descriptor(PNG_1X1)],
      async (_imageIndex, _offset, signal) => {
        markStarted?.()
        await new Promise<never>((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(signal.reason), { once: true })
        })
        throw new Error('unreachable')
      },
      controller.signal,
    )
    await started
    controller.abort(new Error('deterministic transfer cancel'))
    await assert.rejects(preparation)
    await session.release()
    assert.equal(existsSync(join(storeRoot(store), session.ownerId)), false)
  })
})

function storeRoot(store: TaskAttachmentStore): string {
  // The test helper passes the attachment root as a sibling of the profile;
  // recover it from the prepared owner entries through a live session dir.
  const runtimeRoot = (store as unknown as { root: string }).root
  return runtimeRoot
}


test('owner release waits for an in-flight projection before deleting its cache', async () => {
  await withStore(async (store, root) => {
    const session = store.createSession()
    const blocks = await session.prepare(
      [descriptor(PNG_1X1)],
      chunksFor(PNG_1X1),
      new AbortController().signal,
    )
    const ref = blocks[0]?.attachment
    assert.ok(ref !== undefined)
    const projection = store.readImageRequest(ref, { width: 64, height: 64, maxBytes: 1024 * 1024 })
    const release = session.release()
    const [version] = await Promise.all([projection, release])
    assert.ok(version.bytes > 0)
    assert.equal(existsSync(join(root, 'attachments', session.ownerId)), false)
  })
})

test('aborted projection cannot write a cache after successful owner cleanup', async () => {
  await withStore(async (store, root) => {
    const session = store.createSession()
    const blocks = await session.prepare(
      [descriptor(PNG_1X1)],
      chunksFor(PNG_1X1),
      new AbortController().signal,
    )
    const ref = blocks[0]?.attachment
    assert.ok(ref !== undefined)
    const controller = new AbortController()
    const projection = store.readImageRequest(
      ref,
      { width: 64, height: 64, maxBytes: 1024 * 1024 },
      controller.signal,
    )
    controller.abort(new Error('deterministic projection cancel'))
    await assert.rejects(projection)
    await session.release()
    assert.equal(existsSync(join(root, 'attachments', session.ownerId)), false)
  })
})
