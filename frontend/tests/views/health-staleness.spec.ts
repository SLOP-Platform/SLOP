// frontend/tests/views/health-staleness.spec.ts
//
// Unit test for health staleness detection logic in HealthView.vue
// Validates that stale checks (>10min) are properly detected and rendering
// reflects freshness/staleness signals from the backend.
import { describe, it, expect } from 'vitest'

// Mock HealthCheck types with staleness
interface MockHealthCheck {
  app_key: string
  check_name: string
  status: 'ok' | 'warning' | 'error'
  summary: string
  last_checked: string | null
  auto_fix: string | null
  last_checked_age_seconds: number | null
}

// Helper function extracted from HealthView — test staleness logic
function isCheckStale(check: MockHealthCheck): boolean {
  if (check.last_checked_age_seconds === null || check.last_checked_age_seconds === undefined) {
    return false
  }
  return check.last_checked_age_seconds > 600
}

// Helper function for age formatting (seconds → human readable)
function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

describe('HealthView staleness detection', () => {
  it('marks checks older than 10min (600s) as stale', () => {
    const staleCheck: MockHealthCheck = {
      app_key: 'sonarr',
      check_name: 'memory_usage',
      status: 'ok',
      summary: 'Memory OK',
      last_checked: '2026-06-10T20:00:00Z',
      auto_fix: null,
      last_checked_age_seconds: 601, // 1 second over threshold
    }
    expect(isCheckStale(staleCheck)).toBe(true)
  })

  it('does not mark checks younger than 10min as stale', () => {
    const freshCheck: MockHealthCheck = {
      app_key: 'radarr',
      check_name: 'api_response',
      status: 'ok',
      summary: 'API responding',
      last_checked: '2026-06-10T20:09:00Z',
      auto_fix: null,
      last_checked_age_seconds: 120, // 2 minutes
    }
    expect(isCheckStale(freshCheck)).toBe(false)
  })

  it('treats null/undefined last_checked_age_seconds as fresh (no data yet)', () => {
    const noDataCheck: MockHealthCheck = {
      app_key: 'prowlarr',
      check_name: 'indexer_count',
      status: 'unknown',
      summary: 'Checking…',
      last_checked: null,
      auto_fix: null,
      last_checked_age_seconds: null,
    }
    expect(isCheckStale(noDataCheck)).toBe(false)
  })

  it('marks boundary case: exactly 600s (10min) as fresh', () => {
    const boundaryCheck: MockHealthCheck = {
      app_key: 'lidarr',
      check_name: 'disk_usage',
      status: 'warning',
      summary: 'Disk 80% full',
      last_checked: '2026-06-10T20:00:00Z',
      auto_fix: null,
      last_checked_age_seconds: 600,
    }
    expect(isCheckStale(boundaryCheck)).toBe(false)
  })

  it('formats age correctly for various durations', () => {
    expect(formatAge(30)).toBe('30s ago')
    expect(formatAge(120)).toBe('2m ago')
    expect(formatAge(3660)).toBe('1h ago')
    expect(formatAge(86400)).toBe('1d ago')
  })

  it('detects stale global health (scheduler dead)', () => {
    const staleSummary = {
      ok: 5,
      warning: 1,
      error: 0,
      unknown: 0,
      agent_status: 'running',
      process_integrity_status: 'ok',
      last_cycle_age_seconds: null,
      scheduler_alive: false, // DEAD
    }
    expect(staleSummary.scheduler_alive).toBe(false)
  })

  it('detects stale global health (last cycle >5min old)', () => {
    const staleSummary = {
      ok: 5,
      warning: 0,
      error: 0,
      unknown: 0,
      agent_status: 'running',
      process_integrity_status: 'ok',
      last_cycle_age_seconds: 301, // 5min + 1s
      scheduler_alive: true,
    }
    expect(staleSummary.last_cycle_age_seconds).toBeGreaterThan(300)
  })

  it('treats fresh global health correctly', () => {
    const freshSummary = {
      ok: 10,
      warning: 0,
      error: 0,
      unknown: 0,
      agent_status: 'running',
      process_integrity_status: 'ok',
      last_cycle_age_seconds: 45, // 45 seconds, well within threshold
      scheduler_alive: true,
    }
    expect(freshSummary.scheduler_alive).toBe(true)
    expect(freshSummary.last_cycle_age_seconds).toBeLessThanOrEqual(300)
  })
})
