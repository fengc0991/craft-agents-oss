/**
 * LarkAdapter tests — focused on pure / unit-testable surface.
 *
 * The full adapter relies on Lark's WSClient (long-polling socket) and a
 * concrete `Client` instance, neither of which can be exercised in a unit
 * test without integration infrastructure. These tests cover the credential
 * parser and confirm the adapter's static contract (capabilities, platform).
 *
 * End-to-end behaviour (event dispatch, send/edit roundtrips) is verified
 * via manual smoke against a real Lark Custom App.
 */
import { describe, expect, it } from 'bun:test'
import { parseLarkCredentials, LarkAdapter, type LarkCredentials } from '../adapters/lark/index'

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void
  const promise = new Promise<void>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function installFakeLarkClient(adapter: LarkAdapter): {
  createCalls: unknown[]
  updateCalls: unknown[]
  patchCalls: unknown[]
  reactionCalls: unknown[]
  fileCalls: unknown[]
  imageCalls: unknown[]
} {
  const createCalls: unknown[] = []
  const updateCalls: unknown[] = []
  const patchCalls: unknown[] = []
  const reactionCalls: unknown[] = []
  const fileCalls: unknown[] = []
  const imageCalls: unknown[] = []
  ;(adapter as unknown as { client: unknown }).client = {
    im: {
      v1: {
        messageReaction: {
          create: async (args: unknown) => {
            reactionCalls.push(args)
            return { data: { reaction_id: `reaction_${reactionCalls.length}` } }
          },
        },
      },
      message: {
        create: async (args: unknown) => {
          createCalls.push(args)
          return { data: { message_id: `om_${createCalls.length}` } }
        },
        update: async (args: unknown) => {
          updateCalls.push(args)
          return {}
        },
        patch: async (args: unknown) => {
          patchCalls.push(args)
          return {}
        },
      },
      file: {
        create: async (args: unknown) => {
          fileCalls.push(args)
          return { data: { file_key: 'file_1' } }
        },
      },
      image: {
        create: async (args: unknown) => {
          imageCalls.push(args)
          return { data: { image_key: 'img_1' } }
        },
      },
    },
  }
  return { createCalls, updateCalls, patchCalls, reactionCalls, fileCalls, imageCalls }
}

function installFakeLarkUploads(adapter: LarkAdapter): {
  fileUploadCalls: Array<{ file: Buffer; filename: string }>
  imageUploadCalls: Array<{ file: Buffer; filename: string }>
} {
  const fileUploadCalls: Array<{ file: Buffer; filename: string }> = []
  const imageUploadCalls: Array<{ file: Buffer; filename: string }> = []
  ;(
    adapter as unknown as {
      uploadLarkFileWithFetch: (file: Buffer, filename: string) => Promise<string>
      uploadLarkImageWithFetch: (file: Buffer, filename: string) => Promise<string>
    }
  ).uploadLarkFileWithFetch = async (file, filename) => {
    fileUploadCalls.push({ file, filename })
    return 'file_1'
  }
  ;(
    adapter as unknown as {
      uploadLarkFileWithFetch: (file: Buffer, filename: string) => Promise<string>
      uploadLarkImageWithFetch: (file: Buffer, filename: string) => Promise<string>
    }
  ).uploadLarkImageWithFetch = async (file, filename) => {
    imageUploadCalls.push({ file, filename })
    return 'img_1'
  }
  return { fileUploadCalls, imageUploadCalls }
}

describe('parseLarkCredentials', () => {
  it('parses a valid JSON-encoded credential blob', () => {
    const creds = parseLarkCredentials(
      JSON.stringify({ appId: 'cli_abc', appSecret: 'secret', domain: 'lark' }),
    )
    expect(creds.appId).toBe('cli_abc')
    expect(creds.appSecret).toBe('secret')
    expect(creds.domain).toBe('lark')
  })

  it('accepts feishu domain', () => {
    const creds = parseLarkCredentials(
      JSON.stringify({ appId: 'cli_abc', appSecret: 'x', domain: 'feishu' }),
    )
    expect(creds.domain).toBe('feishu')
  })

  it('throws on missing token', () => {
    expect(() => parseLarkCredentials(undefined)).toThrow(/missing/i)
    expect(() => parseLarkCredentials('')).toThrow(/missing/i)
  })

  it('throws on non-JSON input', () => {
    expect(() => parseLarkCredentials('not-json')).toThrow(/JSON/i)
  })

  it('throws on missing appId or appSecret', () => {
    expect(() =>
      parseLarkCredentials(JSON.stringify({ appSecret: 'x', domain: 'lark' })),
    ).toThrow(/appId/i)
    expect(() =>
      parseLarkCredentials(JSON.stringify({ appId: 'cli_x', domain: 'lark' })),
    ).toThrow(/appSecret/i)
  })

  it('throws on invalid domain', () => {
    expect(() =>
      parseLarkCredentials(JSON.stringify({ appId: 'cli_x', appSecret: 'x', domain: 'larksuite' })),
    ).toThrow(/domain/i)
  })
})

describe('LarkAdapter — static contract', () => {
  it('declares platform = "lark"', () => {
    const adapter = new LarkAdapter()
    expect(adapter.platform).toBe('lark')
  })

  it('reports Phase 2 capabilities (edits, buttons, lark-post)', () => {
    const adapter = new LarkAdapter()
    expect(adapter.capabilities.messageEditing).toBe(true)
    expect(adapter.capabilities.inlineButtons).toBe(true)
    expect(adapter.capabilities.markdown).toBe('lark-post')
    expect(adapter.capabilities.maxButtons).toBe(10)
    expect(adapter.capabilities.webhookSupport).toBe(false)
  })

  it('starts disconnected before initialize', () => {
    const adapter = new LarkAdapter()
    expect(adapter.isConnected()).toBe(false)
  })

  it('sends text replies as post messages so later edits can render Markdown', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)

    await adapter.sendText('oc_1', 'thinking...')

    const create = calls.createCalls[0] as {
      data: { msg_type: string; content: string }
    }
    expect(create.data.msg_type).toBe('post')
    expect(JSON.parse(create.data.content)).toEqual({
      zh_cn: { content: [[{ tag: 'text', text: 'thinking...' }]] },
    })
  })

  it('falls back to text messages when Lark rejects the post payload', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)
    ;(adapter as unknown as {
      client: {
        im: {
          message: {
            create: (args: unknown) => Promise<{ data?: { message_id?: string } }>
          }
        }
      }
    }).client.im.message.create = async (args: unknown) => {
      calls.createCalls.push(args)
      const msgType = (args as { data?: { msg_type?: string } }).data?.msg_type
      if (msgType === 'post') {
        throw Object.assign(new Error('invalid post content'), {
          response: { status: 400, data: { code: 230001, msg: 'invalid post content' } },
        })
      }
      return { data: { message_id: 'om_text' } }
    }

    const text = '### 文件\n| path | note |\n| --- | --- |\n| config.json | ok |'
    const sent = await adapter.sendText('oc_1', text)

    expect(sent.messageId).toBe('om_text')
    expect(
      calls.createCalls.map((call) => (call as { data: { msg_type: string } }).data.msg_type),
    ).toEqual(['post', 'text'])
    const postUuid = (calls.createCalls[0] as { data: { uuid: string } }).data.uuid
    const textUuid = (calls.createCalls[1] as { data: { uuid: string } }).data.uuid
    expect(typeof postUuid).toBe('string')
    expect(textUuid).toBe(postUuid)
    const fallback = calls.createCalls[1] as { data: { content: string } }
    expect(JSON.parse(fallback.data.content)).toEqual({ text })
  })

  it('does not text-fallback ambiguous transport failures that may have delivered', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)
    ;(adapter as unknown as {
      client: {
        im: {
          message: {
            create: (args: unknown) => Promise<{ data?: { message_id?: string } }>
          }
        }
      }
    }).client.im.message.create = async (args: unknown) => {
      calls.createCalls.push(args)
      throw Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' })
    }

    let rejected = false
    try {
      await adapter.sendText('oc_1', '### 文件\n- config.json')
    } catch {
      rejected = true
    }

    expect(rejected).toBe(true)
    expect(calls.createCalls).toHaveLength(1)
    expect((calls.createCalls[0] as { data: { msg_type: string } }).data.msg_type).toBe('post')
  })

  it('keeps edited progress replies as post messages and renders fenced code blocks', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)

    const sent = await adapter.sendText('oc_1', 'thinking...')
    await adapter.editMessage('oc_1', sent.messageId, '```text\nhello world\n```')

    const update = calls.updateCalls[0] as {
      data: { msg_type: string; content: string }
    }
    expect(update.data.msg_type).toBe('post')
    expect(JSON.parse(update.data.content)).toEqual({
      zh_cn: { content: [[{ tag: 'code_block', language: 'text', text: 'hello world' }]] },
    })
  })

  it('sends non-image attachments as native Lark file messages', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)
    const uploads = installFakeLarkUploads(adapter)

    await adapter.sendFile('oc_1', Buffer.from('# poem'), '静夜思.md')

    expect(uploads.fileUploadCalls).toEqual([{ file: Buffer.from('# poem'), filename: '静夜思.md' }])
    expect(uploads.imageUploadCalls).toEqual([])

    const create = calls.createCalls[0] as {
      data: { msg_type: string; content: string }
    }
    expect(create.data.msg_type).toBe('file')
    expect(JSON.parse(create.data.content)).toEqual({ file_key: 'file_1' })
  })

  it('uploads generated files through Feishu multipart OpenAPI', async () => {
    const adapter = new LarkAdapter()
    const internals = adapter as unknown as {
      credentials: LarkCredentials | null
      apiBaseUrl: string
      tenantAccessTokenCache: { token: string; expiresAt: number } | null
      uploadLarkFileWithFetch(file: Buffer, filename: string): Promise<string>
    }
    internals.credentials = { appId: 'cli_1', appSecret: 'secret_1', domain: 'feishu' }
    internals.apiBaseUrl = 'https://open.feishu.cn'
    internals.tenantAccessTokenCache = null

    const originalFetch = globalThis.fetch
    const fetchCalls: Array<{
      input: Parameters<typeof fetch>[0]
      init: Parameters<typeof fetch>[1]
    }> = []
    globalThis.fetch = (async (input, init) => {
      fetchCalls.push({ input, init })
      const url = String(input)
      if (url.endsWith('/open-apis/auth/v3/tenant_access_token/internal')) {
        return new Response(
          JSON.stringify({ code: 0, tenant_access_token: 'tenant_token_1', expire: 7200 }),
          { status: 200 },
        )
      }
      if (url.endsWith('/open-apis/im/v1/files')) {
        return new Response(JSON.stringify({ code: 0, data: { file_key: 'file_fetch_1' } }), {
          status: 200,
        })
      }
      return new Response(JSON.stringify({ code: 404, msg: 'unexpected test URL' }), { status: 404 })
    }) as typeof fetch

    try {
      const key = await internals.uploadLarkFileWithFetch(Buffer.from('# poem'), '春晓.md')
      expect(key).toBe('file_fetch_1')

      expect(String(fetchCalls[0]?.input)).toBe(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
      )
      expect(fetchCalls[0]?.init?.method).toBe('POST')
      expect(JSON.parse(String(fetchCalls[0]?.init?.body))).toEqual({
        app_id: 'cli_1',
        app_secret: 'secret_1',
      })

      expect(String(fetchCalls[1]?.input)).toBe('https://open.feishu.cn/open-apis/im/v1/files')
      expect(fetchCalls[1]?.init?.method).toBe('POST')
      expect(fetchCalls[1]?.init?.headers).toEqual({ Authorization: 'Bearer tenant_token_1' })

      const form = fetchCalls[1]?.init?.body as FormData
      expect(form.get('file_type')).toBe('stream')
      expect(form.get('file_name')).toBe('春晓.md')
      const filePart = form.get('file') as { name?: string; size?: number } | null
      expect(filePart?.name).toBe('春晓.md')
      expect(filePart?.size).toBe(Buffer.byteLength('# poem'))
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  it('sends images as native Lark image messages', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)
    const uploads = installFakeLarkUploads(adapter)

    await adapter.sendFile('oc_1', Buffer.from('png'), 'cover.png')

    expect(uploads.imageUploadCalls).toEqual([{ file: Buffer.from('png'), filename: 'cover.png' }])
    expect(uploads.fileUploadCalls).toEqual([])

    const create = calls.createCalls[0] as {
      data: { msg_type: string; content: string }
    }
    expect(create.data.msg_type).toBe('image')
    expect(JSON.parse(create.data.content)).toEqual({ image_key: 'img_1' })
  })

  it('adds an OK reaction to inbound text messages as an immediate ack', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)
    adapter.onMessage(async () => {})

    await (
      adapter as unknown as {
        handleIncomingMessage(data: unknown): Promise<void>
      }
    ).handleIncomingMessage({
      sender: { sender_id: { user_id: 'user-1' } },
      message: {
        message_id: 'om_ack_1',
        chat_id: 'oc_1',
        chat_type: 'p2p',
        message_type: 'text',
        content: JSON.stringify({ text: 'hello' }),
        create_time: String(Date.now()),
      },
    })

    await Promise.resolve()

    expect(calls.reactionCalls).toEqual([
      {
        path: { message_id: 'om_ack_1' },
        data: { reaction_type: { emoji_type: 'OK' } },
      },
    ])
  })

  it('does not add duplicate ack reactions for redelivered inbound messages', async () => {
    const adapter = new LarkAdapter()
    const calls = installFakeLarkClient(adapter)
    adapter.onMessage(async () => {})
    const event = {
      sender: { sender_id: { user_id: 'user-1' } },
      message: {
        message_id: 'om_ack_dup',
        chat_id: 'oc_1',
        chat_type: 'p2p',
        message_type: 'text',
        content: JSON.stringify({ text: 'hello' }),
        create_time: String(Date.now()),
      },
    }

    await (
      adapter as unknown as {
        handleIncomingMessage(data: unknown): Promise<void>
      }
    ).handleIncomingMessage(event)
    await (
      adapter as unknown as {
        handleIncomingMessage(data: unknown): Promise<void>
      }
    ).handleIncomingMessage(event)

    await Promise.resolve()

    expect(calls.reactionCalls.length).toBe(1)
  })

  it('acks inbound text events without waiting for message processing to finish', async () => {
    const adapter = new LarkAdapter()
    const gate = deferred()
    const handledTexts: string[] = []
    adapter.onMessage(async (msg) => {
      handledTexts.push(msg.text)
      await gate.promise
    })

    const call = (
      adapter as unknown as {
        handleIncomingMessage(data: unknown): Promise<void>
      }
    ).handleIncomingMessage({
      sender: { sender_id: { user_id: 'user-1' } },
      message: {
        message_id: 'om_1',
        chat_id: 'oc_1',
        chat_type: 'p2p',
        message_type: 'text',
        content: JSON.stringify({ text: 'hello' }),
        create_time: String(Date.now()),
      },
    })

    const result = await Promise.race([
      call.then(() => 'returned'),
      new Promise<string>((resolve) => setTimeout(() => resolve('timeout'), 20)),
    ])
    expect(result).toBe('returned')
    expect(handledTexts).toEqual(['hello'])

    gate.resolve()
    await call
  })
})
