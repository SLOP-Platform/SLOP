import { describe, expect, it } from 'vitest'

type CloudProviderMeta = {
  label?: string
  configured?: boolean
}

function ensurePrimaryIsActive(primary: string, activeProviders: string[]): string[] {
  return Array.from(new Set([...activeProviders, primary].filter(Boolean)))
}

function sortProviders<T extends CloudProviderMeta>(providers: Record<string, T>): Array<[string, T]> {
  return Object.entries(providers)
    .sort(([, a], [, b]) => String(a.label || '').localeCompare(String(b.label || ''), undefined, { sensitivity: 'base' }))
}

function providerDisplayName(
  key: string,
  runtimeProviders: Record<string, CloudProviderMeta>,
  configuredProviders: Record<string, CloudProviderMeta>
): string {
  return runtimeProviders[key]?.label || configuredProviders[key]?.label || key
}

function toggleProvider(activeProviders: string[], key: string, primary: string): string[] {
  if (key === primary) return Array.from(new Set(activeProviders.filter(Boolean).concat(primary)))
  return activeProviders.includes(key)
    ? activeProviders.filter(provider => provider !== key)
    : activeProviders.concat(key)
}

describe('settings AI provider helpers', () => {
  it('sorts providers by label for compact multi-column card layouts', () => {
    const providers = {
      mistral: { label: 'Mistral' },
      groq: { label: 'Groq' },
      anthropic: { label: 'Anthropic' },
    }

    expect(sortProviders(providers).map(([key]) => key)).toEqual(['anthropic', 'groq', 'mistral'])
  })

  it('counts configured runtime providers accurately', () => {
    const runtimeProviders = {
      groq: { label: 'Groq', configured: true },
      mistral: { label: 'Mistral', configured: false },
      openrouter: { label: 'OpenRouter', configured: true },
    }

    expect(sortProviders(runtimeProviders).filter(([, meta]) => Boolean(meta.configured))).toHaveLength(2)
  })

  it('falls back to configured provider metadata when runtime labels are absent', () => {
    const runtimeProviders = {
      groq: { label: 'Groq' },
    }
    const configuredProviders = {
      openrouter: { label: 'OpenRouter' },
    }

    expect(providerDisplayName('groq', runtimeProviders, configuredProviders)).toBe('Groq')
    expect(providerDisplayName('openrouter', runtimeProviders, configuredProviders)).toBe('OpenRouter')
    expect(providerDisplayName('custom-edge', runtimeProviders, configuredProviders)).toBe('custom-edge')
  })

  it('keeps the selected primary provider active in the lower cloud providers surface', () => {
    expect(ensurePrimaryIsActive('groq', ['openrouter'])).toEqual(['openrouter', 'groq'])
    expect(ensurePrimaryIsActive('openrouter', ['openrouter', 'groq'])).toEqual(['openrouter', 'groq'])
  })

  it('does not allow the compact provider toggle to disable the primary provider', () => {
    expect(toggleProvider(['groq', 'openrouter'], 'groq', 'groq')).toEqual(['groq', 'openrouter'])
    expect(toggleProvider(['groq', 'openrouter'], 'openrouter', 'groq')).toEqual(['groq'])
    expect(toggleProvider(['groq'], 'mistral', 'groq')).toEqual(['groq', 'mistral'])
  })
})
