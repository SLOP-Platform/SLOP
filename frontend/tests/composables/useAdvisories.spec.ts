// frontend/tests/composables/useAdvisories.spec.ts
//
// Vitest unit test for the useAdvisories composable (#1213) — the consumer of
// GET /api/v1/health/advisories (#1089 read surface). Verifies: it hits the
// canonical advisories path, populates state on success, stays empty (non-fatal)
// on failure, and that annotationText handles object / string / null shapes.
import { describe, it, expect, beforeEach, vi } from 'vitest'

let lastFetchUrl: string | null = null
let nextResponse: Response = new Response('{}', { status: 200 })

beforeEach(() => {
  lastFetchUrl = null
  nextResponse = new Response('{}', { status: 200 })
  globalThis.fetch = vi.fn((url: string | URL | Request) => {
    lastFetchUrl = typeof url === 'string' ? url : (url as Request).url
    return Promise.resolve(nextResponse)
  }) as unknown as typeof fetch
})

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useAdvisories', () => {
  it('fetches the canonical /api/v1/health/advisories surface with the limit', async () => {
    const { useAdvisories } = await import('@/composables/useAdvisories')
    nextResponse = jsonResponse({ advisories: [] })
    const { fetchAdvisories } = useAdvisories()
    await fetchAdvisories(25)
    expect(lastFetchUrl).toMatch(/^\/api\/v1\/health\/advisories\?limit=25$/)
  })

  it('populates state + count on a successful fetch', async () => {
    const { useAdvisories } = await import('@/composables/useAdvisories')
    nextResponse = jsonResponse({
      advisories: [
        { id: 2, finding_id: 'health.cve.nginx', verdict: 'drift', annotation: { note: 'review me' }, provider: 'llm_review', created_at: 1 },
        { id: 1, finding_id: 'health.x', verdict: 'verified', annotation: null, provider: 'llm_review', created_at: 0 },
      ],
    })
    const { fetchAdvisories, advisories, advisoryCount, hasAdvisories } = useAdvisories()
    await fetchAdvisories()
    expect(advisoryCount.value).toBe(2)
    expect(hasAdvisories.value).toBe(true)
    expect(advisories.value[0].finding_id).toBe('health.cve.nginx')
  })

  it('stays empty (non-fatal) when the read surface errors', async () => {
    const { useAdvisories } = await import('@/composables/useAdvisories')
    nextResponse = new Response('boom', { status: 500 })
    const { fetchAdvisories, advisories, hasAdvisories } = useAdvisories()
    await fetchAdvisories()
    expect(advisories.value).toEqual([])
    expect(hasAdvisories.value).toBe(false)
  })

  it('annotationText handles object (summary/note), string, and null shapes', async () => {
    const { useAdvisories } = await import('@/composables/useAdvisories')
    const { annotationText } = useAdvisories()
    const base = { id: 1, finding_id: 'f', verdict: 'drift', provider: 'p', created_at: 0 }
    expect(annotationText({ ...base, annotation: { summary: 'hi' } })).toBe('hi')
    expect(annotationText({ ...base, annotation: { note: 'n' } })).toBe('n')
    expect(annotationText({ ...base, annotation: 'raw text' })).toBe('raw text')
    expect(annotationText({ ...base, annotation: null })).toBe('')
    // object with no preferred key falls back to JSON
    expect(annotationText({ ...base, annotation: { x: 1 } })).toBe('{"x":1}')
  })
})
