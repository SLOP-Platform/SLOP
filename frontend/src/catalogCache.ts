// Shared catalog cache — populated by App.vue prefetch on startup,
// consumed by CatalogView so it renders instantly with no API wait.
import type { CatalogEntry } from './api/client'

export let catalogCache: Record<string, CatalogEntry[]> | null = null
export let installedCache: Set<string> | null = null

export function setCatalogCache(data: Record<string, CatalogEntry[]>) {
  catalogCache = data
}
export function setInstalledCache(data: Set<string>) {
  installedCache = data
}
