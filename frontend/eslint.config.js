// ESLint flat config for SLOP Vue 3 frontend
// Core Rule 3.1: semantic checks on frontend, not just syntax
// Findings written to data/eslint_findings.json for LLM ingestion

import js from "@eslint/js";
import pluginVue from "eslint-plugin-vue";
import tsParser from "@typescript-eslint/parser";
import vueParser from "vue-eslint-parser";

export default [
  js.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  {
    files: ["src/**/*.vue", "src/**/*.js", "src/**/*.ts"],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tsParser,
        ecmaVersion: 2022,
        sourceType: "module",
        extraFileExtensions: [".vue"],
      },
      globals: {
        // Browser / DOM globals — RequestInit and other Web APIs are defined here
        window: "readonly",
        document: "readonly",
        fetch: "readonly",
        RequestInit: "readonly",
        Response: "readonly",
        AbortController: "readonly",
        EventSource: "readonly",
        HTMLElement: "readonly",
        HTMLImageElement: "readonly",
        HTMLInputElement: "readonly",
        MessageEvent: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        localStorage: "readonly",
        URL: "readonly",
        crypto: "readonly",
      },
    },
    rules: {
      // Real bug prevention
      "no-unused-vars": "error",
      "no-undef": "error",
      "no-unreachable": "error",
      "no-constant-condition": "error",

      // Vue-specific real bugs
      "vue/no-unused-vars": "error",
      "vue/no-unused-components": "error",
      "vue/no-mutating-props": "error",       // Core Rule 2.3: test behavior
      "vue/require-v-for-key": "error",
      "vue/valid-v-if": "error",
      "vue/no-async-in-computed-properties": "error",
      "vue/no-side-effects-in-computed-properties": "error",

      // Security
      "no-eval": "error",                     // Core Rule 3.8
      "no-implied-eval": "error",

      // Style (warnings only — don't block CI)
      "vue/component-name-in-template-casing": ["warn", "PascalCase"],
      "vue/html-self-closing": "warn",
      // Relax attribute-per-line limit; flat/recommended default is 1
      "vue/max-attributes-per-line": ["warn", { "singleline": { "max": 3 }, "multiline": { "max": 3 } }],
    },
  },
  {
    // Structural guard (#1219 / #1237, CL-01): ban raw `fetch('/api/…')` in views
    // and composables. State-mutating and read calls MUST route through the typed
    // client (`src/api/client.ts`) so URL/version/contract/auth-token handling stays
    // centralized — a raw fetch silently bypasses all of it (the original bug class:
    // a reset call that omitted its ?confirm token → silent 400). `src/api/**` is
    // exempt (client.ts is the one legitimate home for fetch).
    //
    // GENUINE raw-Response carve-outs (status-code branching, body-read-on-non-ok,
    // assume-success, install-orchestration polling) — where the throwing typed
    // client would change behavior — annotate IN-PLACE with:
    //   // eslint-disable-next-line no-restricted-syntax -- raw-response: <reason>
    // The carve-out inventory + rationale per site: .claude/run/c-3po-1237-carveout-map.md.
    // This annotation class is registered in tools/suppression_ledger.json.
    files: ["src/**/*.{vue,ts}"],
    ignores: ["src/api/**"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "CallExpression[callee.name='fetch'] > Literal[value=/\\/api\\//]",
          message:
            "Do not call fetch('/api/…') directly — route it through the typed client (src/api/client.ts). If this site genuinely needs raw Response semantics, annotate it: // eslint-disable-next-line no-restricted-syntax -- raw-response: <reason> (see .claude/run/c-3po-1237-carveout-map.md).",
        },
        {
          selector:
            "CallExpression[callee.name='fetch'] > TemplateLiteral:has(TemplateElement[value.raw=/\\/api\\//])",
          message:
            "Do not call fetch(`/api/…`) directly — route it through the typed client (src/api/client.ts). If this site genuinely needs raw Response semantics, annotate it: // eslint-disable-next-line no-restricted-syntax -- raw-response: <reason> (see .claude/run/c-3po-1237-carveout-map.md).",
        },
      ],
    },
  },
  {
    ignores: ["dist/**", "node_modules/**", "*.min.js"],
  },
];
