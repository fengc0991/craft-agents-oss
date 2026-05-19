import { readFile, writeFile, unlink, mkdir, readdir, stat } from 'fs/promises'
import { isAbsolute, join, resolve, dirname, parse as parsePath } from 'path'
import { homedir } from 'os'
import { gzip } from 'zlib'
import { promisify } from 'util'
import { validatePathFormat } from '../../utils/path-validation'
import { randomUUID } from 'crypto'
import { RPC_CHANNELS, type FileAttachment, type DirectoryListingResult, type DownloadPathResult } from '@craft-agent/shared/protocol'
import type { StoredAttachment } from '@craft-agent/core/types'
import { readFileAttachment, validateImageForClaudeAPI, IMAGE_LIMITS } from '@craft-agent/shared/utils'
import { getSessionAttachmentsPath, validateSessionId, MAX_BUNDLE_SIZE_BYTES } from '@craft-agent/shared/sessions'
import { getWorkspaceByNameOrId } from '@craft-agent/shared/config'
import { resizeImageForAPI, inspectImageBuffer } from '@craft-agent/server-core/services'
import { sanitizeFilename, validateFilePath, getWorkspaceAllowedDirs } from '@craft-agent/server-core/handlers'
import { MarkItDown } from 'markitdown-js'
import type { RpcServer } from '@craft-agent/server-core/transport'
import type { HandlerDeps } from '../handler-deps'
import { requestClientOpenFileDialog } from '@craft-agent/server-core/transport'

export const HANDLED_CHANNELS = [
  RPC_CHANNELS.file.READ,
  RPC_CHANNELS.file.READ_DATA_URL,
  RPC_CHANNELS.file.READ_PREVIEW_DATA_URL,
  RPC_CHANNELS.file.READ_BINARY,
  RPC_CHANNELS.file.DOWNLOAD_PATH,
  RPC_CHANNELS.file.OPEN_DIALOG,
  RPC_CHANNELS.file.READ_ATTACHMENT,
  RPC_CHANNELS.file.READ_USER_ATTACHMENT,
  RPC_CHANNELS.file.STORE_ATTACHMENT,
  RPC_CHANNELS.file.GENERATE_THUMBNAIL,
  RPC_CHANNELS.fs.SEARCH,
  RPC_CHANNELS.fs.LIST_DIRECTORY,
] as const

const gzipAsync = promisify(gzip)

function getDownloadMimeType(path: string): string {
  const ext = parsePath(path).ext.toLowerCase()
  const mimeMap: Record<string, string> = {
    '.avif': 'image/avif',
    '.bmp': 'image/bmp',
    '.css': 'text/css',
    '.csv': 'text/csv',
    '.gif': 'image/gif',
    '.html': 'text/html',
    '.ico': 'image/x-icon',
    '.jpeg': 'image/jpeg',
    '.jpg': 'image/jpeg',
    '.js': 'text/javascript',
    '.json': 'application/json',
    '.md': 'text/markdown',
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.txt': 'text/plain',
    '.webp': 'image/webp',
    '.xml': 'application/xml',
    '.yaml': 'application/yaml',
    '.yml': 'application/yaml',
    '.zip': 'application/zip',
  }
  return mimeMap[ext] ?? 'application/octet-stream'
}

function tarPathFor(rootName: string, relativePath: string): string {
  return `${rootName}/${relativePath}`.replace(/\\/g, '/')
}

function splitTarPath(path: string): { name: string; prefix: string } {
  if (Buffer.byteLength(path) <= 100) {
    return { name: path, prefix: '' }
  }

  const parts = path.split('/')
  for (let i = 1; i < parts.length; i += 1) {
    const prefix = parts.slice(0, i).join('/')
    const name = parts.slice(i).join('/')
    if (Buffer.byteLength(prefix) <= 155 && Buffer.byteLength(name) <= 100) {
      return { name, prefix }
    }
  }

  throw new Error(`Path is too long to archive: ${path}`)
}

function writeTarString(header: Buffer, value: string, offset: number, length: number): void {
  const bytes = Buffer.from(value)
  bytes.copy(header, offset, 0, Math.min(bytes.length, length))
}

function writeTarOctal(header: Buffer, value: number, offset: number, length: number): void {
  const text = Math.floor(value).toString(8).padStart(length - 1, '0').slice(-(length - 1))
  writeTarString(header, `${text}\0`, offset, length)
}

function createTarHeader(
  path: string,
  size: number,
  mode: number,
  mtimeMs: number,
  typeflag: '0' | '5' = '0',
): Buffer {
  const header = Buffer.alloc(512)
  const { name, prefix } = splitTarPath(path)

  writeTarString(header, name, 0, 100)
  writeTarOctal(header, mode & 0o777, 100, 8)
  writeTarOctal(header, 0, 108, 8)
  writeTarOctal(header, 0, 116, 8)
  writeTarOctal(header, size, 124, 12)
  writeTarOctal(header, Math.floor(mtimeMs / 1000), 136, 12)
  header.fill(0x20, 148, 156)
  writeTarString(header, typeflag, 156, 1)
  writeTarString(header, 'ustar\0', 257, 6)
  writeTarString(header, '00', 263, 2)
  writeTarString(header, prefix, 345, 155)

  let checksum = 0
  for (const byte of header) checksum += byte
  writeTarString(header, `${checksum.toString(8).padStart(6, '0')}\0 `, 148, 8)

  return header
}

function tarPadding(size: number): Buffer {
  const remainder = size % 512
  return remainder === 0 ? Buffer.alloc(0) : Buffer.alloc(512 - remainder)
}

async function collectDirectoryForTar(
  rootPath: string,
  rootName: string,
  allowedDirs: string[],
): Promise<Buffer> {
  const chunks: Buffer[] = []
  let totalSize = 0

  const rootInfo = await stat(rootPath)
  chunks.push(createTarHeader(`${rootName}/`, 0, rootInfo.mode, rootInfo.mtimeMs, '5'))

  const walk = async (currentDir: string, relativeDir: string): Promise<void> => {
    const entries = (await readdir(currentDir, { withFileTypes: true }))
      .filter((entry) => !entry.name.startsWith('.'))
      .sort((a, b) => a.name.localeCompare(b.name))

    for (const entry of entries) {
      const absolutePath = join(currentDir, entry.name)
      const relativePath = relativeDir ? `${relativeDir}/${entry.name}` : entry.name

      if (entry.isDirectory()) {
        let safeChildPath: string
        try {
          safeChildPath = await validateFilePath(absolutePath, allowedDirs)
        } catch {
          continue
        }
        const info = await stat(safeChildPath)
        chunks.push(createTarHeader(tarPathFor(rootName, `${relativePath}/`), 0, info.mode, info.mtimeMs, '5'))
        await walk(safeChildPath, relativePath)
        continue
      }

      if (!entry.isFile()) continue

      let safeChildPath: string
      try {
        safeChildPath = await validateFilePath(absolutePath, allowedDirs)
      } catch {
        continue
      }

      const info = await stat(safeChildPath)
      totalSize += info.size
      if (totalSize > MAX_BUNDLE_SIZE_BYTES) {
        throw new Error(`Download exceeds ${Math.round(MAX_BUNDLE_SIZE_BYTES / 1024 / 1024)}MB limit`)
      }

      const data = await readFile(safeChildPath)
      const archivePath = tarPathFor(rootName, relativePath)
      chunks.push(createTarHeader(archivePath, data.length, info.mode, info.mtimeMs))
      chunks.push(data)
      chunks.push(tarPadding(data.length))
    }
  }

  await walk(rootPath, '')
  chunks.push(Buffer.alloc(1024))
  return Buffer.concat(chunks)
}

export function registerFilesHandlers(server: RpcServer, deps: HandlerDeps): void {
  // Read a file (with path validation to prevent traversal attacks)
  server.handle(RPC_CHANNELS.file.READ, async (ctx, path: string) => {
    try {
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      const safePath = await validateFilePath(path, getWorkspaceAllowedDirs(workspaceId))
      const content = await readFile(safePath, 'utf-8')
      return content
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      // ENOENT is expected for optional config files (e.g. automations.json)
      if (error instanceof Error && 'code' in error && (error as NodeJS.ErrnoException).code === 'ENOENT') {
        deps.platform.logger.debug('readFile: file not found:', path)
      } else {
        deps.platform.logger.error('readFile error:', path, message)
      }
      throw new Error(`Failed to read file: ${message}`)
    }
  })

  // Read an image file as a data URL for in-app image preview overlays.
  // Returns data:{mime};base64,{content} — used by ImagePreviewOverlay and markdown image blocks.
  server.handle(RPC_CHANNELS.file.READ_DATA_URL, async (ctx, path: string) => {
    try {
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      const safePath = await validateFilePath(path, getWorkspaceAllowedDirs(workspaceId))
      const buffer = await readFile(safePath)
      const ext = safePath.split('.').pop()?.toLowerCase() ?? ''

      // Map previewable image extensions to MIME types.
      // HEIC/HEIF/TIFF are intentionally excluded — no Chromium codec, opened externally instead.
      const mimeMap: Record<string, string> = {
        png: 'image/png',
        jpg: 'image/jpeg',
        jpeg: 'image/jpeg',
        gif: 'image/gif',
        webp: 'image/webp',
        svg: 'image/svg+xml',
        bmp: 'image/bmp',
        ico: 'image/x-icon',
        avif: 'image/avif',
      }
      const mime = mimeMap[ext] || 'application/octet-stream'
      const base64 = buffer.toString('base64')
      return `data:${mime};base64,${base64}`
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      deps.platform.logger.error('readFileDataUrl error:', message)
      throw new Error(`Failed to read file as data URL: ${message}`)
    }
  })

  // Read an image file as a small preview data URL for lightweight thumbnail rendering.
  // Returns a PNG data URL resized to fit within maxSize×maxSize.
  server.handle(RPC_CHANNELS.file.READ_PREVIEW_DATA_URL, async (ctx, path: string, maxSize = 64) => {
    try {
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      const safePath = await validateFilePath(path, getWorkspaceAllowedDirs(workspaceId))
      const size = Number.isFinite(maxSize) ? Math.max(16, Math.min(256, Math.floor(maxSize))) : 64
      const preview = await deps.platform.imageProcessor.process(safePath, {
        resize: { width: size, height: size },
        fit: 'inside',
        format: 'png',
      })
      return `data:image/png;base64,${preview.toString('base64')}`
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      deps.platform.logger.error('readFilePreviewDataUrl error:', message)
      throw new Error(`Failed to read file preview: ${message}`)
    }
  })

  // Read a file as raw binary (Uint8Array) for react-pdf.
  // The WS transport codec preserves Uint8Array payloads over JSON envelopes.
  server.handle(RPC_CHANNELS.file.READ_BINARY, async (ctx, path: string) => {
    try {
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      const safePath = await validateFilePath(path, getWorkspaceAllowedDirs(workspaceId))
      const buffer = await readFile(safePath)
      // Return as Uint8Array (serializes to ArrayBuffer over IPC)
      return new Uint8Array(buffer)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      deps.platform.logger.error('readFileBinary error:', message)
      throw new Error(`Failed to read file as binary: ${message}`)
    }
  })

  // Prepare a file or directory for browser download.
  // Files are returned as-is. Directories are streamed back as a gzip-compressed tar archive.
  server.handle(RPC_CHANNELS.file.DOWNLOAD_PATH, async (ctx, path: string): Promise<DownloadPathResult> => {
    try {
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      const allowedDirs = getWorkspaceAllowedDirs(workspaceId)
      const safePath = await validateFilePath(path, allowedDirs)
      const info = await stat(safePath)

      if (info.isDirectory()) {
        const baseName = sanitizeFilename(parsePath(safePath).base || 'download')
        const tarBuffer = await collectDirectoryForTar(safePath, baseName, allowedDirs)
        const gzipped = await gzipAsync(tarBuffer)
        return {
          filename: `${baseName}.tar.gz`,
          mimeType: 'application/gzip',
          data: new Uint8Array(gzipped),
        }
      }

      if (!info.isFile()) {
        throw new Error('Only regular files and directories can be downloaded')
      }

      if (info.size > MAX_BUNDLE_SIZE_BYTES) {
        throw new Error(`Download exceeds ${Math.round(MAX_BUNDLE_SIZE_BYTES / 1024 / 1024)}MB limit`)
      }

      const buffer = await readFile(safePath)
      return {
        filename: sanitizeFilename(parsePath(safePath).base || 'download'),
        mimeType: getDownloadMimeType(safePath),
        data: new Uint8Array(buffer),
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      deps.platform.logger.error('downloadPath error:', path, message)
      throw new Error(`Failed to prepare download: ${message}`)
    }
  })

  // Open native file dialog for selecting files to attach (routed to client)
  server.handle(RPC_CHANNELS.file.OPEN_DIALOG, async (ctx) => {
    const result = await requestClientOpenFileDialog(server, ctx.clientId, {
      properties: ['openFile', 'multiSelections'],
      filters: [
        // Allow all files by default - the agent can figure out how to handle them
        { name: 'All Files', extensions: ['*'] },
        { name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif'] },
        { name: 'Documents', extensions: ['pdf', 'docx', 'xlsx', 'pptx', 'doc', 'xls', 'ppt', 'txt', 'md', 'rtf'] },
        { name: 'Code', extensions: ['js', 'ts', 'tsx', 'jsx', 'py', 'json', 'css', 'html', 'xml', 'yaml', 'yml', 'sh', 'sql', 'go', 'rs', 'rb', 'php', 'java', 'c', 'cpp', 'h', 'swift', 'kt'] },
      ]
    })
    return result.canceled ? [] : result.filePaths
  })

  // Read file and return as FileAttachment with Quick Look thumbnail
  server.handle(RPC_CHANNELS.file.READ_ATTACHMENT, async (ctx, path: string) => {
    try {
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      const safePath = await validateFilePath(path, getWorkspaceAllowedDirs(workspaceId))
      // Use shared utility that handles file type detection, encoding, etc.
      const attachment = await readFileAttachment(safePath)
      if (!attachment) return null

      // Generate thumbnail for image preview
      // Only works for image formats the processor supports — PDFs/Office files get icon fallback
      try {
        const thumbBuffer = await deps.platform.imageProcessor.process(safePath, {
          resize: { width: 200, height: 200 },
          format: 'png',
        })
        ;(attachment as { thumbnailBase64?: string }).thumbnailBase64 = thumbBuffer.toString('base64')
      } catch (thumbError) {
        // Thumbnail generation failed (non-image file or corrupt) — icon fallback
        deps.platform.logger.info('Thumbnail generation failed (using fallback):', thumbError instanceof Error ? thumbError.message : thumbError)
      }

      return attachment
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      deps.platform.logger.error('readFileAttachment error:', message)
      return null
    }
  })

  // Read a user-attached file (bypasses workspace-dir validation).
  // Used only by renderer draft hydration: the path was written to drafts.json by a
  // previous user-initiated OS-picker / Finder-drag attach, so the path implies consent.
  // NOT exposed to agent code — no equivalent MCP tool. Kept separate from readFileAttachment
  // on purpose to preserve the agent-facing read's narrow trust boundary.
  const USER_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
  server.handle(RPC_CHANNELS.file.READ_USER_ATTACHMENT, async (_ctx, path: string) => {
    try {
      if (!path || typeof path !== 'string' || !isAbsolute(path)) return null
      const info = await stat(path).catch(() => null)
      if (!info || !info.isFile()) return null
      if (info.size > USER_ATTACHMENT_MAX_BYTES) {
        deps.platform.logger.warn(`[readUserAttachment] file exceeds ${USER_ATTACHMENT_MAX_BYTES} bytes, skipping: ${path}`)
        return null
      }
      const attachment = readFileAttachment(path)
      if (!attachment) return null
      try {
        const thumbBuffer = await deps.platform.imageProcessor.process(path, {
          resize: { width: 200, height: 200 },
          format: 'png',
        })
        ;(attachment as { thumbnailBase64?: string }).thumbnailBase64 = thumbBuffer.toString('base64')
      } catch {
        // Non-image or corrupt — icon fallback, same as readFileAttachment
      }
      return attachment
    } catch (error) {
      deps.platform.logger.error('readUserAttachment error:', error instanceof Error ? error.message : error)
      return null
    }
  })

  // Generate thumbnail from base64 data (for drag-drop files where we don't have a path)
  server.handle(RPC_CHANNELS.file.GENERATE_THUMBNAIL, async (_ctx, base64: string, _mimeType: string): Promise<string | null> => {
    try {
      const buffer = Buffer.from(base64, 'base64')
      const thumbBuffer = await deps.platform.imageProcessor.process(buffer, {
        resize: { width: 200, height: 200 },
        format: 'png',
      })
      return thumbBuffer.toString('base64')
    } catch (error) {
      deps.platform.logger.info('generateThumbnail failed:', error instanceof Error ? error.message : error)
      return null
    }
  })

  // Store an attachment to disk and generate thumbnail/markdown conversion
  // This is the core of the persistent file attachment system
  server.handle(RPC_CHANNELS.file.STORE_ATTACHMENT, async (ctx, sessionId: string, attachment: FileAttachment): Promise<StoredAttachment> => {
    // Track files we've written for cleanup on error
    const filesToCleanup: string[] = []

    try {
      // Reject empty files early
      if (attachment.size === 0) {
        throw new Error('Cannot attach empty file')
      }

      // Get workspace slug from the calling window
      const workspaceId = ctx.workspaceId ?? deps.windowManager?.getWorkspaceForWindow(ctx.webContentsId!)
      if (!workspaceId) {
        throw new Error('Cannot determine workspace for attachment storage')
      }
      const workspace = getWorkspaceByNameOrId(workspaceId)
      if (!workspace) {
        throw new Error(`Workspace not found: ${workspaceId}`)
      }
      const workspaceRootPath = workspace.rootPath

      // SECURITY: Validate sessionId to prevent path traversal attacks
      // This must happen before using sessionId in any file path operations
      validateSessionId(sessionId)

      // Create attachments directory if it doesn't exist
      const attachmentsDir = getSessionAttachmentsPath(workspaceRootPath, sessionId)
      await mkdir(attachmentsDir, { recursive: true })

      // Generate unique ID for this attachment
      const id = randomUUID()
      const safeName = sanitizeFilename(attachment.name)
      const storedFileName = `${id}_${safeName}`
      const storedPath = join(attachmentsDir, storedFileName)

      // Track if image was resized (for return value)
      let wasResized = false
      let finalSize = attachment.size
      let resizedBase64: string | undefined

      // 1. Save the file (with image validation and resizing)
      if (attachment.base64) {
        // Images, PDFs, Office files - decode from base64
        let decoded: Buffer = Buffer.from(attachment.base64, 'base64')
        // Validate decoded size matches expected (allow small variance for encoding overhead)
        if (Math.abs(decoded.length - attachment.size) > 100) {
          throw new Error(`Attachment corrupted: size mismatch (expected ${attachment.size}, got ${decoded.length})`)
        }

        // For images: validate and resize if needed for Claude API compatibility
        if (attachment.type === 'image') {
          const imageInspection = await inspectImageBuffer(decoded, deps.platform.imageProcessor)
          const imageSize = imageInspection.status === 'ok'
            ? { width: imageInspection.width, height: imageInspection.height }
            : null

          // Determine if we should resize
          let shouldResize = false
          let targetSize: { width: number; height: number } | undefined

          if (imageInspection.status === 'processor_unavailable') {
            deps.platform.logger.warn('Image processing unavailable while validating attachment:', imageInspection.error?.message ?? 'unknown error')
            if (decoded.length > IMAGE_LIMITS.MAX_SIZE) {
              throw new Error('Image processing is unavailable, so oversized images cannot be validated or resized automatically. Please attach a smaller image.')
            }
          } else if (imageInspection.status === 'invalid_image') {
            throw new Error(imageInspection.error?.message || 'Invalid or unsupported image file')
          } else {
            // Validate image for Claude API
            const validation = validateImageForClaudeAPI(decoded.length, imageSize!.width, imageSize!.height)

            shouldResize = validation.needsResize ?? false
            targetSize = validation.suggestedSize

            if (!validation.valid && validation.errorCode === 'dimension_exceeded') {
              // Image exceeds 8000px limit - calculate resize to fit within limits
              const maxDim = IMAGE_LIMITS.MAX_DIMENSION
              const scale = Math.min(maxDim / imageSize!.width, maxDim / imageSize!.height)
              targetSize = {
                width: Math.floor(imageSize!.width * scale),
                height: Math.floor(imageSize!.height * scale),
              }
              shouldResize = true
              deps.platform.logger.info(`Image exceeds ${maxDim}px limit (${imageSize!.width}x${imageSize!.height}), will resize to ${targetSize.width}x${targetSize.height}`)
            } else if (!validation.valid && validation.errorCode === 'size_exceeded') {
              // File >5MB — try resize+compress instead of rejecting
              shouldResize = true
              deps.platform.logger.info(`Image exceeds 5MB (${(decoded.length / 1024 / 1024).toFixed(1)}MB), will attempt resize`)
            } else if (!validation.valid) {
              throw new Error(validation.error)
            }
          }

          // If resize is needed (either recommended or required), do it now
          if (shouldResize) {
            const isPhoto = attachment.mimeType === 'image/jpeg'

            if (targetSize) {
              // Dimension-exceeded: resize to specific target dimensions
              deps.platform.logger.info(`Resizing image from ${imageSize!.width}x${imageSize!.height} to ${targetSize.width}x${targetSize.height}`)
              try {
                decoded = await deps.platform.imageProcessor.process(decoded, {
                  resize: { width: targetSize.width, height: targetSize.height },
                  format: isPhoto ? 'jpeg' : 'png',
                  quality: isPhoto ? IMAGE_LIMITS.JPEG_QUALITY_HIGH : undefined,
                })
                wasResized = true
                finalSize = decoded.length

                // Re-validate final size after resize
                if (decoded.length > IMAGE_LIMITS.MAX_SIZE) {
                  decoded = await deps.platform.imageProcessor.process(decoded, { format: 'jpeg', quality: IMAGE_LIMITS.JPEG_QUALITY_FALLBACK })
                  finalSize = decoded.length
                  if (decoded.length > IMAGE_LIMITS.MAX_SIZE) {
                    throw new Error(`Image still too large after resize (${(decoded.length / 1024 / 1024).toFixed(1)}MB). Please use a smaller image.`)
                  }
                }
              } catch (resizeError) {
                deps.platform.logger.error('Image resize failed:', resizeError)
                const reason = resizeError instanceof Error ? resizeError.message : String(resizeError)
                throw new Error(`Image too large (${imageSize!.width}x${imageSize!.height}) and automatic resize failed: ${reason}. Please manually resize it before attaching.`)
              }
            } else {
              // Size-exceeded or optimal resize — use shared utility for full pipeline
              const result = await resizeImageForAPI(decoded, { isPhoto })
              if (!result) {
                throw new Error(`Image too large (${(decoded.length / 1024 / 1024).toFixed(1)}MB) and could not be compressed enough. Please use a smaller image.`)
              }
              decoded = result.buffer
              wasResized = true
              finalSize = decoded.length
            }

            deps.platform.logger.info(`Image resized: ${attachment.size} -> ${finalSize} bytes (${Math.round((1 - finalSize / attachment.size) * 100)}% reduction)`)

            // Store resized base64 to return to renderer
            // This is used when sending to Claude API instead of original large base64
            resizedBase64 = decoded.toString('base64')
          }
        }

        await writeFile(storedPath, decoded)
        filesToCleanup.push(storedPath)
      } else if (attachment.text) {
        // Text files - save as UTF-8
        await writeFile(storedPath, attachment.text, 'utf-8')
        filesToCleanup.push(storedPath)
      } else {
        throw new Error('Attachment has no content (neither base64 nor text)')
      }

      // 2. Generate thumbnail (images only — PDFs/Office get icon fallback)
      let thumbnailPath: string | undefined
      let thumbnailBase64: string | undefined
      const thumbFileName = `${id}_thumb.png`
      const thumbPath = join(attachmentsDir, thumbFileName)
      try {
        const pngBuffer = await deps.platform.imageProcessor.process(storedPath, {
          resize: { width: 200, height: 200 },
          format: 'png',
        })
        await writeFile(thumbPath, pngBuffer)
        thumbnailPath = thumbPath
        thumbnailBase64 = pngBuffer.toString('base64')
        filesToCleanup.push(thumbPath)
      } catch (thumbError) {
        // Thumbnail generation failed (non-image or corrupt) — icon fallback
        deps.platform.logger.info('Thumbnail generation failed (using fallback):', thumbError instanceof Error ? thumbError.message : thumbError)
      }

      // 3. Convert Office files to markdown (for sending to Claude)
      // This is required for Office files - Claude can't read raw Office binary
      let markdownPath: string | undefined
      if (attachment.type === 'office') {
        const mdFileName = `${id}_${safeName}.md`
        const mdPath = join(attachmentsDir, mdFileName)
        try {
          const markitdown = new MarkItDown()
          const result = await markitdown.convert(storedPath)
          if (!result || !result.textContent) {
            throw new Error('Conversion returned empty result')
          }
          await writeFile(mdPath, result.textContent, 'utf-8')
          markdownPath = mdPath
          filesToCleanup.push(mdPath)
          deps.platform.logger.info(`Converted Office file to markdown: ${mdPath}`)
        } catch (convertError) {
          // Conversion failed - throw so user knows the file can't be processed
          // Claude can't read raw Office binary, so a failed conversion = unusable file
          const errorMsg = convertError instanceof Error ? convertError.message : String(convertError)
          deps.platform.logger.error('Office to markdown conversion failed:', errorMsg)
          throw new Error(`Failed to convert "${attachment.name}" to readable format: ${errorMsg}`)
        }
      }

      // Return StoredAttachment metadata
      // Include wasResized flag so UI can show notification
      // Include resizedBase64 so renderer uses resized image for Claude API
      return {
        id,
        type: attachment.type,
        name: attachment.name,
        mimeType: attachment.mimeType,
        size: finalSize, // Use final size (may differ if resized)
        originalSize: wasResized ? attachment.size : undefined, // Track original if resized
        storedPath,
        thumbnailPath,
        thumbnailBase64,
        markdownPath,
        wasResized,
        resizedBase64, // Only set when wasResized=true, used for Claude API
      }
    } catch (error) {
      // Clean up any files we've written before the error
      if (filesToCleanup.length > 0) {
        deps.platform.logger.info(`Cleaning up ${filesToCleanup.length} orphaned file(s) after storage error`)
        await Promise.all(filesToCleanup.map(f => unlink(f).catch(() => {})))
      }

      const message = error instanceof Error ? error.message : 'Unknown error'
      deps.platform.logger.error('storeAttachment error:', message)
      throw new Error(`Failed to store attachment: ${message}`)
    }
  })

  // Filesystem search for @ mention file selection.
  // Parallel BFS walk that skips ignored directories BEFORE entering them,
  // avoiding reading node_modules/etc. contents entirely. Uses withFileTypes
  // to get entry types without separate stat calls.
  server.handle(RPC_CHANNELS.fs.SEARCH, async (_ctx, basePath: string, query: string) => {
    deps.platform.logger.info('[FS_SEARCH] called:', basePath, query)
    const MAX_RESULTS = 50

    // Directories to never recurse into
    const SKIP_DIRS = new Set([
      'node_modules', '.git', '.svn', '.hg', 'dist', 'build',
      '.next', '.nuxt', '.cache', '__pycache__', 'vendor',
      '.idea', '.vscode', 'coverage', '.nyc_output', '.turbo', 'out',
    ])

    const lowerQuery = query.toLowerCase()
    const results: Array<{ name: string; path: string; type: 'file' | 'directory'; relativePath: string }> = []

    try {
      // BFS queue: each entry is a relative path prefix ('' for root)
      let queue = ['']

      while (queue.length > 0 && results.length < MAX_RESULTS) {
        // Process current level: read all directories in parallel
        const nextQueue: string[] = []

        const dirResults = await Promise.all(
          queue.map(async (relDir) => {
            const absDir = relDir ? join(basePath, relDir) : basePath
            try {
              return { relDir, entries: await readdir(absDir, { withFileTypes: true }) }
            } catch {
              // Skip dirs we can't read (permissions, broken symlinks, etc.)
              return { relDir, entries: [] as import('fs').Dirent[] }
            }
          })
        )

        for (const { relDir, entries } of dirResults) {
          if (results.length >= MAX_RESULTS) break

          for (const entry of entries) {
            if (results.length >= MAX_RESULTS) break

            const name = entry.name
            // Skip hidden files/dirs and ignored directories
            if (name.startsWith('.') || SKIP_DIRS.has(name)) continue

            const relativePath = relDir ? `${relDir}/${name}` : name
            const isDir = entry.isDirectory()

            // Queue subdirectories for next BFS level
            if (isDir) {
              nextQueue.push(relativePath)
            }

            // Check if name or path matches the query
            const lowerName = name.toLowerCase()
            const lowerRelative = relativePath.toLowerCase()
            if (lowerName.includes(lowerQuery) || lowerRelative.includes(lowerQuery)) {
              results.push({
                name,
                path: join(basePath, relativePath),
                type: isDir ? 'directory' : 'file',
                relativePath,
              })
            }
          }
        }

        queue = nextQueue
      }

      // Sort: directories first, then by name length (shorter = better match)
      results.sort((a, b) => {
        if (a.type !== b.type) return a.type === 'directory' ? -1 : 1
        return a.name.length - b.name.length
      })

      deps.platform.logger.info('[FS_SEARCH] returning', results.length, 'results')
      return results
    } catch (err) {
      deps.platform.logger.error('[FS_SEARCH] error:', err)
      return []
    }
  })

  // List directories in a given path (for remote directory browsing).
  // Returns only directories (not files) — this is a folder picker.
  server.handle(RPC_CHANNELS.fs.LIST_DIRECTORY, async (_ctx, dirPath: string) => {
    // Resolve ~ to server's home directory (thin clients don't know the server's home)
    if (dirPath === '~' || dirPath.startsWith('~/')) {
      dirPath = dirPath === '~' ? homedir() : join(homedir(), dirPath.slice(2))
    }

    // Reject cross-platform and relative paths before resolve() can concatenate with cwd
    const pathCheck = validatePathFormat(dirPath)
    if (!pathCheck.valid) {
      throw new Error(pathCheck.reason!)
    }

    // Normalize (collapses .. segments, trailing slashes, etc.)
    const resolved = resolve(dirPath)

    // Read entries, filter to directories
    const raw = await readdir(resolved, { withFileTypes: true })

    const entries: Array<{ name: string; path: string; isSymlink: boolean }> = []
    for (const entry of raw) {
      const fullPath = join(resolved, entry.name)
      const isSymlink = entry.isSymbolicLink()

      if (entry.isDirectory()) {
        entries.push({ name: entry.name, path: fullPath, isSymlink: false })
      } else if (isSymlink) {
        // Follow symlink — check if target is a directory
        try {
          const target = await stat(fullPath)
          if (target.isDirectory()) {
            entries.push({ name: entry.name, path: fullPath, isSymlink: true })
          }
        } catch {
          // Broken symlink — skip silently
        }
      }
    }

    // Sort alphabetically (case-insensitive), cap at 500
    entries.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
    const totalEntries = entries.length
    const truncated = totalEntries > 500
    if (truncated) entries.length = 500

    // Compute parent path
    const parentPath = resolved === parsePath(resolved).root ? null : dirname(resolved)

    // Compute breadcrumbs server-side
    const breadcrumbs: Array<{ name: string; path: string }> = []
    let current = resolved
    while (true) {
      const parsed = parsePath(current)
      const name = parsed.base || parsed.root
      breadcrumbs.unshift({ name, path: current })
      if (current === parsed.root) break
      current = dirname(current)
    }

    return {
      currentPath: resolved,
      parentPath,
      breadcrumbs,
      platform: process.platform as DirectoryListingResult['platform'],
      truncated,
      totalEntries,
      entries,
    } satisfies DirectoryListingResult
  })
}
