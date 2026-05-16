/**
 * Markdown → Lark `post` converter tests.
 *
 * Covers: plain-text path, every supported style, links, code blocks,
 * paragraph splitting, malformed input. Scope is the documented subset —
 * headers/lists/tables intentionally fall through as plain text.
 */
import { describe, expect, it } from 'bun:test'
import {
  formatForLarkPost,
  wrapAsTrivialPost,
  type LarkPost,
  type LarkPostElement,
} from '../format'

function zhContent(post: LarkPost): LarkPostElement[][] {
  expect(post.zh_cn).toBeDefined()
  return post.zh_cn!.content
}

describe('formatForLarkPost — plain text path', () => {
  it('returns kind: text for input with no formatting', () => {
    const result = formatForLarkPost('Hello, world. No markdown here.')
    expect(result.kind).toBe('text')
    if (result.kind === 'text') {
      expect(result.text).toBe('Hello, world. No markdown here.')
    }
  })

  it('preserves single newlines as plain text', () => {
    const result = formatForLarkPost('Line 1\nLine 2\nLine 3')
    expect(result.kind).toBe('text')
  })
})

describe('formatForLarkPost — inline styles', () => {
  it('maps **bold** to text element with bold style', () => {
    const result = formatForLarkPost('Some **bold** text')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    const elements = zhContent(result.post)[0]!
    const boldEl = elements.find((el) => el.tag === 'text' && el.text === 'bold')
    expect(boldEl).toBeDefined()
    expect((boldEl as { style?: string[] })?.style).toContain('bold')
  })

  it('maps *italic* and _italic_ to italic style', () => {
    const a = formatForLarkPost('Some *italic* text')
    expect(a.kind).toBe('post')
    if (a.kind === 'post') {
      const italicEl = zhContent(a.post)[0]!.find((el) => el.tag === 'text' && el.text === 'italic')
      expect((italicEl as { style?: string[] })?.style).toContain('italic')
    }

    const b = formatForLarkPost('Some _italic_ text')
    expect(b.kind).toBe('post')
    if (b.kind === 'post') {
      const italicEl = zhContent(b.post)[0]!.find((el) => el.tag === 'text' && el.text === 'italic')
      expect((italicEl as { style?: string[] })?.style).toContain('italic')
    }
  })

  it('maps ~~strike~~ to lineThrough style', () => {
    const result = formatForLarkPost('A ~~strike~~ word')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    const strikeEl = zhContent(result.post)[0]!.find(
      (el) => el.tag === 'text' && el.text === 'strike',
    )
    expect((strikeEl as { style?: string[] })?.style).toContain('lineThrough')
  })

  it('maps inline `code` to bold (documented fallback — Lark has no inline-code element)', () => {
    const result = formatForLarkPost('Use `npm install` here')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    const codeEl = zhContent(result.post)[0]!.find(
      (el) => el.tag === 'text' && el.text === 'npm install',
    )
    expect((codeEl as { style?: string[] })?.style).toContain('bold')
  })
})

describe('formatForLarkPost — links', () => {
  it('maps [label](url) to an `a` element with text + href', () => {
    const result = formatForLarkPost('Visit [our docs](https://example.com/docs) here')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    const linkEl = zhContent(result.post)[0]!.find((el) => el.tag === 'a') as
      | { tag: 'a'; text: string; href: string }
      | undefined
    expect(linkEl).toBeDefined()
    expect(linkEl?.text).toBe('our docs')
    expect(linkEl?.href).toBe('https://example.com/docs')
  })
})

describe('formatForLarkPost — code blocks', () => {
  it('extracts language and content from a fenced code block', () => {
    const result = formatForLarkPost('```python\nprint("hi")\n```')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    const para = zhContent(result.post)[0]!
    expect(para.length).toBe(1)
    const codeEl = para[0]! as { tag: string; language?: string; text?: string }
    expect(codeEl.tag).toBe('code_block')
    expect(codeEl.language).toBe('python')
    expect(codeEl.text).toBe('print("hi")')
  })

  it('falls back to no language when fence has no language hint', () => {
    const result = formatForLarkPost('```\nbare code\n```')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    const codeEl = zhContent(result.post)[0]![0]! as { tag: string; language?: string }
    expect(codeEl.tag).toBe('code_block')
    expect(codeEl.language).toBeUndefined()
  })
})

describe('formatForLarkPost — paragraphs', () => {
  it('splits on double newlines into multiple top-level entries', () => {
    const result = formatForLarkPost('First paragraph with **bold**.\n\nSecond paragraph.')
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    expect(zhContent(result.post).length).toBe(2)
  })
})

describe('formatForLarkPost — block Markdown', () => {
  it('passes Markdown tables through the native md tag', () => {
    const table = '| 姓名 | 年龄 |\n| --- | --- |\n| 张三 | 28 |'
    const result = formatForLarkPost(table)
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    expect(zhContent(result.post)).toEqual([[{ tag: 'md', text: table }]])
  })

  it('passes Markdown headings and lists through the native md tag', () => {
    const text = '### Title\n- one\n- two'
    const result = formatForLarkPost(text)
    expect(result.kind).toBe('post')
    if (result.kind !== 'post') return
    expect(zhContent(result.post)).toEqual([[{ tag: 'md', text }]])
  })
})

describe('wrapAsTrivialPost', () => {
  it('produces a single-paragraph post with one text element, no styles', () => {
    const post = wrapAsTrivialPost('Hello there')
    const content = zhContent(post)
    expect(content.length).toBe(1)
    expect(content[0]!.length).toBe(1)
    const el = content[0]![0]!
    expect(el.tag).toBe('text')
    if (el.tag === 'text') {
      expect(el.text).toBe('Hello there')
      expect(el.style).toBeUndefined()
    }
  })

  it('can emit an en_us locale for Lark global tenants', () => {
    const post = wrapAsTrivialPost('Hello there', 'en_us')
    expect(post.en_us?.content[0]?.[0]).toEqual({ tag: 'text', text: 'Hello there' })
    expect(post.zh_cn).toBeUndefined()
  })
})
