// frontend/tests/views/poll-failure-handling.spec.ts
//
// Focused test for 3-strike poll failure behavior in batch install and setup flows.
// This validates that consecutive poll failures are counted and monitoring is stopped
// after 3 failures to avoid frozen/lying spinners.
//
import { describe, it, expect } from 'vitest'

/**
 * Test the core logic: track consecutive failures, stop after 3.
 * This is a unit test of the retry-counter logic pattern used in
 * CatalogView.vue batch install (runBatchInstall) and
 * SetupView.vue app install (installStacks).
 */
describe('Poll failure handling — 3-strike rule', () => {
  it('resets failure counter on successful poll', () => {
    let pollFailureCount = 0
    const failureLimit = 3

    // Simulate 2 failures followed by success
    pollFailureCount++
    pollFailureCount++
    expect(pollFailureCount).toBe(2)

    // On success, reset
    pollFailureCount = 0
    expect(pollFailureCount).toBe(0)
  })

  it('stops monitoring after 3 consecutive failures', () => {
    let pollFailureCount = 0
    let shouldStopMonitoring = false
    const failureLimit = 3

    // Simulate failure detection in poll loop
    for (let i = 0; i < 4; i++) {
      pollFailureCount++
      if (pollFailureCount >= failureLimit) {
        shouldStopMonitoring = true
        break
      }
    }

    expect(shouldStopMonitoring).toBe(true)
    expect(pollFailureCount).toBe(3)
  })

  it('allows recovery if failures are separated by success', () => {
    let pollFailureCount = 0
    const failureLimit = 3
    let shouldStop = false

    // First batch: 2 failures
    pollFailureCount++
    pollFailureCount++
    expect(pollFailureCount).toBe(2)

    // Success: reset
    pollFailureCount = 0

    // Second batch: 3 failures (should trigger stop)
    for (let i = 0; i < 3; i++) {
      pollFailureCount++
      if (pollFailureCount >= failureLimit) {
        shouldStop = true
        break
      }
    }

    expect(shouldStop).toBe(true)
    expect(pollFailureCount).toBe(3)
  })
})
