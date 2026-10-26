/** Sanitized stderr helpers shared by boot, the protocol runner, and fatal guards. */
const SECRET_PATTERNS: ReadonlyArray<readonly [RegExp, string]> = [
  [/\bsk-[A-Za-z0-9._-]{8,}\b/g, '[REDACTED]'],
  [/\bBearer\s+[^\s]+/gi, 'Bearer [REDACTED]'],
  [/\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*['"]?[^\s'"]+/gi, '[REDACTED]'],
  [/\/\/[^/@\s]+@/g, '//[REDACTED]@'],
]

export function sanitizeLogText(value: string): string {
  let text = value.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '')
  for (const [pattern, replacement] of SECRET_PATTERNS) text = text.replace(pattern, replacement)
  return text.slice(0, 2000)
}

export function logTo(stream: NodeJS.WritableStream, message: string): void {
  stream.write(`komari-agent: ${sanitizeLogText(message)}\n`)
}

/** Temporarily sanitize direct `process.stderr.write` calls made by official boot helpers. */
export function installSanitizedStderr(): () => void {
  const original = process.stderr.write.bind(process.stderr) as (...args: unknown[]) => boolean
  const patched = ((chunk: string | Uint8Array, ...rest: unknown[]) => {
    const text = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    return original(sanitizeLogText(text), ...rest)
  }) as typeof process.stderr.write
  process.stderr.write = patched
  return () => {
    process.stderr.write = original as typeof process.stderr.write
  }
}
