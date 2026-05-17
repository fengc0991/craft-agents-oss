/**
 * LarkAdapter — Lark / Feishu in-process adapter.
 *
 * Transport: long-polling via `@larksuiteoapi/node-sdk`'s `WSClient`. No public
 * webhook URL needed (correct fit for desktop / electron). Same lifecycle
 * shape as the Telegram adapter, just a different SDK underneath.
 *
 * Phase 1 scope (text only): receive text in DMs and group @mentions, send
 * text replies, support `/pair`-style commands. Phase 2 layers on edits,
 * interactive cards, attachments, and Markdown→post rich-text formatting.
 */

import { statSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { extname, join } from 'node:path'
import { randomBytes } from 'node:crypto'
import * as lark from '@larksuiteoapi/node-sdk'
import type {
  PlatformAdapter,
  PlatformConfig,
  AdapterCapabilities,
  IncomingAttachment,
  IncomingMessage,
  SentMessage,
  InlineButton,
  ButtonPress,
  MessagingLogger,
  MessagingLogMeta,
  SendOptions,
} from '../../types'
import {
  formatForLarkPost,
  wrapAsTrivialPost,
  type LarkPost,
  type LarkPostLocaleKey,
} from './format'
import {
  buildLarkCard,
  buildClearedCard,
  isLarkEditExpiredError,
  LARK_MAX_BUTTONS,
} from './card'

/**
 * Hard cap for downloaded attachment size. Matches Telegram's MAX_ATTACHMENT_BYTES
 * — files larger than this would be rejected by `readFileAttachment` anyway, so
 * we fail fast in the adapter with a user-visible reply.
 */
const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
const LARK_MAX_UPLOAD_FILE_BYTES = 30 * 1024 * 1024
const LARK_MAX_UPLOAD_IMAGE_BYTES = 10 * 1024 * 1024
const ACK_REACTION_EMOJI = 'OK'
const ACKED_MESSAGE_MAX = 2000

const NOOP_LOGGER: MessagingLogger = {
  info: () => {},
  warn: () => {},
  error: () => {},
  child: () => NOOP_LOGGER,
}

/**
 * Credential payload for a Lark/Feishu bot.
 *
 * Stored as a JSON string in the `messaging_bearer` credential row (one row
 * per workspace+platform). Single existing schema, no migrations.
 */
export interface LarkCredentials {
  appId: string
  appSecret: string
  /**
   * Which Open Platform domain to talk to. Lark and Feishu are separate
   * ecosystems — a Lark bot only works against open.larksuite.com,
   * a Feishu bot only against open.feishu.cn.
   */
  domain: 'lark' | 'feishu'
}

/**
 * Parse the JSON-encoded credentials from `PlatformConfig.token`.
 *
 * Throws with a clear message if the input is malformed — surfaces as
 * `state: 'error'` with a user-readable `lastError` in the registry.
 */
export function parseLarkCredentials(token: string | undefined): LarkCredentials {
  if (!token) throw new Error('Lark credentials are missing')
  let parsed: unknown
  try {
    parsed = JSON.parse(token)
  } catch {
    throw new Error('Lark credentials are not valid JSON')
  }
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('Lark credentials must be a JSON object')
  }
  const { appId, appSecret, domain } = parsed as Record<string, unknown>
  if (typeof appId !== 'string' || appId.length === 0) {
    throw new Error('Lark credentials are missing `appId`')
  }
  if (typeof appSecret !== 'string' || appSecret.length === 0) {
    throw new Error('Lark credentials are missing `appSecret`')
  }
  if (domain !== 'lark' && domain !== 'feishu') {
    throw new Error('Lark credentials `domain` must be "lark" or "feishu"')
  }
  return { appId, appSecret, domain }
}

/**
 * Map our `'lark' | 'feishu'` selector to the SDK's `Domain` enum.
 */
function resolveLarkDomain(domain: 'lark' | 'feishu'): lark.Domain {
  return domain === 'feishu' ? lark.Domain.Feishu : lark.Domain.Lark
}

function resolveLarkApiBaseUrl(domain: 'lark' | 'feishu'): string {
  return domain === 'feishu' ? 'https://open.feishu.cn' : 'https://open.larksuite.com'
}

type LarkUploadFileType = 'opus' | 'mp4' | 'pdf' | 'doc' | 'xls' | 'ppt' | 'stream'

function resolveLarkFileType(filename: string): LarkUploadFileType {
  switch (extname(filename).toLowerCase()) {
    case '.opus':
      return 'opus'
    case '.mp4':
      return 'mp4'
    case '.pdf':
      return 'pdf'
    case '.doc':
    case '.docx':
      return 'doc'
    case '.xls':
    case '.xlsx':
      return 'xls'
    case '.ppt':
    case '.pptx':
      return 'ppt'
    default:
      return 'stream'
  }
}

function createMessageUuid(): string {
  return `craft_${randomBytes(12).toString('hex')}`
}

interface LarkApiEnvelope<TData extends object = Record<string, unknown>> {
  code?: number
  msg?: string
  data?: TData
  tenant_access_token?: string
  expire?: number
}

function extractLarkErrorMeta(err: unknown): MessagingLogMeta {
  const meta: MessagingLogMeta = {
    error: err instanceof Error ? err.message : String(err),
  }
  const errObj = (err ?? {}) as {
    code?: unknown
    msg?: unknown
    response?: { status?: unknown; data?: unknown }
    cause?: unknown
  }
  if (typeof errObj.code === 'number' || typeof errObj.code === 'string') meta.code = errObj.code
  if (typeof errObj.msg === 'string') meta.msg = errObj.msg
  if (typeof errObj.response?.status === 'number') meta.httpStatus = errObj.response.status

  const responseData = errObj.response?.data
  if (responseData && typeof responseData === 'object') {
    const data = responseData as { code?: unknown; msg?: unknown; error?: unknown }
    if (typeof data.code === 'number' || typeof data.code === 'string') meta.larkCode = data.code
    if (typeof data.msg === 'string') meta.larkMsg = data.msg
    if (data.error !== undefined) meta.larkError = data.error
  }
  if (errObj.cause instanceof Error) meta.cause = errObj.cause.message
  return meta
}

function shouldFallbackPostToText(err: unknown): boolean {
  const errObj = (err ?? {}) as {
    code?: unknown
    response?: { status?: unknown; data?: unknown }
  }
  const status = errObj.response?.status
  if (typeof status === 'number') {
    return status >= 400 && status < 500
  }
  const responseData = errObj.response?.data
  if (responseData && typeof responseData === 'object') return true
  return typeof errObj.code === 'number'
}

/**
 * Strip a leading `<at user_id="...">…</at> ` prefix from a Lark text message
 * content. Lark prepends the @mention as a literal in the content, but the
 * agent only cares about what comes after.
 */
function stripMentionPrefix(text: string): string {
  return text.replace(/^<at[^>]*>[^<]*<\/at>\s*/, '').trim()
}

/**
 * Narrow projection over the SDK's `Client` for the methods we actually call.
 * The SDK's full type union is enormous (~250k lines) and changes shape between
 * minor versions; pinning a hand-rolled interface keeps our adapter loosely
 * coupled and the ts-checker happy.
 */
interface LarkClient {
  im: {
    v1?: {
      messageReaction: {
        create: (args: {
          path: { message_id: string }
          data: { reaction_type: { emoji_type: string } }
        }) => Promise<{ data?: { reaction_id?: string } } | null>
      }
    }
    message: {
      create: (args: {
        params: { receive_id_type: 'chat_id' | 'open_id' | 'union_id' }
        data: { receive_id: string; msg_type: string; content: string; uuid?: string }
      }) => Promise<{ data?: { message_id?: string } } | null>
      update: (args: {
        path: { message_id: string }
        data: { msg_type: string; content: string }
      }) => Promise<unknown>
      patch: (args: {
        path: { message_id: string }
        data: { content: string }
      }) => Promise<unknown>
    }
    file: {
      create: (args: {
        data: { file_type: 'opus' | 'mp4' | 'pdf' | 'doc' | 'xls' | 'ppt' | 'stream'; file_name: string; file: Buffer }
      }) => Promise<{ file_key?: string; data?: { file_key?: string } } | null>
    }
    image: {
      create: (args: {
        data: { image_type: 'message' | 'avatar'; image: Buffer }
      }) => Promise<{ image_key?: string; data?: { image_key?: string } } | null>
    }
    messageResource?: {
      get: (args: {
        path: { message_id: string; file_key: string }
        params: { type: 'image' | 'file' }
      }) => Promise<{ writeFile?: (path: string) => Promise<unknown>; file?: Buffer } & Record<string, unknown>>
    }
  }
}

/**
 * Flat shape after the SDK's `EventDispatcher.parse()` unwraps the v2 envelope.
 * The dispatcher merges `{schema, header, event}` into a single object before
 * invoking handlers, so payload fields land at the top level — there is no
 * outer `.event` accessor.
 */
interface LarkMessageEvent {
  sender: {
    sender_id?: { user_id?: string; open_id?: string; union_id?: string }
  }
  message: {
    message_id: string
    chat_id: string
    chat_type: string
    message_type: string
    content: string
    create_time: string
    mentions?: Array<{ key: string; id: { user_id?: string }; name: string }>
  }
}

/**
 * Card-action press event after the SDK's `EventDispatcher.parse()` flattens
 * the v2 envelope. Schema 2.0 nests the chat id under `context` instead of
 * at the top level — handle both shapes so the same code path works for v1
 * and v2 cards.
 */
interface LarkCardActionEvent {
  operator?: { user_id?: string; open_id?: string; union_id?: string }
  /** Schema 1.0 location for the chat id. */
  open_chat_id?: string
  /** Schema 2.0 location — `context.open_chat_id` and friends. */
  context?: {
    open_chat_id?: string
    open_message_id?: string
  }
  action?: {
    value?: unknown
    tag?: string
  }
}

export class LarkAdapter implements PlatformAdapter {
  readonly platform = 'lark' as const
  readonly capabilities: AdapterCapabilities = {
    messageEditing: true,
    inlineButtons: true,
    maxButtons: LARK_MAX_BUTTONS,
    maxMessageLength: 30000,
    markdown: 'lark-post',
    webhookSupport: false,
  }

  private client: LarkClient | null = null
  private wsClient: lark.WSClient | null = null
  private messageHandler: ((msg: IncomingMessage) => Promise<void>) | null = null
  private buttonHandler: ((press: ButtonPress) => Promise<void>) | null = null
  private connected = false
  private log: MessagingLogger = NOOP_LOGGER
  private postLocale: LarkPostLocaleKey = 'zh_cn'
  private ackedInboundMessages = new Set<string>()
  private credentials: LarkCredentials | null = null
  private apiBaseUrl = ''
  private tenantAccessTokenCache: { token: string; expiresAt: number } | null = null
  /**
   * Track each outbound message's wire `msg_type` so `editMessage` can dispatch
   * to `update` (text/post) vs `patch` (interactive card) correctly. Lark
   * requires the new `msg_type` to match the original.
   */
  private sentMsgTypes = new Map<string, 'text' | 'post' | 'interactive'>()

  /** Fetch bot profile for UI hints. */
  async getBotInfo(): Promise<{ name?: string } | null> {
    if (!this.client) return null
    try {
      // The SDK's `bot.v3.info.get` (no args) returns `{ data: { bot: { app_name } } }`.
      // Unsafe-cast through unknown — the bot namespace isn't in our narrow projection.
      const c = this.client as unknown as {
        bot: { v3: { info: { get: () => Promise<{ data?: { bot?: { app_name?: string } } }> } } }
      }
      const result = await c.bot.v3.info.get()
      const name = result.data?.bot?.app_name
      return name ? { name } : null
    } catch {
      return null
    }
  }

  async initialize(config: PlatformConfig): Promise<void> {
    this.log = config.logger ?? NOOP_LOGGER
    const creds = parseLarkCredentials(config.token)
    const sdkDomain = resolveLarkDomain(creds.domain)
    this.postLocale = creds.domain === 'lark' ? 'en_us' : 'zh_cn'
    this.credentials = creds
    this.apiBaseUrl = resolveLarkApiBaseUrl(creds.domain)
    this.tenantAccessTokenCache = null

    // Construct REST client (sends + lookups go through this).
    this.client = new lark.Client({
      appId: creds.appId,
      appSecret: creds.appSecret,
      domain: sdkDomain,
      loggerLevel: lark.LoggerLevel.warn,
    }) as unknown as LarkClient

    // Long-connection WS client + event dispatcher.
    //
    // Lifecycle hooks log explicitly so we can distinguish "socket never
    // opened" from "socket open but no events firing" — the second one
    // usually means the app's scopes or event subscriptions are misconfigured
    // on the Open Platform side, which is invisible from our side otherwise.
    this.wsClient = new lark.WSClient({
      appId: creds.appId,
      appSecret: creds.appSecret,
      domain: sdkDomain,
      loggerLevel: lark.LoggerLevel.info,
      onReady: () => {
        this.log.info('[lark] ws ready', { event: 'lark_ws_ready' })
      },
      onError: (err: unknown) => {
        this.log.error('[lark] ws error', {
          event: 'lark_ws_error',
          error: err instanceof Error ? err.message : String(err),
        })
      },
      onReconnecting: () => {
        this.log.info('[lark] ws reconnecting', { event: 'lark_ws_reconnecting' })
      },
      onReconnected: () => {
        this.log.info('[lark] ws reconnected', { event: 'lark_ws_reconnected' })
      },
    } as unknown as ConstructorParameters<typeof lark.WSClient>[0])

    // The SDK's `register` typing is a wide-open union over hundreds of event
    // names. Cast the handler block once via `unknown` to keep the adapter
    // readable; the per-handler payload casts above handle the actual shape.
    const eventDispatcher = new lark.EventDispatcher({}).register({
      'im.message.receive_v1': async (data: unknown) => {
        await this.handleIncomingMessage(data as LarkMessageEvent)
      },
      'card.action.trigger': async (data: unknown) => {
        await this.handleCardAction(data as LarkCardActionEvent)
        // Lark expects a synchronous return that may patch the card; we
        // return an empty object (no patch) and let `clearButtons` do the
        // visual cleanup async via the binding's existing post-press flow.
        return {}
      },
    } as unknown as Parameters<lark.EventDispatcher['register']>[0])

    await this.wsClient.start({ eventDispatcher })
    this.connected = true
    this.log.info('[lark] connected', {
      event: 'lark_connected',
      domain: creds.domain,
    })
  }

  async destroy(): Promise<void> {
    // The SDK's WSClient doesn't currently expose a `.stop()` method in its
    // public types — it tears down on process exit. We null out our refs so
    // re-init works; the underlying socket gets garbage-collected.
    this.wsClient = null
    this.client = null
    this.credentials = null
    this.apiBaseUrl = ''
    this.tenantAccessTokenCache = null
    this.connected = false
    this.sentMsgTypes.clear()
    this.ackedInboundMessages.clear()
  }

  isConnected(): boolean {
    return this.connected
  }

  onMessage(handler: (msg: IncomingMessage) => Promise<void>): void {
    this.messageHandler = handler
  }

  onButtonPress(handler: (press: ButtonPress) => Promise<void>): void {
    this.buttonHandler = handler
  }

  private dispatchMessage(msg: IncomingMessage): void {
    const handler = this.messageHandler
    if (!handler) return
    void handler(msg).catch((err: unknown) => {
      this.log.error('[lark] message handler failed', {
        event: 'lark_message_handler_failed',
        messageId: msg.messageId,
        chatId: msg.channelId,
        error: err instanceof Error ? err.message : String(err),
      })
    })
  }

  private acknowledgeInboundMessage(messageId: string): void {
    if (!messageId || this.ackedInboundMessages.has(messageId)) return
    this.ackedInboundMessages.add(messageId)
    this.trimAckedInboundMessages()

    void this.addReaction(messageId, ACK_REACTION_EMOJI).catch((err: unknown) => {
      this.log.warn('[lark] failed to add inbound ack reaction', {
        event: 'lark_ack_reaction_failed',
        messageId,
        emojiType: ACK_REACTION_EMOJI,
        error: err instanceof Error ? err.message : String(err),
      })
    })
  }

  private async addReaction(messageId: string, emojiType: string): Promise<void> {
    if (!this.client?.im.v1?.messageReaction) {
      this.log.warn('[lark] messageReaction API is unavailable on SDK client', {
        event: 'lark_ack_reaction_api_missing',
        messageId,
        emojiType,
      })
      return
    }
    await this.client.im.v1.messageReaction.create({
      path: { message_id: messageId },
      data: { reaction_type: { emoji_type: emojiType } },
    })
  }

  private trimAckedInboundMessages(): void {
    while (this.ackedInboundMessages.size > ACKED_MESSAGE_MAX) {
      const oldest = this.ackedInboundMessages.values().next().value
      if (!oldest) break
      this.ackedInboundMessages.delete(oldest)
    }
  }

  private async readLarkApiEnvelope<TData extends object>(
    response: Awaited<ReturnType<typeof fetch>>,
    operation: string,
  ): Promise<LarkApiEnvelope<TData>> {
    const text = await response.text()
    let parsed: unknown = {}
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text)
      } catch {
        throw new Error(`${operation} returned a non-JSON response: ${text.slice(0, 500)}`)
      }
    }

    const body = parsed as LarkApiEnvelope<TData>
    const msg = typeof body.msg === 'string' && body.msg.length > 0 ? body.msg : ''
    const code = typeof body.code === 'number' ? body.code : 0

    if (!response.ok) {
      throw new Error(`${operation} failed: HTTP ${response.status}${msg ? ` ${msg}` : ''}`)
    }
    if (code !== 0) {
      throw new Error(`${operation} failed: ${code}${msg ? ` ${msg}` : ''}`)
    }
    return body
  }

  private async getTenantAccessToken(): Promise<string> {
    const cached = this.tenantAccessTokenCache
    if (cached && cached.expiresAt > Date.now() + 60_000) return cached.token

    const creds = this.credentials
    if (!creds || !this.apiBaseUrl) throw new Error('Lark adapter is not initialized')

    const response = await fetch(`${this.apiBaseUrl}/open-apis/auth/v3/tenant_access_token/internal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app_id: creds.appId, app_secret: creds.appSecret }),
    })
    const body = await this.readLarkApiEnvelope(response, 'get Lark tenant_access_token')
    const token = body.tenant_access_token
    if (typeof token !== 'string' || token.length === 0) {
      throw new Error('Lark auth returned no tenant_access_token')
    }

    const expiresInSeconds = typeof body.expire === 'number' && body.expire > 0 ? body.expire : 5400
    this.tenantAccessTokenCache = {
      token,
      expiresAt: Date.now() + Math.max(60, expiresInSeconds - 300) * 1000,
    }
    return token
  }

  private async uploadLarkFileWithFetch(file: Buffer, filename: string): Promise<string> {
    const token = await this.getTenantAccessToken()
    const form = new FormData()
    form.append('file_type', resolveLarkFileType(filename))
    form.append('file_name', filename)
    form.append('file', new Blob([new Uint8Array(file)]), filename)

    const response = await fetch(`${this.apiBaseUrl}/open-apis/im/v1/files`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    })
    const body = await this.readLarkApiEnvelope<{ file_key?: string }>(response, 'upload Lark file')
    const fileKey = body.data?.file_key
    if (typeof fileKey !== 'string' || fileKey.length === 0) {
      throw new Error('Lark file upload returned no file_key')
    }
    return fileKey
  }

  private async uploadLarkImageWithFetch(file: Buffer, filename: string): Promise<string> {
    const token = await this.getTenantAccessToken()
    const form = new FormData()
    form.append('image_type', 'message')
    form.append('image', new Blob([new Uint8Array(file)]), filename)

    const response = await fetch(`${this.apiBaseUrl}/open-apis/im/v1/images`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    })
    const body = await this.readLarkApiEnvelope<{ image_key?: string }>(response, 'upload Lark image')
    const imageKey = body.data?.image_key
    if (typeof imageKey !== 'string' || imageKey.length === 0) {
      throw new Error('Lark image upload returned no image_key')
    }
    return imageKey
  }

  // -------------------------------------------------------------------------
  // Outbound — sends, edits, files, cards
  // -------------------------------------------------------------------------

  async sendText(channelId: string, text: string, _opts?: SendOptions): Promise<SentMessage> {
    if (!this.client) throw new Error('Lark adapter is not connected')
    const formatted = formatForLarkPost(text, this.postLocale)
    const post =
      formatted.kind === 'post'
        ? formatted.post
        : wrapAsTrivialPost(formatted.text, this.postLocale)
    const msgType = 'post' as const
    const content = JSON.stringify(post)
    const uuid = createMessageUuid()

    try {
      const result = await this.client.im.message.create({
        params: { receive_id_type: 'chat_id' },
        data: { receive_id: channelId, msg_type: msgType, content, uuid },
      })
      const messageId = result?.data?.message_id ?? ''
      if (messageId) this.sentMsgTypes.set(messageId, msgType)
      return { platform: 'lark', channelId, messageId }
    } catch (postErr: unknown) {
      if (!shouldFallbackPostToText(postErr)) {
        this.log.warn('[lark] post message failed with ambiguous delivery status', {
          event: 'lark_send_post_ambiguous_failed',
          chatId: channelId,
          payloadSize: content.length,
          ...extractLarkErrorMeta(postErr),
        })
        throw postErr
      }

      this.log.warn('[lark] post message failed; retrying as text', {
        event: 'lark_send_post_fallback',
        chatId: channelId,
        payloadSize: content.length,
        payloadPreview: content.slice(0, 500),
        ...extractLarkErrorMeta(postErr),
      })

      const textMsgType = 'text' as const
      const textContent = JSON.stringify({ text })
      try {
        const result = await this.client.im.message.create({
          params: { receive_id_type: 'chat_id' },
          data: { receive_id: channelId, msg_type: textMsgType, content: textContent, uuid },
        })
        const messageId = result?.data?.message_id ?? ''
        if (messageId) this.sentMsgTypes.set(messageId, textMsgType)
        return { platform: 'lark', channelId, messageId }
      } catch (textErr: unknown) {
        this.log.error('[lark] text fallback also failed', {
          event: 'lark_send_text_fallback_failed',
          chatId: channelId,
          payloadSize: textContent.length,
          ...extractLarkErrorMeta(textErr),
        })
        throw textErr
      }
    }
  }

  async editMessage(
    channelId: string,
    messageId: string,
    text: string,
    _opts?: SendOptions,
  ): Promise<void> {
    if (!this.client) throw new Error('Lark adapter is not connected')
    const originalType = this.sentMsgTypes.get(messageId) ?? 'text'

    // Cards are patched, not updated — different API.
    if (originalType === 'interactive') {
      // Editing an active card replaces its text body but keeps the buttons.
      // For the text-only edit path the renderer takes, we fall back to a
      // cleared-card patch (text without buttons), matching the Telegram
      // behaviour where a final-text edit removes the button row.
      try {
        await this.client.im.message.patch({
          path: { message_id: messageId },
          data: { content: JSON.stringify(buildClearedCard(text)) },
        })
      } catch (err: unknown) {
        if (isLarkEditExpiredError(err)) return
        throw err
      }
      return
    }

    // text or post — match the original type so Lark accepts the update.
    let content: string
    let msgType: 'text' | 'post'
    if (originalType === 'post') {
      // If the new content has formatting, format it; otherwise wrap as
      // a trivial post so the msg_type still matches the original.
      const formatted = formatForLarkPost(text, this.postLocale)
      const post: LarkPost =
        formatted.kind === 'post'
          ? formatted.post
          : wrapAsTrivialPost(formatted.text, this.postLocale)
      content = JSON.stringify(post)
      msgType = 'post'
    } else {
      content = JSON.stringify({ text })
      msgType = 'text'
    }

    try {
      await this.client.im.message.update({
        path: { message_id: messageId },
        data: { msg_type: msgType, content },
      })
    } catch (err: unknown) {
      if (isLarkEditExpiredError(err)) return
      throw err
    }
  }

  async sendButtons(
    channelId: string,
    text: string,
    buttons: InlineButton[],
    _opts?: SendOptions,
  ): Promise<SentMessage> {
    if (!this.client) throw new Error('Lark adapter is not connected')
    if (buttons.length > LARK_MAX_BUTTONS) {
      this.log.warn('[lark] too many buttons; truncating to cap', {
        event: 'lark_button_cap',
        requested: buttons.length,
        cap: LARK_MAX_BUTTONS,
      })
    }

    // Send the card without the messageId in the buttons' value — we don't
    // know the messageId until after the create. Fix this up in two stages:
    // 1) post the card with a placeholder; 2) extract the returned message_id
    //    and patch the card with the real value. Phase 2 acceptance is good
    //    enough — the press handler can look up the binding from chat_id alone
    //    if needed, but storing the id keeps gated routing simple.
    const placeholderCard = buildLarkCard(text, buttons, { messageId: 'pending' })
    const cardJson = JSON.stringify(placeholderCard)

    // Wrap the API call so any payload-shape / scope / quota issues surface
    // in our logs with a structured `lark_send_card_failed` event instead of
    // bubbling up unannotated through the renderer's outer catch. We also
    // post a plain-text fallback so the user always sees *something* in the
    // chat when the rich card path breaks, then re-throw so the renderer
    // can record the failure.
    let messageId = ''
    try {
      const result = await this.client.im.message.create({
        params: { receive_id_type: 'chat_id' },
        data: {
          receive_id: channelId,
          msg_type: 'interactive',
          content: cardJson,
        },
      })
      messageId = result?.data?.message_id ?? ''
      this.log.info('[lark] sent card', {
        event: 'lark_send_card_ok',
        chatId: channelId,
        messageId,
        buttonCount: Math.min(buttons.length, LARK_MAX_BUTTONS),
      })
    } catch (err: unknown) {
      // The SDK wraps every error in axios's `AxiosError`. The actual
      // Lark-side reason (code + msg) lives at `err.response.data`, NOT at
      // the top level — extract it so the log line is actually useful.
      const errObj = (err ?? {}) as {
        code?: unknown
        msg?: unknown
        message?: unknown
        response?: { status?: unknown; data?: unknown }
      }
      const responseData = (errObj.response?.data ?? null) as
        | { code?: unknown; msg?: unknown; error?: unknown }
        | null
      this.log.error('[lark] failed to send card', {
        event: 'lark_send_card_failed',
        chatId: channelId,
        httpStatus: typeof errObj.response?.status === 'number' ? errObj.response.status : undefined,
        larkCode:
          typeof responseData?.code === 'number'
            ? responseData.code
            : typeof errObj.code === 'number'
              ? errObj.code
              : undefined,
        larkMsg:
          typeof responseData?.msg === 'string'
            ? responseData.msg
            : typeof errObj.msg === 'string'
              ? errObj.msg
              : undefined,
        larkError: responseData?.error,
        error: err instanceof Error ? err.message : String(err),
        payloadSize: cardJson.length,
        payloadPreview: cardJson.slice(0, 500),
        buttonCount: buttons.length,
      })
      // Best-effort plain-text fallback so the user knows something happened.
      // Failures here are non-fatal — we still re-throw the original card error.
      try {
        await this.sendText(
          channelId,
          `${text}\n\n(Open the desktop app to respond — the in-chat buttons couldn't be sent.)`,
          _opts,
        )
      } catch {
        // Swallowed — the renderer's outer handler will see the original throw.
      }
      throw err
    }

    if (messageId) {
      this.sentMsgTypes.set(messageId, 'interactive')
      // Patch with the real message_id baked into each button's value so card
      // press events carry the correct correlation.
      try {
        const realCard = buildLarkCard(text, buttons, { messageId })
        await this.client.im.message.patch({
          path: { message_id: messageId },
          data: { content: JSON.stringify(realCard) },
        })
      } catch (err: unknown) {
        // Non-fatal — the card already exists with placeholder ids; press
        // routing will fall back to looking up by chat_id.
        if (!isLarkEditExpiredError(err)) {
          this.log.warn('[lark] failed to patch card with real messageId', {
            event: 'lark_card_patch_failed',
            messageId,
            error: err instanceof Error ? err.message : String(err),
          })
        }
      }
    }
    return { platform: 'lark', channelId, messageId }
  }

  async clearButtons(channelId: string, messageId: string, _opts?: SendOptions): Promise<void> {
    if (!this.client) return
    void channelId
    try {
      await this.client.im.message.patch({
        path: { message_id: messageId },
        data: { content: JSON.stringify(buildClearedCard('')) },
      })
    } catch (err: unknown) {
      if (isLarkEditExpiredError(err)) return
      this.log.warn('[lark] clearButtons failed', {
        event: 'lark_clear_buttons_failed',
        messageId,
        error: err instanceof Error ? err.message : String(err),
      })
    }
  }

  async sendTyping(_channelId: string, _opts?: SendOptions): Promise<void> {
    // Lark has no typing-indicator API. No-op.
  }

  async sendFile(
    channelId: string,
    file: Buffer,
    filename: string,
    caption?: string,
    _opts?: SendOptions,
  ): Promise<SentMessage> {
    if (!this.client) throw new Error('Lark adapter is not connected')

    const isImage = /\.(jpe?g|png|gif|webp|bmp|tiff?|ico)$/i.test(filename)
    const uploadLimit = isImage ? LARK_MAX_UPLOAD_IMAGE_BYTES : LARK_MAX_UPLOAD_FILE_BYTES
    if (file.byteLength === 0) throw new Error('Lark file upload requires a non-empty file')
    if (file.byteLength > uploadLimit) {
      throw new Error(
        `Lark ${isImage ? 'image' : 'file'} upload limit is ${uploadLimit} bytes; got ${file.byteLength}`,
      )
    }

    let content: string
    let msgType: 'image' | 'file'
    let messageId = ''
    try {
      if (isImage) {
        const imageKey = await this.uploadLarkImageWithFetch(file, filename)
        content = JSON.stringify({ image_key: imageKey })
        msgType = 'image'
      } else {
        const fileKey = await this.uploadLarkFileWithFetch(file, filename)
        content = JSON.stringify({ file_key: fileKey })
        msgType = 'file'
      }

      this.log.info('[lark] uploaded outbound resource', {
        event: 'lark_upload_resource_ok',
        chatId: channelId,
        fileName: filename,
        fileSize: file.byteLength,
        resourceType: msgType,
      })

      const result = await this.client.im.message.create({
        params: { receive_id_type: 'chat_id' },
        data: { receive_id: channelId, msg_type: msgType, content },
      })
      messageId = result?.data?.message_id ?? ''
    } catch (err: unknown) {
      this.log.error('[lark] failed to send file', {
        event: 'lark_send_file_failed',
        chatId: channelId,
        fileName: filename,
        fileSize: file.byteLength,
        ...extractLarkErrorMeta(err),
      })
      throw err
    }

    // Lark can't combine caption + file in one message. If the caller wants a
    // caption, send it as a follow-up text message (best-effort).
    if (caption) {
      this.sendText(channelId, caption).catch((err) => {
        this.log.warn('[lark] caption follow-up failed', {
          event: 'lark_caption_failed',
          messageId,
          error: err instanceof Error ? err.message : String(err),
        })
      })
    }

    return { platform: 'lark', channelId, messageId }
  }

  // -------------------------------------------------------------------------
  // Inbound — message + card events
  // -------------------------------------------------------------------------

  private async handleIncomingMessage(data: LarkMessageEvent): Promise<void> {
    if (!this.messageHandler) return
    const { sender, message } = data

    // Visibility log: if this never fires, the bot isn't getting the event
    // from Lark. Most common causes: missing `im:message` scope, missing
    // event subscription, or app not published.
    this.log.info('[lark] event received', {
      event: 'lark_event_received',
      messageType: message.message_type,
      chatType: message.chat_type,
      chatId: message.chat_id,
      messageId: message.message_id,
    })

    const senderId =
      sender.sender_id?.user_id ?? sender.sender_id?.open_id ?? sender.sender_id?.union_id ?? ''

    // Phase 2: support text + image + file. Other types (audio/video/sticker/etc.)
    // are dropped with an info log so users can see the bot received the event
    // but can't process it.
    if (message.message_type === 'text') {
      this.acknowledgeInboundMessage(message.message_id)
      let text: string
      try {
        const parsed = JSON.parse(message.content) as { text?: string }
        text = parsed.text ?? ''
      } catch {
        text = ''
      }
      const cleaned = stripMentionPrefix(text)
      const msg: IncomingMessage = {
        platform: 'lark',
        channelId: message.chat_id,
        messageId: message.message_id,
        senderId,
        text: cleaned,
        timestamp: parseInt(message.create_time, 10) || Date.now(),
        raw: message,
      }
      this.dispatchMessage(msg)
      return
    }

    if (message.message_type === 'image' || message.message_type === 'file') {
      this.acknowledgeInboundMessage(message.message_id)
      void this.handleAttachmentMessage(data).catch((err: unknown) => {
        this.log.error('[lark] attachment handler failed', {
          event: 'lark_attachment_handler_failed',
          messageId: message.message_id,
          chatId: message.chat_id,
          error: err instanceof Error ? err.message : String(err),
        })
      })
      return
    }

    // Unhandled type — log and drop.
    this.log.info('[lark] dropped unsupported message type', {
      event: 'lark_unsupported_msg_type',
      messageType: message.message_type,
      messageId: message.message_id,
      chatId: message.chat_id,
    })
  }

  private async handleAttachmentMessage(data: LarkMessageEvent): Promise<void> {
    if (!this.client || !this.messageHandler) return
    const { sender, message } = data
    const senderId =
      sender.sender_id?.user_id ?? sender.sender_id?.open_id ?? sender.sender_id?.union_id ?? ''

    let parsedContent: { image_key?: string; file_key?: string; file_name?: string }
    try {
      parsedContent = JSON.parse(message.content)
    } catch {
      this.log.warn('[lark] could not parse attachment content', {
        event: 'lark_attachment_parse_failed',
        messageId: message.message_id,
      })
      return
    }

    const isImage = message.message_type === 'image'
    const fileKey = isImage ? parsedContent.image_key : parsedContent.file_key
    if (!fileKey) {
      this.log.warn('[lark] attachment missing key', {
        event: 'lark_attachment_no_key',
        messageId: message.message_id,
      })
      return
    }
    const fallbackName = isImage
      ? `image-${randomBytes(4).toString('hex')}.jpg`
      : parsedContent.file_name ?? `file-${randomBytes(4).toString('hex')}.bin`

    const localPath = await this.downloadResource({
      messageId: message.message_id,
      fileKey,
      filename: fallbackName,
      isImage,
    })
    if (!localPath) return

    const incomingAttachment: IncomingAttachment = {
      type: isImage ? 'photo' : 'document',
      fileId: fileKey,
      fileName: fallbackName,
      localPath,
    }
    const msg: IncomingMessage = {
      platform: 'lark',
      channelId: message.chat_id,
      messageId: message.message_id,
      senderId,
      text: '',
      attachments: [incomingAttachment],
      timestamp: parseInt(message.create_time, 10) || Date.now(),
      raw: message,
    }
    this.dispatchMessage(msg)
  }

  /**
   * Download a Lark resource (image or file) to a local temp path.
   *
   * Lark resource URLs require bearer-token auth; we can't hand a URL to the
   * router. Instead we stream the binary to a temp file and emit `localPath`,
   * matching the Telegram pattern.
   */
  private async downloadResource(args: {
    messageId: string
    fileKey: string
    filename: string
    isImage: boolean
  }): Promise<string | null> {
    if (!this.client) return null
    try {
      // The SDK exposes incoming message-resource downloads at
      // `im.messageResource.get`. Keep a legacy nested fallback for older
      // local mocks / SDK shapes, but prefer the generated client surface.
      const currentResource = this.client.im.messageResource
      const legacyResource = (this.client as unknown as {
        im?: {
          message: {
            resource?: {
              get?: (args: {
                path: { message_id: string; file_key: string }
                params: { type: 'image' | 'file' }
              }) => Promise<{ writeFile?: (path: string) => Promise<unknown>; file?: Buffer } & Record<string, unknown>>
            }
          }
        }
      }).im?.message?.resource
      if (!currentResource?.get && !legacyResource?.get) {
        throw new Error('Lark messageResource API is unavailable on SDK client')
      }

      const resourceArgs = {
        path: { message_id: args.messageId, file_key: args.fileKey },
        params: { type: args.isImage ? 'image' : 'file' },
      } as const
      const sdkResource = currentResource?.get
        ? await currentResource.get(resourceArgs)
        : await legacyResource!.get!(resourceArgs)

      const ext = extname(args.filename) || (args.isImage ? '.jpg' : '.bin')
      const localPath = join(tmpdir(), `lark-${randomBytes(8).toString('hex')}${ext}`)
      // Different SDK versions expose either `writeFile`, `file` (Buffer), or
      // a plain Node Readable. Handle the common shapes.
      if (typeof sdkResource.writeFile === 'function') {
        await sdkResource.writeFile(localPath)
        const size = statSync(localPath).size
        if (size > MAX_ATTACHMENT_BYTES) {
          try { unlinkSync(localPath) } catch {}
          throw new Error(`attachment exceeds ${MAX_ATTACHMENT_BYTES} bytes`)
        }
      } else if (sdkResource.file instanceof Buffer) {
        const buf = sdkResource.file
        if (buf.length > MAX_ATTACHMENT_BYTES) {
          throw new Error(`attachment exceeds ${MAX_ATTACHMENT_BYTES} bytes`)
        }
        writeFileSync(localPath, buf)
      } else {
        throw new Error('Lark resource SDK returned an unsupported shape')
      }
      return localPath
    } catch (err: unknown) {
      this.log.warn('[lark] resource download failed', {
        event: 'lark_resource_download_failed',
        messageId: args.messageId,
        fileKey: args.fileKey,
        error: err instanceof Error ? err.message : String(err),
      })
      return null
    }
  }

  private async handleCardAction(data: LarkCardActionEvent): Promise<void> {
    // Visibility log: if this never fires when the user presses a button,
    // the missing piece is on the Lark Open Platform side — schema-2.0
    // cards only emit `card.action.trigger` events when the app has the
    // **Card Callback Communication** subscription enabled under
    // Events & Callbacks (separate from `im.message.receive_v1`).
    const channelId = data.context?.open_chat_id ?? data.open_chat_id ?? ''
    this.log.info('[lark] card action received', {
      event: 'lark_card_action_received',
      chatId: channelId,
      tag: data.action?.tag,
      hasValue: data.action?.value !== undefined,
    })

    if (!this.buttonHandler) return
    const value = data.action?.value as
      | { buttonId?: string; messageId?: string; data?: string }
      | undefined
    if (!value?.buttonId || !value?.messageId) {
      this.log.warn('[lark] card action missing correlation ids', {
        event: 'lark_card_action_no_ids',
        operator: data.operator,
      })
      return
    }
    const operator = data.operator
    const senderId = operator?.user_id ?? operator?.open_id ?? operator?.union_id ?? ''

    const press: ButtonPress = {
      platform: 'lark',
      channelId,
      messageId: value.messageId,
      buttonId: value.buttonId,
      senderId,
      ...(value.data !== undefined ? { data: value.data } : {}),
    }
    await this.buttonHandler(press)
  }
}
