// Shared app-level cache — primed by App.vue on startup, consumed by views
// so they render immediately without waiting for API calls on mount.
import type { AppStatus, HealthCheck, InfraSlot, RoutingConfig } from './api/client'

export let appsCache: AppStatus[] | null = null
export let healthCache: HealthCheck[] | null = null
export let infraSlotsCache: InfraSlot[] | null = null
export let routingCache: RoutingConfig[] | null = null

export function setAppsCache(d: AppStatus[])       { appsCache = d }
export function setHealthCache(d: HealthCheck[])   { healthCache = d }
export function setInfraSlotsCache(d: InfraSlot[]) { infraSlotsCache = d }
export function setRoutingCache(d: RoutingConfig[]) { routingCache = d }
