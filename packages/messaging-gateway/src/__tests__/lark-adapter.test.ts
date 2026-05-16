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
import { parseLarkCredentials, LarkAdapter } from '../adapters/lark/index'

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
} {
  const createCalls: unknown[] = []
  const updateCalls: unknown[] = []
  const patchCalls: unknown[] = []
  const reactionCalls: unknown[] = []
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
        create: async () => ({ file_key: 'file_1' }),
      },
      image: {
        create: async () => ({ image_key: 'img_1' }),
      },
    },
  }
  return { createCalls, updateCalls, patchCalls, reactionCalls }
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
