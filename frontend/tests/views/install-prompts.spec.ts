// frontend/tests/views/install-prompts.spec.ts
//
// Unit tests for install_prompts wizard field behaviour (id=816).
// Tests the logic pattern used in SetupView.vue: collecting per-app
// prompt values and threading them into the install API call payload.
//
import { describe, it, expect } from 'vitest'

describe('install_prompts wizard field logic', () => {
  it('appsWithInstallPrompts filters to apps that have install_prompts', () => {
    // Simulates the appsWithInstallPrompts computed logic
    const catalogApps = [
      { key: 'opencode', display_name: 'OpenCode', install_prompts: [
        { key: 'workspace_path', label: 'Workspace', description: 'Path to mount', type: 'path', required: false, default: '' }
      ]},
      { key: 'sonarr', display_name: 'Sonarr', install_prompts: [] },
      { key: 'radarr', display_name: 'Radarr' }, // no install_prompts field
    ]
    const selectedKeys = ['opencode', 'sonarr', 'radarr']

    const appsWithInstallPrompts = selectedKeys
      .map(key => {
        const app = catalogApps.find(a => a.key === key)
        const prompts: any[] = (app as any)?.install_prompts ?? []
        return { key, display_name: (app as any)?.display_name ?? key, prompts }
      })
      .filter(a => a.prompts.length > 0)

    expect(appsWithInstallPrompts).toHaveLength(1)
    expect(appsWithInstallPrompts[0].key).toBe('opencode')
    expect(appsWithInstallPrompts[0].prompts[0].key).toBe('workspace_path')
  })

  it('user_volume_paths only included in request body when values are present', () => {
    // Simulates the JSON.stringify logic in installStacks()
    function buildRequestBody(userVolumePaths: Record<string, string>): object {
      return Object.keys(userVolumePaths).length > 0
        ? { user_volume_paths: userVolumePaths }
        : {}
    }

    // App with a value set
    expect(buildRequestBody({ workspace_path: '/home/user/projects' })).toEqual({
      user_volume_paths: { workspace_path: '/home/user/projects' }
    })

    // App with no value (user skipped optional prompt)
    expect(buildRequestBody({})).toEqual({})
  })

  it('installPromptValues stores per-app per-key values', () => {
    // Simulates form.installPromptValues reactive state
    const installPromptValues: Record<string, Record<string, string>> = {}

    // User fills in workspace for opencode
    if (!installPromptValues['opencode']) installPromptValues['opencode'] = {}
    installPromptValues['opencode']['workspace_path'] = '/home/user/projects'

    expect(installPromptValues['opencode']['workspace_path']).toBe('/home/user/projects')
    expect(installPromptValues['sonarr']).toBeUndefined()
  })

  it('default value falls back when user has not typed anything', () => {
    // Simulates the :value binding in the input element
    const installPromptValues: Record<string, Record<string, string>> = {}
    const prompt = { key: 'workspace_path', default: '/default/path' }
    const appKey = 'opencode'

    const resolvedValue =
      installPromptValues[appKey]?.[prompt.key] ?? prompt.default ?? ''

    expect(resolvedValue).toBe('/default/path')
  })

  it('user value overrides default', () => {
    const installPromptValues: Record<string, Record<string, string>> = {
      opencode: { workspace_path: '/home/user/code' }
    }
    const prompt = { key: 'workspace_path', default: '/default/path' }
    const appKey = 'opencode'

    const resolvedValue =
      installPromptValues[appKey]?.[prompt.key] ?? prompt.default ?? ''

    expect(resolvedValue).toBe('/home/user/code')
  })
})
