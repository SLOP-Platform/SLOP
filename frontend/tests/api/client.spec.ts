// frontend/tests/api/client.spec.ts
//
// Vitest seed test — verifies the typed API client (`src/api/client.ts`)
// hits the canonical /api/v1/ surface and that the error-message
// coercion in `request<T>()` handles the various failure-body shapes
// the backend returns.
//
// Step 7 of the post-cleanup wire-up establishes Vitest infrastructure;
// this test is its load-bearing first user.
import { describe, it, expect, beforeEach, vi } from 'vitest'

// Capture every fetch URL the client tries.
let lastFetchUrl: string | null = null
let lastFetchInit: RequestInit | null = null
let nextResponse: Response = new Response('{}', { status: 200 })

beforeEach(() => {
  lastFetchUrl = null
  lastFetchInit = null
  nextResponse = new Response('{}', { status: 200 })
  globalThis.fetch = vi.fn((url: string | URL | Request, init?: RequestInit) => {
    lastFetchUrl = typeof url === 'string' ? url : (url as Request).url
    lastFetchInit = init ?? null
    return Promise.resolve(nextResponse)
  }) as unknown as typeof fetch
})

describe('api/client BASE constant', () => {
  it('hits /api/v1/<path>, not /api/<path> (ADR 0005 canonical surface)', async () => {
    const { platform } = await import('@/api/client')
    nextResponse = new Response(JSON.stringify({ status: 'ready' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
    await platform.status()
    expect(lastFetchUrl).toMatch(/^\/api\/v1\//)
    expect(lastFetchUrl).not.toMatch(/^\/api\/[^v]/)
  })

  it('attaches Content-Type: application/json on every call', async () => {
    const { platform } = await import('@/api/client')
    nextResponse = new Response('{}', { status: 200, headers: {
      'Content-Type': 'application/json',
    }})
    await platform.status()
    const headers = (lastFetchInit?.headers ?? {}) as Record<string, string>
    expect(headers['Content-Type']).toBe('application/json')
  })
})

describe('api/client error-message coercion', () => {
  it('throws Error with detail string when backend returns {detail: "msg"}', async () => {
    const { platform } = await import('@/api/client')
    nextResponse = new Response(
      JSON.stringify({ detail: 'invalid token' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } },
    )
    await expect(platform.status()).rejects.toThrow('invalid token')
  })

  it('throws Error with status text when backend returns no JSON body', async () => {
    const { platform } = await import('@/api/client')
    nextResponse = new Response('', { status: 502, statusText: 'Bad Gateway' })
    await expect(platform.status()).rejects.toThrow(/Bad Gateway|HTTP 502/)
  })

  it('extracts nested detail.message when backend returns structured error', async () => {
    const { platform } = await import('@/api/client')
    nextResponse = new Response(
      JSON.stringify({ detail: { message: 'rate limited', code: 'too_many' } }),
      { status: 429, headers: { 'Content-Type': 'application/json' } },
    )
    await expect(platform.status()).rejects.toThrow('rate limited')
  })
})
