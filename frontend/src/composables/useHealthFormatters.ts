// Small, pure time/age formatters shared across the health UI. Extracted from
// HealthView.vue (#1213) to keep the view free of business logic (per the
// "NO business logic in view files" rule) and to let AdvisoriesPanel.vue reuse
// the same epoch→age rendering as the rest of the health surface.

export function useHealthFormatters() {
  // Render a DURATION in seconds as a compact "Xs/m/h/d ago" string.
  function formatAge(seconds: number): string {
    if (seconds < 60) return `${Math.floor(seconds)}s ago`
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
    return `${Math.floor(seconds / 86400)}d ago`
  }

  // Title-attribute helper — kept as a named alias so existing call sites and
  // intent (a hover timestamp) stay readable.
  function formatTimestamp(seconds: number): string {
    return formatAge(seconds)
  }

  // Render an EPOCH timestamp (seconds since 1970) as "X ago" relative to now.
  function formatAgeTimestamp(ts: number): string {
    const diff = Math.floor(Date.now() / 1000) - ts
    if (diff < 60) return `${diff}s ago`
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
    return `${Math.floor(diff / 86400)}d ago`
  }

  return { formatAge, formatTimestamp, formatAgeTimestamp }
}
