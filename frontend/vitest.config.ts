/// <reference types="vitest" />
//
// Vitest config — step 7 of the post-cleanup wire-up.
//
// Extends the existing `vite.config.ts` (so the `@/` alias and Vue
// plugin transfer to test runs unchanged) and configures the test
// runner for component + unit tests under `frontend/tests/`.
//
// Run:
//   npm test          — single pass (CI gate)
//   npm run test:watch — re-run on change (dev loop)
//
// Test environment is jsdom — required for component tests that
// touch `document` / `window`. Pure logic tests (e.g. the
// `frontend/src/api/client.ts` smoke tests) work without it but
// the runner stays consistent.
import { defineConfig, mergeConfig } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      globals: true,
      environment: 'jsdom',
      include: ['tests/**/*.{spec,test}.{ts,vue}'],
      coverage: {
        reporter: ['text', 'html'],
        include: ['src/**/*.{ts,vue}'],
        exclude: ['src/**/*.d.ts'],
      },
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
  })
)
