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
    // Structural guard (CL-01 STREAM E / C3): ban raw `fetch('/api/...reset...')`
    // in .vue files. Raw fetch to the platform reset endpoints silently omits the
    // required confirm token (?confirm=RESET_PLATFORM / ?confirm=DESTROY_ALL_DATA),
    // producing a silent 400 in production. Reset MUST go through the typed client
    // (`platform.reset()` / `platform.resetFull()` in src/api/client.ts), which
    // always attaches the token. This prevents the bug class from recurring.
    files: ["src/**/*.vue"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "CallExpression[callee.name='fetch'] > Literal[value=/\\/api\\/.*reset/]",
          message:
            "Do not call fetch('/api/.../reset') directly — use the typed client (platform.reset() / platform.resetFull()) so the required ?confirm token is attached. Raw fetch omits it → silent 400.",
        },
        {
          selector:
            "CallExpression[callee.name='fetch'] > TemplateLiteral:has(TemplateElement[value.raw=/\\/api\\/.*reset/])",
          message:
            "Do not call fetch(`/api/.../reset`) directly — use the typed client (platform.reset() / platform.resetFull()) so the required ?confirm token is attached. Raw fetch omits it → silent 400.",
        },
      ],
    },
  },
  {
    ignores: ["dist/**", "node_modules/**", "*.min.js"],
  },
];
