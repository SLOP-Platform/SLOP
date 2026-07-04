<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="mb-4">
      <h1 class="page-title">
        Settings
      </h1>
      <p class="page-subtitle">
        Health scheduler, LLM agent, notifications, and system profile
      </p>
    </div>

    <!-- Tab navigation always visible -->
    <div class="flex mb-4 border-b border-slate-200">
      <button
        v-for="tab in [{id:'health',label:'Health'},{id:'secrets',label:'Secrets'},{id:'ai',label:'AI'},{id:'updates',label:'Updates'},{id:'system',label:'System'},{id:'platform',label:'Platform'}]"
        :key="tab.id"
        :class="['flex-1 py-2 text-sm font-medium text-center whitespace-nowrap border-b-2 -mb-px transition-colors',
                 activeTab === tab.id ? 'border-orange-500 text-orange-600' : 'border-transparent text-slate-500 hover:text-slate-700']"
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
      </button>
    </div>
    <div
      v-if="loading"
      class="space-y-3 animate-pulse"
    >
      <div class="card card-body h-10 bg-slate-50" />
      <div class="card card-body h-10 bg-slate-50" />
      <div class="card card-body h-10 bg-slate-50" />
    </div>
    <template v-else>
      <div v-show="activeTab === 'health'">
        <div class="grid grid-cols-2 gap-3">
          <!-- Left col: Scheduler + Notifications + Disk + CF -->
          <section class="card">
            <div class="card-body space-y-3">
              <!-- Scheduler row -->
              <div class="flex items-center gap-3">
                <span class="text-xs font-medium text-slate-600 w-28 shrink-0">Check interval</span>
                <input
                  v-model.number="form.health_check_interval_secs"
                  type="number"
                  min="10"
                  max="3600"
                  class="input text-xs w-20"
                >
                <span class="text-xs text-slate-400">seconds</span>
                <span :class="['badge text-xs ml-auto', schedulerRunning ? 'badge-green' : 'badge-gray']">
                  {{ schedulerRunning ? 'running' : 'stopped' }}
                </span>
              </div>
              <div
                v-if="lastCycle"
                class="text-xs text-slate-400 pl-32"
              >
                Last: {{ lastCycleAgo }} ago · {{ lastCycle.apps_checked }} checked · {{ lastCycle.apps_healthy }} healthy
                <span
                  v-if="lastCycle.apps_degraded"
                  class="text-amber-500"
                > · {{ lastCycle.apps_degraded }} degraded</span>
              </div>
              <!-- Notifications row -->
              <div class="flex items-center gap-3 border-t border-slate-100 pt-3">
                <label class="flex items-center gap-2 w-28 shrink-0 cursor-pointer">
                  <input
                    v-model="form.ntfy_enabled"
                    type="checkbox"
                    class="w-3.5 h-3.5 rounded border-slate-300"
                  >
                  <span class="text-xs font-medium text-slate-600">ntfy alerts</span>
                </label>
                <input
                  v-model="form.ntfy_url"
                  class="input text-xs flex-1"
                  placeholder="http://ntfy:80"
                  :disabled="!form.ntfy_enabled"
                >
                <input
                  v-model="form.ntfy_topic"
                  class="input text-xs w-28"
                  placeholder="slop"
                  :disabled="!form.ntfy_enabled"
                >
              </div>
              <!-- Disk alerts row -->
              <div class="flex items-center gap-3 border-t border-slate-100 pt-3">
                <span class="text-xs font-medium text-slate-600 w-28 shrink-0">Disk alerts</span>
                <span class="text-xs text-slate-500">warn at</span>
                <input
                  v-model.number="form.disk_warn_percent"
                  type="number"
                  min="50"
                  max="95"
                  class="input text-xs w-16"
                >
                <span class="text-xs text-slate-400">%</span>
                <span class="text-xs text-slate-500 ml-2">error at</span>
                <input
                  v-model.number="form.disk_error_percent"
                  type="number"
                  min="50"
                  max="99"
                  class="input text-xs w-16"
                >
                <span class="text-xs text-slate-400">%</span>
              </div>
              <!-- CF row -->
              <div class="flex items-center gap-3 border-t border-slate-100 pt-3">
                <label class="flex items-center gap-2 cursor-pointer">
                  <input
                    v-model="form.cf_auto_register_hostnames"
                    type="checkbox"
                    class="w-3.5 h-3.5 rounded border-slate-300"
                  >
                  <span class="text-xs text-slate-600">Auto-register CF Tunnel hostnames on app install</span>
                </label>
              </div>
            </div>
          </section>
          <!-- Right col: LLM Health Agent -->
          <section class="card">
            <div class="card-header flex items-center justify-between">
              <div>
                <div class="font-semibold text-sm">
                  LLM Health Agent
                </div>
                <div class="text-xs text-slate-400 mt-0.5">
                  AI-powered failure diagnosis and auto-healing
                </div>
              </div>
              <label class="flex items-center gap-2 cursor-pointer">
                <input
                  v-model="form.llm_enabled"
                  type="checkbox"
                  class="rounded border-slate-300"
                >
                <span class="text-xs text-slate-600">Enabled</span>
              </label>
            </div>
            <div :class="['card-body space-y-4', !form.llm_enabled && 'opacity-50 pointer-events-none']">
              <!-- Provider tabs: Local / Cloud -->
              <div class="flex gap-1 p-1 bg-slate-100 rounded-lg w-fit">
                <button
                  v-for="t in ['local','cloud']"
                  :key="t"
                  :class="['px-3 py-1 text-xs rounded-md transition-all font-medium',
                           llmTab === t ? 'bg-white shadow-sm text-slate-800' : 'text-slate-500 hover:text-slate-700']"
                  @click="llmTab = t"
                >
                  {{ t === 'local' ? '🖥 Local' : '☁ Cloud' }}
                </button>
              </div>

              <!-- Local: Ollama / llama.cpp -->
              <template v-if="llmTab === 'local'">
                <div class="flex gap-2 mb-3">
                  <label
                    v-for="opt in [{v:'ollama',l:'Ollama'},{v:'llamacpp',l:'llama.cpp'}]"
                    :key="opt.v"
                    :class="['flex-1 flex items-center gap-2 p-2.5 rounded-lg border cursor-pointer transition-all',
                             form.llm_backend === opt.v ? 'border-sky-400 bg-sky-50' : 'border-slate-200 hover:border-slate-300']"
                  >
                    <input
                      v-model="form.llm_backend"
                      type="radio"
                      :value="opt.v"
                      class="sr-only"
                    >
                    <span class="text-sm font-medium">{{ opt.l }}</span>
                    <span
                      v-if="opt.v === 'ollama'"
                      class="ml-auto text-xs text-slate-400"
                    >Recommended</span>
                  </label>
                </div>
                <div class="grid grid-cols-2 gap-3">
                  <div>
                    <label class="label text-xs">{{ form.llm_backend === 'ollama' ? 'Ollama' : 'llama.cpp' }} URL</label>
                    <input
                      v-model="form.llm_ollama_url"
                      class="input text-xs"
                      :placeholder="form.llm_backend === 'ollama' ? 'http://ollama:11434' : 'http://localhost:8081'"
                    >
                  </div>
                  <div>
                    <label class="label text-xs">Model</label>
                    <input
                      v-model="form.llm_model"
                      class="input text-xs"
                      placeholder="e.g. phi4-mini"
                    >
                  </div>
                </div>
                <p class="text-xs text-slate-400">
                  <RouterLink
                    to="/models"
                    class="text-sky-500 hover:text-sky-600"
                  >
                    Manage models →
                  </RouterLink>
                  · Install Ollama from Catalog if not running.
                </p>
              </template>

              <!-- Cloud providers -->
              <template v-if="llmTab === 'cloud'">
                <div
                  v-if="!llmProviders"
                  class="text-xs text-slate-400"
                >
                  <button
                    class="btn-secondary btn-sm text-xs"
                    @click="loadLLMProviders"
                  >
                    Load providers
                  </button>
                </div>
                <template v-else>
                  <!-- Primary provider -->
                  <div>
                    <label class="label text-xs mb-2">Primary provider</label>
                    <div class="space-y-2">
                      <div
                        v-for="(p, key) in featuredProviders"
                        :key="key"
                        :class="['border rounded-lg p-3 cursor-pointer transition-all',
                                 llmPrimary === String(key) ? 'border-sky-400 bg-sky-50' : 'border-slate-200 hover:border-slate-300']"
                        @click="llmPrimary = String(key)"
                      >
                        <div class="flex items-center gap-3">
                          <input
                            type="radio"
                            :checked="llmPrimary === String(key)"
                            class="w-3.5 h-3.5"
                            readonly
                          >
                          <div class="flex-1 min-w-0">
                            <div class="flex items-center gap-2 flex-wrap">
                              <span class="text-sm font-medium">{{ p.label }}</span>
                              <span
                                v-if="p.free_tier"
                                class="text-xs bg-green-100 text-green-700 rounded-full px-1.5 py-0.5"
                              >Free tier</span>
                              <span
                                :class="['text-xs rounded-full px-1.5 py-0.5',
                                         p.privacy === 'eu' ? 'bg-blue-100 text-blue-700' :
                                         p.privacy === 'cn' ? 'bg-orange-100 text-orange-700' :
                                         'bg-slate-100 text-slate-500']"
                              >
                                {{ p.privacy?.toUpperCase() }}
                              </span>
                            </div>
                            <div class="text-xs text-slate-400 mt-0.5">
                              {{ p.notes }}
                            </div>
                          </div>
                        </div>
                        <!-- Expanded config when selected -->
                        <div
                          v-if="llmPrimary === String(key)"
                          class="mt-3 space-y-2 border-t border-slate-100 pt-3"
                        >
                          <div class="grid grid-cols-2 gap-2">
                            <div>
                              <label class="label text-xs">API Key</label>
                              <input
                                v-model="llmApiKeys[String(key)]"
                                type="password"
                                class="input text-xs font-mono"
                                :placeholder="p.env_key"
                              >
                            </div>
                            <div>
                              <label class="label text-xs">Model</label>
                              <!-- Loading state while fetching live models -->
                              <div
                                v-if="cloudModelFetching[String(key)]"
                                class="input text-xs text-slate-400 flex items-center gap-1"
                              >
                                <span class="animate-spin inline-block w-3 h-3 border border-slate-400 border-t-transparent rounded-full" />
                                Fetching models…
                              </div>
                              <!-- Error + free-text fallback -->
                              <div
                                v-else-if="cloudModelError[String(key)] && !(cloudModelList[String(key)]?.length)"
                                class="space-y-1"
                              >
                                <p class="text-xs text-amber-600">
                                  ⚠ Could not fetch models — enter model ID manually
                                </p>
                                <input
                                  v-model="llmModels[String(key)]"
                                  class="input text-xs font-mono"
                                  :placeholder="(p.models?.[0]?.id) || 'e.g. gpt-4o-mini'"
                                >
                              </div>
                              <!-- Live models from provider API -->
                              <select
                                v-else-if="cloudModelList[String(key)]?.length"
                                v-model="llmModels[String(key)]"
                                class="input text-xs"
                              >
                                <option
                                  v-for="m in cloudModelList[String(key)]"
                                  :key="m"
                                  :value="m"
                                >
                                  {{ m }}
                                </option>
                              </select>
                              <!-- Static fallback: provider's curated model list -->
                              <select
                                v-else
                                v-model="llmModels[String(key)]"
                                class="input text-xs"
                              >
                                <option
                                  v-for="m in (p.models || [])"
                                  :key="m.id"
                                  :value="m.id"
                                >
                                  {{ m.label }}
                                </option>
                              </select>
                            </div>
                          </div>
                          <div class="flex items-center gap-2">
                            <button
                              :disabled="llmTesting === String(key) || !llmApiKeys[String(key)]"
                              class="btn-secondary btn-sm text-xs"
                              @click="testLLMProvider(String(key))"
                            >
                              {{ llmTesting === key ? 'Testing…' : 'Test connection' }}
                            </button>
                            <span
                              v-if="llmTestResults[String(key)]?.ok"
                              class="text-xs text-green-600"
                            >
                              ✓ Connected · {{ llmTestResults[String(key)].latency_ms }}ms
                            </span>
                            <span
                              v-else-if="llmTestResults[String(key)]"
                              class="text-xs text-red-600"
                            >
                              ✗ {{ llmTestResults[String(key)].error }}
                            </span>
                            <a
                              v-if="p.env_key"
                              :href="PROVIDER_KEY_LINKS[String(key)]"
                              target="_blank"
                              rel="noopener"
                              class="text-xs text-sky-600 underline ml-auto"
                            >Get API key ↗</a>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>

                  <!-- Cascade fallback -->
                  <div class="border-t border-slate-100 pt-3">
                    <div class="flex items-center justify-between mb-2">
                      <label class="label text-xs mb-0">Fallback cascade</label>
                      <span class="text-xs text-slate-400">If primary fails, try these in order</span>
                    </div>
                    <div class="flex flex-wrap gap-1.5">
                      <template
                        v-for="(p, pkey) in llmProviders.providers"
                        :key="pkey"
                      >
                        <button
                          v-if="String(pkey) !== llmPrimary && !['z_ai','siliconflow','anthropic','openai','featherless'].includes(String(pkey))"
                          :class="['text-xs px-2.5 py-1 rounded-full border transition-all',
                                   llmCascade.includes(String(pkey))
                                     ? 'bg-slate-700 text-white border-slate-700'
                                     : 'bg-white text-slate-600 border-slate-200 hover:border-slate-400']"
                          @click="toggleCascade(String(pkey))"
                        >
                          {{ p.label }}
                        </button>
                      </template>
                    </div>
                    <p class="text-xs text-slate-400 mt-1.5">
                      Cascade order: <span class="font-mono">{{ llmPrimary }}{{ llmCascade.length ? ' → ' + llmCascade.join(' → ') : '' }}</span>
                    </p>
                  </div>

                  <!-- Advanced: paid / CN providers -->
                  <details class="border-t border-slate-100 pt-3">
                    <summary class="text-xs text-slate-400 cursor-pointer select-none hover:text-slate-600">
                      Advanced providers (paid-only or CN-hosted)
                    </summary>
                    <div class="mt-2 flex flex-wrap gap-1.5">
                      <button
                        v-for="pkey in ['anthropic','openai','featherless','z_ai','siliconflow']"
                        :key="pkey"
                        :class="['text-xs px-2.5 py-1 rounded-full border transition-all',
                                 llmCascade.includes(pkey)
                                   ? 'bg-slate-700 text-white border-slate-700'
                                   : 'bg-white text-slate-600 border-slate-200 hover:border-slate-400']"
                        @click="toggleCascade(pkey)"
                      >
                        {{ llmProviders.providers[pkey]?.label }}
                      </button>
                    </div>
                  </details>

                  <button
                    :disabled="savingLLM"
                    class="btn-primary btn-sm"
                    @click="saveLLMConfig"
                  >
                    {{ savingLLM ? 'Saving…' : 'Save LLM configuration' }}
                  </button>
                  <p
                    v-if="llmSaveOk"
                    class="text-xs text-green-600"
                  >
                    ✓ LLM configuration saved
                  </p>
                </template>
              </template>
            </div>
          </section>
        </div><!-- /2-col grid -->
      </div><!-- /health -->

      <div v-show="activeTab === 'secrets'">
        <!-- Secrets (.env file) -->
        <section class="card mb-3">
          <div class="card-header flex items-center justify-between">
            <div>
              <div class="font-semibold text-sm">
                Secrets &amp; Environment
              </div>
              <div class="text-xs text-slate-400 mt-0.5">
                Edits the <code class="font-mono bg-slate-100 px-1 rounded">{{ secretsEnvFile }}</code> file directly
              </div>
            </div>
            <button
              :disabled="loadingSecrets"
              class="btn-secondary btn-sm"
              @click="loadSecrets"
            >
              {{ loadingSecrets ? '…' : 'Load' }}
            </button>
          </div>
          <div
            v-if="secrets"
            class="card-body space-y-3"
          >
            <div class="grid grid-cols-1 gap-3">
              <div
                v-for="(meta, key) in secrets"
                :key="key"
                class="grid grid-cols-2 gap-3 items-center"
              >
                <label class="text-xs font-mono text-slate-600">{{ key }}</label>
                <div class="flex gap-2">
                  <input
                    v-model="secretEdits[key]"
                    :type="meta.is_sensitive && !secretVisible[key] ? 'password' : 'text'"
                    :placeholder="meta.is_set ? meta.value : 'not set'"
                    class="input text-xs font-mono flex-1"
                  >
                </div>
              </div>
            </div>
            <div class="border-t border-slate-100 pt-3 flex items-center gap-3">
              <button
                :disabled="savingSecrets"
                class="btn-primary btn-sm"
                @click="saveSecrets"
              >
                {{ savingSecrets ? 'Saving…' : 'Save secrets' }}
              </button>
              <span
                v-if="secretsSaved"
                class="text-xs text-green-600"
              >✓ Saved — restart service for changes to take effect</span>
              <span class="text-xs text-slate-400 ml-auto">
                Changes require a service restart:
                <code class="font-mono bg-slate-100 px-1 rounded">sudo systemctl restart slop</code>
              </span>
            </div>
          </div>
          <div
            v-else
            class="card-body text-center py-6 text-slate-400 text-sm"
          >
            Click Load to view and edit secrets
          </div>
        </section>
      </div><!-- /secrets -->

      <div v-show="activeTab === 'ai'">
        <!-- HuggingFace Token -->
        <section class="card mb-3">
          <div class="card-header">
            <div class="font-semibold text-sm">
              HuggingFace Token
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              Required for gated models — Phi-4, Llama, and others
            </div>
          </div>
          <div class="card-body">
            <div class="flex items-center gap-2">
              <input
                v-model="hfTokenInput"
                type="password"
                class="input text-xs flex-1 font-mono"
                placeholder="hf_xxxxxxxxxxxxxxxxxxxxxxxx"
                autocomplete="off"
              >
              <span
                v-if="hfTokenSaved"
                class="badge badge-green text-xs shrink-0"
              >Saved ✓</span>
              <button
                :disabled="!hfTokenInput || savingHFToken"
                class="btn-primary btn-sm text-xs shrink-0"
                @click="saveHFToken"
              >
                {{ savingHFToken ? 'Saving…' : 'Save' }}
              </button>
            </div>
            <p class="text-xs text-slate-400 mt-1.5">
              <a
                href="https://huggingface.co/settings/tokens"
                target="_blank"
                class="text-sky-500 hover:underline"
              >
                Get a token at huggingface.co ↗
              </a>
            </p>
          </div>
        </section>

        <!-- Cloud LLM Providers -->
        <section class="card mb-3">
          <div class="card-header flex items-center justify-between">
            <div>
              <div class="font-semibold text-sm">
                Cloud LLM Providers
              </div>
              <div class="text-xs text-slate-400 mt-0.5">
                Escalation cascade when local model needs help
              </div>
            </div>
            <button
              :disabled="loadingCloud"
              class="btn-secondary btn-sm"
              @click="loadCloudLLM"
            >
              {{ loadingCloud ? '…' : 'Load' }}
            </button>
          </div>
          <div
            v-if="cloudLLM"
            class="card-body space-y-4"
          >
            <!-- Cost monitor -->
            <div class="rounded-lg bg-slate-50 border border-slate-100 p-3">
              <div class="flex items-center justify-between mb-1">
                <span class="text-xs font-medium text-slate-600">Monthly spend</span>
                <span class="text-xs font-mono text-slate-800">
                  ${{ cloudLLM.total_spend_this_month.toFixed(4) }} / ${{ cloudLLM.monthly_limit_usd.toFixed(2) }} limit
                </span>
              </div>
              <div class="h-1.5 bg-slate-200 rounded-full overflow-hidden">
                <div
                  class="h-full bg-sky-500 transition-all"
                  :style="`width: ${Math.min(100, (cloudLLM.total_spend_this_month / cloudLLM.monthly_limit_usd) * 100)}%`"
                  :class="cloudLLM.total_spend_this_month / cloudLLM.monthly_limit_usd > 0.8 ? '!bg-amber-500' : ''"
                />
              </div>
              <div class="flex items-center gap-2 mt-2">
                <label class="text-xs text-slate-500 shrink-0">Monthly limit: $</label>
                <input
                  v-model.number="cloudMonthlyLimit"
                  type="number"
                  min="0"
                  step="0.50"
                  class="input text-xs w-24"
                >
                <button
                  class="btn-secondary btn-sm text-xs"
                  @click="saveCloudLimit"
                >
                  Save
                </button>
              </div>
            </div>
            <!-- Provider list -->
            <div class="space-y-2">
              <div
                v-for="(meta, provKey) in cloudLLM.providers"
                :key="provKey"
                class="flex items-center gap-3 px-3 py-2 rounded-lg border border-slate-100"
              >
                <div class="flex-1 min-w-0">
                  <div class="text-sm font-medium text-slate-800 flex items-center gap-2">
                    {{ meta.label }}
                    <span
                      v-if="meta.free_tier"
                      class="text-xs px-1.5 py-0.5 rounded-full bg-green-100 text-green-700 font-medium"
                    >FREE</span>
                    <span
                      v-if="meta.privacy === 'eu'"
                      class="text-xs px-1.5 py-0.5 rounded-full bg-blue-100 text-blue-700"
                    >EU</span>
                    <span
                      v-if="meta.privacy === 'cn'"
                      class="text-xs px-1.5 py-0.5 rounded-full bg-orange-100 text-orange-700"
                    >CN</span>
                  </div>
                  <div class="text-xs text-slate-400 truncate">
                    {{ meta.notes }}
                  </div>
                </div>
                <span :class="['text-xs font-medium shrink-0', meta.configured ? 'text-green-600' : 'text-slate-300']">
                  {{ meta.configured ? '✓ Key set' : 'No key' }}
                </span>
              </div>
            </div>
            <p class="text-xs text-slate-400">
              Add API keys in the <strong>Secrets</strong> section above (GROQ_API_KEY, CEREBRAS_API_KEY, GOOGLE_AI_API_KEY, etc.)
            </p>
            <!-- Recent calls -->
            <div v-if="cloudLLM.recent_calls.length">
              <p class="text-xs font-medium text-slate-500 mb-1">
                Recent calls
              </p>
              <div class="space-y-0.5">
                <div
                  v-for="call in cloudLLM.recent_calls.slice(0, 5)"
                  :key="call.created_at"
                  class="flex items-center gap-2 text-xs text-slate-400"
                >
                  <span class="font-medium text-slate-600">{{ call.provider }}</span>
                  <span>{{ call.total_tokens }} tokens</span>
                  <span class="text-slate-300">·</span>
                  <span>${{ call.cost_usd.toFixed(5) }}</span>
                  <span class="text-slate-300">·</span>
                  <span>{{ call.purpose }}</span>
                </div>
              </div>
            </div>
          </div>
          <div
            v-else
            class="card-body text-center py-6 text-slate-400 text-sm"
          >
            Click Load to configure cloud LLM providers
          </div>
        </section>


        <!-- AI Safety -->
        <section class="card mb-3">
          <div class="card-header">
            <div class="font-semibold text-sm">
              AI Safety
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              Control what the health agent can do automatically
            </div>
          </div>
          <div class="card-body space-y-4">
            <div class="rounded-lg bg-sky-50 border border-sky-100 p-3 text-xs text-sky-800">
              <strong>Three tiers:</strong>
              <strong> Observe</strong> — read-only, no suggestions.
              <strong> Suggest</strong> — proposes fixes, you approve (default).
              <strong> Act</strong> — executes automatically (explicit opt-in only).
            </div>
            <div
              v-if="safetyLevels"
              class="space-y-3"
            >
              <div
                v-for="(meta, actionType) in safetyLevels"
                :key="actionType"
              >
                <div class="flex items-center justify-between mb-1">
                  <div>
                    <span class="text-sm font-medium text-slate-700 capitalize">{{ actionType.replace(/_/g, ' ') }}</span>
                    <p class="text-xs text-slate-400">
                      {{ meta.description }}
                    </p>
                  </div>
                  <div class="flex gap-1">
                    <button
                      v-for="level in (meta.can_auto_act ? ['observe', 'suggest', 'act'] : ['observe', 'suggest'])"
                      :key="level"
                      :class="['text-xs px-2.5 py-1 rounded-full border font-medium transition-colors',
                               meta.level === level
                                 ? level === 'act' ? 'bg-red-500 border-red-500 text-white'
                                   : level === 'suggest' ? 'bg-sky-500 border-sky-500 text-white'
                                     : 'bg-slate-500 border-slate-500 text-white'
                                 : 'border-slate-200 text-slate-500 hover:border-slate-300']"
                      @click="setSafetyLevel(actionType, level)"
                    >
                      {{ level }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
            <div
              v-else
              class="text-sm text-slate-400 text-center py-3"
            >
              <button
                class="btn-secondary btn-sm"
                @click="loadSafety"
              >
                Load AI safety settings
              </button>
            </div>
          </div>
        </section>

        <!-- Pre-approval policy (N5 — tier × scope) -->
        <section class="card mb-3">
          <div class="card-header">
            <div class="font-semibold text-sm">
              Pre-Approval Policy
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              Decide which risk tiers the agent may act on WITHOUT asking, per app
            </div>
          </div>
          <div class="card-body space-y-4">
            <div class="rounded-lg bg-amber-50 border border-amber-100 p-3 text-xs text-amber-800">
              <strong>Tiers by blast radius:</strong>
              <strong> T0 Investigate</strong> — read-only (always on).
              <strong> T1 Reversible</strong>, <strong>T2 Recoverable</strong> — opt-in.
              <strong> T3 Irreversible</strong> — always asks; <em>cannot</em> be pre-approved.
            </div>

            <div
              v-if="preApproval"
              class="space-y-4"
            >
              <!-- Global per-tier defaults -->
              <div>
                <div class="text-xs font-semibold text-slate-500 uppercase mb-2">
                  Global defaults
                </div>
                <div class="space-y-2">
                  <div
                    v-for="t in preApproval.tiers"
                    :key="t.tier"
                    class="flex items-center justify-between"
                  >
                    <div>
                      <span class="text-sm font-medium text-slate-700">T{{ t.tier }} — {{ t.name }}</span>
                      <span
                        v-if="!t.pre_approvable"
                        class="ml-2 text-xs text-red-500"
                      >always asks</span>
                    </div>
                    <button
                      :disabled="!t.pre_approvable"
                      :class="['text-xs px-3 py-1 rounded-full border font-medium transition-colors',
                               !t.pre_approvable ? 'border-slate-200 text-slate-300 cursor-not-allowed'
                               : t.global_pre_approved ? 'bg-emerald-500 border-emerald-500 text-white'
                                 : 'border-slate-200 text-slate-500 hover:border-slate-300']"
                      @click="t.pre_approvable && setTierDefault(t.tier, !t.global_pre_approved)"
                    >
                      {{ t.global_pre_approved ? 'Pre-approved' : 'Ask first' }}
                    </button>
                  </div>
                </div>
              </div>

              <!-- Per-app overrides (read-out of the effective policy) -->
              <div v-if="Object.keys(preApproval.per_app || {}).length">
                <div class="text-xs font-semibold text-slate-500 uppercase mb-2">
                  Per-app overrides
                </div>
                <div
                  v-for="(over, app) in preApproval.per_app"
                  :key="app"
                  class="flex items-center justify-between text-xs text-slate-600 py-1"
                >
                  <span class="font-medium">{{ app }}</span>
                  <span>{{ Object.entries(over).map(([k, v]) => `T${k}:${v ? 'pre-approved' : 'ask'}`).join(', ') }}</span>
                </div>
              </div>
            </div>

            <div
              v-else
              class="text-sm text-slate-400 text-center py-3"
            >
              <button
                class="btn-secondary btn-sm"
                @click="loadPreApproval"
              >
                Load pre-approval policy
              </button>
            </div>
          </div>
        </section>
      </div><!-- /ai -->

      <div v-show="activeTab === 'system'">
        <!-- Custom App Install -->
        <section class="card mb-3">
          <div class="card-header flex items-center justify-between">
            <div>
              <div class="font-semibold text-sm">
                Add Custom App
              </div>
              <div class="text-xs text-slate-400 mt-0.5">
                Install any Docker app from a compose file or GitHub repo
              </div>
            </div>
            <span class="badge badge-yellow text-xs">custom</span>
          </div>
          <div class="card-body space-y-3">
            <!-- Tab switcher -->
            <div class="flex gap-1 border-b border-slate-100 pb-2">
              <button
                v-for="t in ['Paste YAML', 'GitHub repo']"
                :key="t"
                :class="['text-xs px-3 py-1 rounded transition-colors',
                         customAppTab === t ? 'bg-slate-100 text-slate-700 font-medium' : 'text-slate-400 hover:text-slate-600']"
                @click="customAppTab = t"
              >
                {{ t }}
              </button>
            </div>

            <!-- YAML tab -->
            <div
              v-if="customAppTab === 'Paste YAML'"
              class="space-y-3"
            >
              <textarea
                v-model="customYamlInput"
                class="input font-mono text-xs resize-y w-full"
                rows="12"
                placeholder="services:
  myapp:
    image: myimage:latest
    ports:
      - 8080:8080
    volumes:
      - /path/to/config/myapp:/config"
                @input="lintCustomYaml"
              />
              <div
                v-if="customLintResult"
                class="space-y-1.5"
              >
                <div
                  v-for="err in customLintResult.errors"
                  :key="err"
                  class="flex gap-2 text-xs text-red-600 bg-red-50 rounded px-2 py-1"
                >
                  <span class="shrink-0">✗</span><span>{{ err }}</span>
                </div>
                <div
                  v-for="warn in customLintResult.warnings"
                  :key="warn"
                  class="flex gap-2 text-xs text-amber-600 bg-amber-50 rounded px-2 py-1"
                >
                  <span class="shrink-0">⚠</span><span>{{ warn }}</span>
                </div>
                <div
                  v-if="customLintResult.valid"
                  class="flex gap-2 text-xs text-green-600 bg-green-50 rounded px-2 py-1"
                >
                  <span class="shrink-0">✓</span><span>Valid — ready to install</span>
                </div>
              </div>
              <div
                v-if="customLintResult?.manifest_preview"
                class="rounded-lg bg-slate-50 border border-slate-200 p-3"
              >
                <p class="text-xs font-medium text-slate-600 mb-2">
                  Preview
                </p>
                <div class="grid grid-cols-3 gap-x-4 gap-y-1 text-xs">
                  <template
                    v-for="(val, key) in customLintResult.manifest_preview"
                    :key="key"
                  >
                    <!-- Skip 'env' dict — shown in the missing-vars section below -->
                    <template v-if="String(key) !== 'env' && typeof val !== 'object'">
                      <span class="text-slate-400 font-mono truncate">{{ key }}</span>
                      <span class="text-slate-700 col-span-2 truncate">{{ val ?? '—' }}</span>
                    </template>
                  </template>
                </div>
              </div>
              <!-- Missing-vars form — shown when YAML references vars not in .env -->
              <div
                v-if="customMissingVars.length > 0"
                class="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-2"
              >
                <p class="text-xs font-semibold text-amber-800">
                  ⚠ {{ customMissingVars.length }} variable{{ customMissingVars.length > 1 ? 's' : '' }}
                  not found in your .env — provide values before installing:
                </p>
                <div
                  v-for="varName in customMissingVars"
                  :key="varName"
                  class="flex items-center gap-2"
                >
                  <label class="text-xs font-mono text-amber-900 w-48 shrink-0">{{ varName }}</label>
                  <input
                    v-model="customVarValues[varName]"
                    type="text"
                    :placeholder="`Enter ${varName}…`"
                    class="input input-sm text-xs flex-1 font-mono"
                  >
                </div>
                <p class="text-xs text-amber-700">
                  Values are passed as environment variables to the container.
                  Leave blank to skip (container may be misconfigured).
                </p>
              </div>

              <div class="flex gap-2">
                <button
                  class="btn-secondary btn-sm text-xs"
                  @click="clearCustomApp"
                >
                  Clear
                </button>
                <button
                  :disabled="!customLintResult?.valid || installingCustom"
                  class="btn-primary btn-sm text-xs flex-1"
                  @click="installCustomYaml"
                >
                  {{ installingCustom ? 'Installing…' : 'Install custom app' }}
                </button>
              </div>
            </div>

            <!-- GitHub tab -->
            <div
              v-if="customAppTab === 'GitHub repo'"
              class="space-y-3"
            >
              <div>
                <label class="label text-xs">GitHub repo URL</label>
                <input
                  v-model="customGithubUrl"
                  type="text"
                  class="input font-mono text-xs"
                  :disabled="!!githubRegisteredKey"
                  placeholder="https://github.com/user/repo"
                >
                <p class="text-xs text-slate-400 mt-1">
                  Repo must contain a <code>manifest.yaml</code> or <code>slop.yaml</code> at its root.
                </p>
              </div>

              <!-- Fetch error -->
              <div
                v-if="customGithubResult && !customGithubResult.ok"
                class="text-xs rounded-lg p-2 bg-red-50 text-red-700"
              >
                {{ customGithubResult.message || customGithubResult.detail }}
              </div>

              <!-- Post-fetch: registered key + optional missing-vars form + Install button -->
              <template v-if="githubRegisteredKey">
                <div class="text-xs rounded-lg p-2 bg-green-50 text-green-700">
                  ✓ Registered as <code class="font-mono">{{ githubRegisteredKey }}</code>
                  <span v-if="githubMissingVars.length === 0"> — ready to install</span>
                </div>

                <!-- Missing-vars form — same pattern as paste YAML path -->
                <div
                  v-if="githubMissingVars.length > 0"
                  class="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-2"
                >
                  <p class="text-xs font-semibold text-amber-800">
                    ⚠ {{ githubMissingVars.length }} variable{{ githubMissingVars.length > 1 ? 's' : '' }}
                    not found in your .env — provide values before installing:
                  </p>
                  <div
                    v-for="varName in githubMissingVars"
                    :key="varName"
                    class="flex items-center gap-2"
                  >
                    <label class="text-xs font-mono text-amber-900 w-48 shrink-0">{{ varName }}</label>
                    <input
                      v-model="githubVarValues[varName]"
                      type="text"
                      :placeholder="`Enter ${varName}…`"
                      class="input input-sm text-xs flex-1 font-mono"
                    >
                  </div>
                  <p class="text-xs text-amber-700">
                    Values are passed as environment variables to the container.
                    Leave blank to skip (container may be misconfigured).
                  </p>
                </div>
              </template>

              <div class="flex gap-2">
                <button
                  class="btn-secondary btn-sm text-xs"
                  @click="clearGithubApp"
                >
                  Clear
                </button>
                <!-- Before fetch: Fetch & register button -->
                <button
                  v-if="!githubRegisteredKey"
                  :disabled="!customGithubUrl || installingCustom"
                  class="btn-primary btn-sm text-xs flex-1"
                  @click="installCustomGithub"
                >
                  {{ installingCustom ? 'Fetching…' : 'Fetch & register' }}
                </button>
                <!-- After fetch: Install now button -->
                <button
                  v-else
                  :disabled="installingCustom"
                  class="btn-primary btn-sm text-xs flex-1"
                  @click="installGithubApp"
                >
                  {{ installingCustom ? 'Installing…' : 'Install now' }}
                </button>
              </div>
            </div>

            <div class="rounded-lg bg-amber-50 border border-amber-100 px-3 py-2 text-xs text-amber-700">
              Custom apps install with basic container monitoring. Use
              <RouterLink
                to="/catalog"
                class="underline font-medium"
              >
                Catalog
              </RouterLink>
              for fully managed apps with health checks, LLM diagnostics, and auto-fix.
            </div>
          </div>
        </section>








        <!-- System Health — Ghost Resources -->
        <section class="card mb-3">
          <div class="card-header flex items-center justify-between">
            <div>
              <div class="font-semibold text-sm">
                System Health
              </div>
              <div class="text-xs text-slate-400 mt-0.5">
                Ghost containers, fragments and volumes not tracked by S.L.O.P.
              </div>
            </div>
            <button
              :disabled="scanningGhosts"
              class="btn-secondary btn-sm"
              @click="loadGhosts"
            >
              {{ scanningGhosts ? 'Scanning…' : ghostData ? 'Rescan' : 'Scan' }}
            </button>
          </div>
          <div
            v-if="ghostData"
            class="card-body space-y-4"
          >
            <div
              v-if="ghostData.total === 0"
              class="text-sm text-green-600 text-center py-3"
            >
              ✓ No ghost resources detected
            </div>
            <template v-else>
              <!-- Ghost containers -->
              <div v-if="ghostData.containers.length">
                <p class="text-xs font-medium text-slate-500 mb-2 uppercase tracking-wider">
                  Ghost containers ({{ ghostData.containers.length }})
                </p>
                <div class="space-y-2">
                  <div
                    v-for="c in ghostData.containers"
                    :key="c.name"
                    class="flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2"
                  >
                    <div class="flex-1 min-w-0">
                      <div class="text-sm font-medium text-slate-800">
                        {{ c.name }}
                      </div>
                      <div class="text-xs text-slate-500">
                        {{ c.image }} · {{ c.status }}
                      </div>
                    </div>
                    <div class="flex gap-1 shrink-0">
                      <button
                        class="text-xs px-2 py-1 rounded bg-sky-100 text-sky-700 hover:bg-sky-200"
                        @click="ghostAction('container', c.name, 'adopt')"
                      >
                        Adopt
                      </button>
                      <button
                        class="text-xs px-2 py-1 rounded bg-red-100 text-red-600 hover:bg-red-200"
                        @click="ghostAction('container', c.name, 'remove')"
                      >
                        Remove
                      </button>
                      <button
                        class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-500 hover:bg-slate-200"
                        @click="ghostAction('container', c.name, 'ignore')"
                      >
                        Ignore
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Ghost fragments -->
              <div v-if="ghostData.fragments.length">
                <p class="text-xs font-medium text-slate-500 mb-2 uppercase tracking-wider">
                  Orphaned compose fragments ({{ ghostData.fragments.length }})
                </p>
                <div class="space-y-2">
                  <div
                    v-for="f in ghostData.fragments"
                    :key="f.name"
                    class="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2"
                  >
                    <div class="flex-1 min-w-0">
                      <div class="text-sm font-mono text-slate-700">
                        {{ f.name }}.yaml
                      </div>
                      <div class="text-xs text-slate-400">
                        {{ (f.size_bytes / 1024).toFixed(1) }} KB
                      </div>
                    </div>
                    <div class="flex gap-1 shrink-0">
                      <button
                        class="text-xs px-2 py-1 rounded bg-red-100 text-red-600 hover:bg-red-200"
                        @click="ghostAction('fragment', f.name, 'remove')"
                      >
                        Delete
                      </button>
                      <button
                        class="text-xs px-2 py-1 rounded bg-slate-100 text-slate-500 hover:bg-slate-200"
                        @click="ghostAction('fragment', f.name, 'ignore')"
                      >
                        Ignore
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Ghost volumes -->
              <div v-if="ghostData.volumes.length">
                <p class="text-xs font-medium text-slate-500 mb-2 uppercase tracking-wider">
                  Untracked volumes ({{ ghostData.volumes.length }})
                </p>
                <div class="space-y-2">
                  <div
                    v-for="v in ghostData.volumes"
                    :key="v.name"
                    class="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2"
                  >
                    <div class="flex-1 min-w-0">
                      <div class="text-sm font-mono text-slate-700">
                        {{ v.name }}
                      </div>
                      <div class="text-xs text-slate-400">
                        {{ v.driver }}
                      </div>
                    </div>
                    <button
                      class="text-xs px-2 py-1 rounded bg-red-100 text-red-600 hover:bg-red-200 shrink-0"
                      @click="ghostAction('volume', v.name, 'remove')"
                    >
                      Remove
                    </button>
                  </div>
                </div>
              </div>
            </template>
          </div>
          <div
            v-else-if="!scanningGhosts"
            class="card-body text-center text-sm text-slate-400 py-6"
          >
            Click Scan to detect untracked Docker resources
          </div>
        </section>


        <!-- Traefik settings -->
        <section class="card mb-3">
          <div class="card-header flex items-center justify-between">
            <div class="font-semibold text-sm">
              Traefik
            </div>
            <div class="text-xs text-slate-400">
              Restart Traefik to apply changes
            </div>
          </div>
          <div class="card-body space-y-3">
            <div
              v-if="!traefikSettings"
              class="text-center"
            >
              <button
                class="btn-secondary btn-sm"
                @click="loadTraefik"
              >
                Load Traefik settings
              </button>
            </div>
            <template v-else>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="label">Image</label>
                  <input
                    v-model="traefikSettings.image"
                    type="text"
                    class="input font-mono text-sm"
                    placeholder="traefik:v3.2"
                  >
                  <p class="text-xs text-slate-400 mt-0.5">
                    Use traefik:v3.2 with Docker 29+
                  </p>
                </div>
                <div>
                  <label class="label">Dashboard port</label>
                  <input
                    v-model.number="traefikSettings.dashboard_port"
                    type="number"
                    min="1024"
                    max="65535"
                    class="input"
                  >
                  <p class="text-xs text-slate-400 mt-0.5">
                    Default: 8081 (localhost only)
                  </p>
                </div>
              </div>
              <div>
                <label class="label">Dashboard host</label>
                <select
                  v-model="traefikSettings.dashboard_host"
                  class="input"
                >
                  <option value="127.0.0.1">
                    127.0.0.1 (localhost only — recommended)
                  </option>
                  <option value="0.0.0.0">
                    0.0.0.0 (all interfaces — requires auth)
                  </option>
                </select>
              </div>
              <button
                :disabled="savingTraefik"
                class="btn-primary btn-sm"
                @click="saveTraefik"
              >
                {{ savingTraefik ? 'Saving…' : 'Save Traefik settings' }}
              </button>
            </template>
          </div>
        </section>

        <!-- PUID/PGID overrides -->
        <section class="card mb-3">
          <div class="card-header">
            <div class="font-semibold text-sm">
              Per-app PUID/PGID overrides
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              Override global user/group IDs for specific apps
            </div>
          </div>
          <div class="card-body space-y-3">
            <p class="text-xs text-slate-500">
              Global PUID/PGID is set in the wizard. Override here for apps that need different user mappings.
              Format: <code class="font-mono bg-slate-100 px-1 rounded">appkey=UID:GID</code> (one per line)
            </p>
            <textarea
              v-model="puidOverrides"
              class="input font-mono text-xs resize-none"
              rows="4"
              placeholder="jellyfin=1001:1001&#10;plex=1002:1002"
            />
            <button
              :disabled="savingPuid"
              class="btn-primary btn-sm"
              @click="savePuidOverrides"
            >
              {{ savingPuid ? 'Saving…' : 'Save overrides' }}
            </button>
          </div>
        </section>


        <!-- System profile — compact -->
        <section class="card mb-3">
          <div class="card-header flex items-center justify-between">
            <div class="font-semibold text-sm">
              System
            </div>
            <button
              :disabled="loadingProfile"
              class="text-xs text-slate-400 hover:text-slate-600"
              @click="loadProfile"
            >
              {{ loadingProfile ? '…' : '↻ Refresh' }}
            </button>
          </div>
          <div
            v-if="profile"
            class="card-body !py-2 space-y-1.5"
          >
            <div class="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
              <div class="flex gap-2">
                <span class="text-slate-400 w-14">CPU</span><span class="font-medium text-slate-700">{{ profile.cpu_cores }}c · {{ profile.architecture }}</span>
              </div>
              <div class="flex gap-2">
                <span class="text-slate-400 w-14">RAM</span><span class="font-medium text-slate-700">{{ profile.total_ram_gb }}GB · {{ profile.free_ram_gb }}GB free</span>
              </div>
              <div
                v-if="profile.os"
                class="flex gap-2"
              >
                <span class="text-slate-400 w-14">OS</span><span class="font-medium text-slate-700">{{ profile.os.distro }} {{ profile.os.version }}</span>
              </div>
              <div
                v-if="profile.docker"
                class="flex gap-2"
              >
                <span class="text-slate-400 w-14">Docker</span><span class="font-medium text-slate-700">v{{ profile.docker.engine }} · {{ profile.docker.containers_running }} containers</span>
              </div>
            </div>
            <div
              v-if="profile.disks?.length"
              class="space-y-1 pt-1 border-t border-slate-100"
            >
              <div
                v-for="disk in profile.disks"
                :key="disk.path || disk.path"
                class="flex items-center gap-2"
              >
                <span class="text-xs text-slate-400 w-16 shrink-0 truncate">{{ disk.path || disk.path }}</span>
                <div class="flex-1 h-1 bg-slate-100 rounded-full overflow-hidden">
                  <div
                    class="h-full rounded-full"
                    :class="(disk.percent_used || disk.pct_used) > 90 ? 'bg-red-500' : (disk.percent_used || disk.pct_used) > 80 ? 'bg-amber-400' : 'bg-sky-400'"
                    :style="`width: ${disk.percent_used || disk.pct_used}%`"
                  />
                </div>
                <span class="text-xs text-slate-500 shrink-0">{{ disk.free_gb }}GB free</span>
              </div>
            </div>
          </div>
          <div
            v-else
            class="card-body text-center py-3 text-slate-400 text-xs"
          >
            <button
              class="btn-secondary btn-sm text-xs"
              @click="loadProfile"
            >
              Load system info
            </button>
          </div>
        </section>

        <!-- Docker socket -->
        <section class="card mb-3">
          <div class="card-body flex items-center gap-3">
            <span class="text-xs font-medium text-slate-600 w-28 shrink-0">Docker socket</span>
            <input
              v-model="dockerSocket"
              type="text"
              class="input text-xs font-mono flex-1"
              placeholder="/var/run/docker.sock"
            >
            <button
              :disabled="savingSocket"
              class="btn-secondary btn-sm text-xs shrink-0"
              @click="saveDockerSocket"
            >
              {{ savingSocket ? '…' : 'Save' }}
            </button>
          </div>
        </section>
      </div><!-- /system -->

      <!-- Updates tab -->
      <div v-show="activeTab === 'updates'">
        <!-- Global update policy -->
        <section class="card mb-3">
          <div class="card-body space-y-3">
            <div class="flex items-center justify-between">
              <div>
                <div class="text-sm font-medium text-slate-800">
                  Container Updates
                </div>
                <div class="text-xs text-slate-400">
                  Choose how container image updates are handled. Pinned apps are never updated automatically.
                </div>
              </div>
              <button
                :disabled="loadingUpdates"
                class="btn-secondary btn-sm text-xs shrink-0"
                @click="loadUpdates"
              >
                {{ loadingUpdates ? '…' : 'Refresh' }}
              </button>
            </div>
          </div>
        </section>

        <!-- Update check error notice -->
        <div
          v-if="updatesError"
          class="rounded-lg border border-amber-200 bg-amber-50 p-3 mb-3 text-xs text-amber-700"
        >
          {{ updatesError }}
        </div>

        <!-- Per-app table -->
        <section class="card mb-3">
          <div class="card-body">
            <div class="text-sm font-medium text-slate-800 mb-2">
              Per-app preferences
            </div>
            <table class="w-full text-xs">
              <thead>
                <tr class="text-left text-slate-400 border-b border-slate-100">
                  <th class="py-1.5 font-medium">
                    Container
                  </th>
                  <th class="py-1.5 font-medium">
                    Current image
                  </th>
                  <th class="py-1.5 font-medium">
                    Available update
                  </th>
                  <th class="py-1.5 font-medium text-center">
                    PIN
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="c in updateContainers"
                  :key="c.container_key"
                  class="border-b border-slate-50"
                >
                  <td class="py-1.5">
                    <span class="font-medium text-slate-700">{{ c.name || c.container_key }}</span>
                    <span
                      v-if="c.is_self"
                      class="text-xs text-orange-500 ml-1"
                    >(SLOP)</span>
                  </td>
                  <td class="py-1.5 font-mono text-slate-500 truncate max-w-xs">
                    {{ c.current_image || '—' }}
                  </td>
                  <td class="py-1.5">
                    <span
                      v-if="c.update_available"
                      class="text-amber-600 font-mono"
                    >{{ c.available_update }}</span>
                    <span
                      v-else
                      class="text-slate-400"
                    >up to date</span>
                  </td>
                  <td class="py-1.5 text-center">
                    <input
                      v-model="c.pinned"
                      type="checkbox"
                      class="w-3.5 h-3.5 rounded border-slate-300"
                      @change="onPinToggle(c)"
                    >
                  </td>
                </tr>
                <tr v-if="!updateContainers.length">
                  <td
                    colspan="4"
                    class="py-3 text-center text-slate-400"
                  >
                    {{ loadingUpdates ? 'Loading…' : 'No containers reported by the update service.' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- Save update prefs -->
        <section class="card">
          <div class="card-body flex items-center gap-4">
            <div class="flex-1">
              <div class="text-sm font-medium text-slate-800">
                Save Update Preferences
              </div>
              <div class="text-xs text-slate-400">
                Saves the default policy and per-app PIN choices.
              </div>
            </div>
            <span
              v-if="updatesSaved"
              class="text-sm text-green-600"
            >✓ Saved</span>
            <button
              :disabled="savingUpdates"
              class="btn-primary shrink-0"
              @click="saveUpdates"
            >
              {{ savingUpdates ? 'Saving…' : 'Save' }}
            </button>
          </div>
        </section>
      </div><!-- /updates -->

      <!-- Platform tab -->
      <div v-show="activeTab === 'platform'">
        <div class="grid grid-cols-3 gap-3 mb-3">
          <!-- Re-run wizard -->
          <section class="card">
            <div class="card-body">
              <div class="text-sm font-medium text-slate-800 mb-1">
                Re-run Setup Wizard
              </div>
              <div class="text-xs text-slate-400 mb-4">
                Reconfigure domain, certs, DNS, tunnels, or paths. Installed apps are not affected.
              </div>
              <RouterLink
                to="/setup?force=true"
                class="btn-secondary btn-sm text-xs"
              >
                Open wizard →
              </RouterLink>
            </div>
          </section>
          <!-- Reset platform -->
          <section class="card">
            <div class="card-body">
              <div class="text-sm font-medium text-slate-800 mb-1">
                Reset Platform
              </div>
              <div class="text-xs text-slate-400 mb-4">
                Clears platform status to pending. Traefik keeps running. Installed apps unaffected.
              </div>
              <button
                class="btn-secondary btn-sm text-xs text-red-600 hover:text-red-700"
                @click="showResetConfirm = true"
              >
                Reset platform
              </button>
            </div>
          </section>
          <!-- Remove all apps -->
          <section class="card">
            <div class="card-body">
              <div class="text-sm font-medium text-slate-800 mb-1">
                Remove All Apps
              </div>
              <div class="text-xs text-slate-400 mb-4">
                Stops and removes all managed containers. Config folders on disk are kept.
              </div>
              <button
                class="btn-secondary btn-sm text-xs text-red-600 hover:text-red-700"
                @click="showRemoveAllConfirm = true"
              >
                Remove all apps
              </button>
            </div>
          </section>
        </div>
        <!-- Quick Stacks management -->
        <section class="card mb-3">
          <div class="card-body">
            <div class="flex items-center justify-between mb-3">
              <div>
                <div class="text-sm font-medium text-slate-800">
                  Quick Stacks
                </div>
                <div class="text-xs text-slate-400">
                  Customise bundles shown in the setup wizard. Changes take effect on next wizard visit.
                </div>
              </div>
              <button
                class="btn-secondary btn-sm text-xs shrink-0"
                @click="stackAddMode = !stackAddMode"
              >
                {{ stackAddMode ? 'Cancel' : '+ New stack' }}
              </button>
            </div>

            <!-- Add stack form -->
            <div
              v-if="stackAddMode"
              class="rounded-lg border border-sky-200 bg-sky-50 p-3 mb-3 space-y-2"
            >
              <div class="text-xs font-medium text-sky-700 mb-1">
                New custom stack
              </div>
              <div class="flex gap-2">
                <input
                  v-model="stackAddForm.label"
                  type="text"
                  class="input input-sm text-xs flex-1"
                  placeholder="Stack label (e.g. My Media Stack)"
                >
                <input
                  v-model="stackAddForm.ram_note"
                  type="text"
                  class="input input-sm text-xs w-28"
                  placeholder="~2GB RAM"
                >
              </div>
              <input
                v-model="stackAddForm.app_keys"
                type="text"
                class="input input-sm text-xs w-full font-mono"
                placeholder="App keys, comma-separated (e.g. sonarr, radarr, prowlarr)"
              >
              <div class="text-xs text-slate-400">
                Use catalog slugs: sonarr, radarr, jellyfin, immich, vaultwarden, etc.
              </div>
              <div class="flex gap-2 mt-1">
                <button
                  :disabled="stackSaving || !stackAddForm.label.trim() || !stackAddForm.app_keys.trim()"
                  class="btn-primary btn-sm text-xs"
                  @click="addStack"
                >
                  {{ stackSaving ? 'Saving…' : 'Create stack' }}
                </button>
                <button
                  class="btn-secondary btn-sm text-xs"
                  @click="stackAddMode = false; stackAddForm = { label: '', app_keys: '', ram_note: '' }"
                >
                  Cancel
                </button>
              </div>
            </div>

            <!-- Stacks list -->
            <div class="space-y-2">
              <div
                v-for="stack in stacksList"
                :key="stack.id"
                class="rounded-lg border border-slate-200 overflow-hidden"
              >
                <!-- Row header -->
                <div class="flex items-center gap-2.5 px-3 py-2">
                  <div class="flex-1 min-w-0">
                    <div class="flex items-baseline gap-2">
                      <span class="text-sm font-medium text-slate-800">{{ stack.label }}</span>
                      <span
                        v-if="stack.is_custom && !stack.is_default_override"
                        class="text-xs text-sky-500"
                      >custom</span>
                      <span
                        v-if="stack.is_default_override"
                        class="text-xs text-amber-500"
                      >modified</span>
                      <span class="text-xs text-slate-400">{{ stack.ram_note }}</span>
                    </div>
                    <div class="text-xs text-slate-400 font-mono truncate">
                      {{ (stack.app_keys || []).join(', ') }}
                    </div>
                  </div>
                  <div class="flex items-center gap-1.5 shrink-0">
                    <button
                      class="btn-secondary btn-sm text-xs px-2"
                      @click="editStack(stack)"
                    >
                      Edit
                    </button>
                    <button
                      v-if="stack.is_custom || stack.is_default_override"
                      class="btn-secondary btn-sm text-xs px-2 text-amber-600"
                      :title="stack.is_default_override ? 'Reset to default' : 'Delete'"
                      @click="restoreStack(stack.id)"
                    >
                      {{ stack.is_default_override ? 'Reset' : '' }}
                    </button>
                    <button
                      class="btn-secondary btn-sm text-xs px-2 text-red-500 hover:text-red-600"
                      @click="deleteStack(stack.id)"
                    >
                      {{ stack.is_custom && !stack.is_default_override ? 'Delete' : 'Hide' }}
                    </button>
                  </div>
                </div>
                <!-- Inline edit form -->
                <div
                  v-if="stackEditId === stack.id"
                  class="border-t border-slate-100 bg-slate-50 p-3 space-y-2"
                >
                  <div class="flex gap-2">
                    <input
                      v-model="stackEditForm.label"
                      type="text"
                      class="input input-sm text-xs flex-1"
                      placeholder="Stack label"
                    >
                    <input
                      v-model="stackEditForm.ram_note"
                      type="text"
                      class="input input-sm text-xs w-28"
                      placeholder="~2GB RAM"
                    >
                  </div>
                  <input
                    v-model="stackEditForm.app_keys"
                    type="text"
                    class="input input-sm text-xs w-full font-mono"
                    placeholder="App keys, comma-separated"
                  >
                  <div class="flex gap-2">
                    <button
                      :disabled="stackSaving"
                      class="btn-primary btn-sm text-xs"
                      @click="saveStack(stack.id)"
                    >
                      {{ stackSaving ? 'Saving…' : 'Save' }}
                    </button>
                    <button
                      class="btn-secondary btn-sm text-xs"
                      @click="stackEditId = null"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              </div>
              <div
                v-if="!stacksList.length"
                class="text-xs text-slate-400 text-center py-2"
              >
                No stacks — all defaults are hidden. Use "+ New stack" to add one.
              </div>
            </div>
          </div>
        </section>

        <!-- Save settings -->
        <section class="card">
          <div class="card-body flex items-center gap-4">
            <div class="flex-1">
              <div class="text-sm font-medium text-slate-800">
                Save Settings
              </div>
              <div class="text-xs text-slate-400">
                Saves health scheduler interval, notifications, disk alerts, and CF settings.
              </div>
            </div>
            <span
              v-if="saveSuccess"
              class="text-sm text-green-600"
            >✓ Saved</span>
            <span
              v-if="saveError"
              class="text-sm text-red-600"
            >{{ saveError }}</span>
            <button
              :disabled="saving"
              class="btn-primary shrink-0"
              @click="save"
            >
              {{ saving ? 'Saving…' : 'Save settings' }}
            </button>
          </div>
        </section>
      </div><!-- /platform -->
    </template>

    <!-- Reset confirm modal -->
    <Teleport to="body">
      <div
        v-if="showResetConfirm"
        class="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="showResetConfirm = false"
        />
        <div class="relative card w-full max-w-sm mx-4 card-body">
          <h3 class="font-semibold text-slate-900">
            Reset platform?
          </h3>
          <p class="text-sm text-slate-500 mt-2">
            Resets the platform status to pending so you can re-run the wizard.
            Traefik keeps running. Installed apps are not affected.
          </p>
          <div class="flex gap-3 mt-4">
            <button
              class="btn-secondary flex-1"
              @click="showResetConfirm = false"
            >
              Cancel
            </button>
            <button
              class="btn-danger flex-1"
              @click="doReset"
            >
              Reset
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Remove all apps confirm modal -->
    <Teleport to="body">
      <div
        v-if="showRemoveAllConfirm"
        class="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="showRemoveAllConfirm = false"
        />
        <div class="relative card w-full max-w-sm mx-4 card-body">
          <h3 class="font-semibold text-slate-900">
            Remove all apps?
          </h3>
          <p class="text-sm text-slate-500 mt-2">
            Stops and removes all managed containers and their Docker networks.
            Config folders on disk are kept. This cannot be undone.
          </p>
          <div class="flex gap-3 mt-4">
            <button
              class="btn-secondary flex-1"
              @click="showRemoveAllConfirm = false"
            >
              Cancel
            </button>
            <button
              :disabled="removingAll"
              class="btn-danger flex-1"
              @click="doRemoveAll"
            >
              <template v-if="!removingAll">
                Remove all
              </template>
              <template v-else-if="removeTotal === 0">
                Removing…
              </template>
              <template v-else>
                Removing {{ removeCount }} / {{ removeTotal }}…
              </template>
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useToast } from '@/composables/useToast'
import { RouterLink, useRouter } from 'vue-router'
const toast = useToast()
import { settings, platform as platformApi, apps } from '../api/client'

interface FormState {
  health_check_interval_secs: number
  ntfy_topic: string
  ntfy_url: string
  ntfy_enabled: boolean
  llm_enabled: boolean
  llm_backend: string
  llm_ollama_url: string
  llm_llamacpp_url: string
  llm_model: string
  cf_auto_register_hostnames: boolean
  disk_warn_percent: number
  disk_error_percent: number
}

const loading = ref(true)
const router = useRouter()
const activeTab = ref('health')
watch(activeTab, (tab) => {
  if (tab === 'ai' && !llmProviders.value) loadLLMProviders()
  if (tab === 'updates' && !updatesLoaded.value) loadUpdates()
})

// ── Container updates ──────────────────────────────────────────────────
interface UpdateContainer {
  container_key: string
  name: string
  current_image: string
  available_update: string | null
  update_available: boolean
  notify_only: boolean
  pinned: boolean
  is_self: boolean
}
const updateContainers = ref<UpdateContainer[]>([])
const loadingUpdates = ref(false)
const savingUpdates = ref(false)
const updatesSaved = ref(false)
const updatesError = ref('')
const updatesLoaded = ref(false)

async function loadUpdates() {
  loadingUpdates.value = true
  updatesError.value = ''
  try {
    const r = await fetch('/api/v1/updates/status')
    if (r.status === 503) {
      updatesError.value = 'Update check unavailable — Docker is not reachable.'
      updateContainers.value = []
    } else if (r.ok) {
      const data = await r.json()
      updateContainers.value = data.containers || []
    } else {
      updatesError.value = 'Could not load update status.'
    }
  } catch {
    updatesError.value = 'Could not reach the update service. Check Docker is running.'
  } finally {
    loadingUpdates.value = false
    updatesLoaded.value = true
  }
}

function onPinToggle(c: UpdateContainer) {
  // Warn before unpinning SLOP's own container — auto-updating it can restart
  // the manager out from under a running operation.
  if (c.is_self && !c.pinned) {
    const ok = window.confirm(
      'SLOP manages your stack. Unpinning it allows automatic updates that can ' +
      'restart the manager mid-operation. Unpin anyway?'
    )
    if (!ok) {
      c.pinned = true
    }
  }
}

async function saveUpdates() {
  savingUpdates.value = true
  updatesSaved.value = false
  try {
    const preferences: Record<string, { notify_only: boolean; pinned: boolean }> = {}
    for (const c of updateContainers.value) {
      preferences[c.container_key] = {
        notify_only: true,
        pinned: c.pinned,
      }
    }
    const r = await fetch('/api/v1/updates/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preferences }),
    })
    if (r.ok) {
      updatesSaved.value = true
      setTimeout(() => { updatesSaved.value = false }, 2500)
    } else {
      toast.error('Failed to save update preferences')
    }
  } catch {
    toast.error('Failed to save update preferences')
  } finally {
    savingUpdates.value = false
  }
}
const customAppTab = ref('Paste YAML')
const customYamlInput = ref('')
const customLintResult = ref<any>(null)
const customMissingVars = ref<string[]>([])       // ${VAR} refs not in .env
const customVarValues = ref<Record<string, string>>({})  // user-filled values for missing vars
const customGithubUrl = ref('')
const customGithubResult = ref<any>(null)
const githubRegisteredKey = ref<string | null>(null)   // key returned after Fetch & register
const githubMissingVars = ref<string[]>([])            // ${VAR} refs not in .env
const githubVarValues = ref<Record<string, string>>({}) // user-filled values for missing vars
const installingCustom = ref(false)
let _customLintTimer: ReturnType<typeof setTimeout> | null = null
const hfTokenInput = ref('')
const hfTokenSaved = ref(false)
const savingHFToken = ref(false)
const secretVisible = ref<Record<string, boolean>>({})
const saving = ref(false)
const saveSuccess = ref(false)
const saveError = ref<string | null>(null)
const schedulerRunning = ref(false)
const lastCycle = ref<Record<string, any> | null>(null)
const profile = ref<Record<string, any> | null>(null)
const loadingProfile = ref(false)
const showResetConfirm = ref(false)
const showRemoveAllConfirm = ref(false)
const removingAll = ref(false)
const removeCount = ref(0)
const removeTotal = ref(0)

const savingTraefik = ref(false)
const puidOverrides = ref('')
const savingPuid = ref(false)
const dockerSocket = ref('/var/run/docker.sock')
const savingSocket = ref(false)
const safetyLevels = ref<Record<string, any> | null>(null)
const preApproval = ref<{ tiers: any[]; per_app: Record<string, any>; note?: string } | null>(null)
const ghosts = ref<any>(null)
const loadingGhosts = ref(false)
const cloudLLM = ref<any>(null)
const llmTab = ref('local')
const llmProviders = ref<any>(null)
const llmPrimary = ref('groq')
const llmApiKeys = ref<Record<string,string>>({})
const llmModels = ref<Record<string,string>>({})
const llmCascade = ref<string[]>([])
const llmTesting = ref<string|null>(null)
const llmTestResults = ref<Record<string,any>>({})
const savingLLM = ref(false)
const llmSaveOk = ref(false)
// Dynamic cloud model fetching
const cloudModelList    = ref<Record<string, string[]>>({})
const cloudModelFetching = ref<Record<string, boolean>>({})
const cloudModelError   = ref<Record<string, string | null>>({})

const PROVIDER_KEY_LINKS: Record<string,string> = {
  groq:       'https://console.groq.com/keys',
  cerebras:   'https://cloud.cerebras.ai/',
  openrouter: 'https://openrouter.ai/keys',
  mistral:    'https://console.mistral.ai/api-keys',
  cohere:     'https://dashboard.cohere.com/api-keys',
  google:     'https://aistudio.google.com/app/apikey',
  anthropic:  'https://console.anthropic.com/settings/keys',
  openai:     'https://platform.openai.com/api-keys',
  featherless:'https://featherless.ai/account',
}
// providerKeyLinks available as PROVIDER_KEY_LINKS const

const FEATURED_ORDER = ['groq','cerebras','openrouter','mistral','cohere','google']
const featuredProviders = computed(() => {
  if (!llmProviders.value?.providers) return {}
  return Object.fromEntries(
    FEATURED_ORDER
      .filter(k => llmProviders.value.providers[k])
      .map(k => [k, llmProviders.value.providers[k]])
  )
})


const loadingCloud = ref(false)
const cloudMonthlyLimit = ref(1.00)
const traefikSettings = ref<any>(null)
const ghostData = ref<any>(null)
const scanningGhosts = ref(false)
const secrets = ref<Record<string, any> | null>(null)
const secretsEnvFile = ref('')
const secretEdits = ref<Record<string, string>>({})
const loadingSecrets = ref(false)
const savingSecrets = ref(false)
const secretsSaved = ref(false)

const form = ref<FormState>({
  health_check_interval_secs: 30,
  ntfy_topic: 'slop',
  ntfy_url: 'http://ntfy:80',
  ntfy_enabled: true,
  llm_enabled: true,
  llm_backend: 'ollama',
  llm_ollama_url: 'http://ollama:11434',
  llm_llamacpp_url: 'http://localhost:8081',
  llm_model: 'phi4-mini',
  cf_auto_register_hostnames: false,
  disk_warn_percent: 80,
  disk_error_percent: 90,
})

const lastCycleAt = ref<number | null>(null)
const lastCycleAgo = computed(() => {
  if (!lastCycleAt.value) return 'never'
  const secs = Math.round(Date.now() / 1000 - lastCycleAt.value)
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`
  return `${Math.floor(secs / 86400)}d`
})

async function loadLLMProviders() {
  try {
    const r = await fetch('/api/v1/health/llm-providers')
    llmProviders.value = await r.json()
    // Pre-select default models
    for (const [k, p] of Object.entries(llmProviders.value.providers as any)) {
      const rec = (p as any).models?.find((m: any) => m.recommended)
      if (rec && !llmModels.value[k]) llmModels.value[k] = rec.id
    }
    // Load existing config
    try {
      const s = await fetch('/api/v1/settings')
      const d = await s.json()
      const cfg = JSON.parse(d.llm_agent_config || '{}')
      if (cfg.provider && cfg.provider !== 'ollama') {
        llmTab.value = 'cloud'
        llmPrimary.value = cfg.provider
        if (cfg.api_key) llmApiKeys.value[cfg.provider] = cfg.api_key
        if (cfg.model) llmModels.value[cfg.provider] = cfg.model
        llmCascade.value = cfg.cascade || []
      }
    } catch { /* intentional: per-provider config missing is non-fatal */ }
  } catch (e) {
    toast.error('Could not load LLM providers.', String(e))
  }
}

async function testLLMProvider(key: string) {
  llmTesting.value = key
  llmTestResults.value[key] = null
  try {
    const r = await fetch('/api/v1/health/llm-test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: key,
        api_key: llmApiKeys.value[key] || '',
        model: llmModels.value[key] || '',
      }),
    })
    llmTestResults.value[key] = await r.json()
  } catch (e) {
    llmTestResults.value[key] = { ok: false, error: String(e), latency_ms: 0 }
  } finally {
    llmTesting.value = null
  }
}

async function saveLLMConfig() {
  savingLLM.value = true
  llmSaveOk.value = false
  try {
    const cfg = llmTab.value === 'cloud'
      ? {
          provider: llmPrimary.value,
          api_key: llmApiKeys.value[llmPrimary.value] || '',
          model: llmModels.value[llmPrimary.value] || '',
          cascade: llmCascade.value,
        }
      : {
          provider: form.value.llm_backend,
          api_key: '',
          model: form.value.llm_model,
          cascade: [],
        }
    await fetch('/api/v1/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ llm_agent_config: JSON.stringify(cfg) }),
    })
    llmSaveOk.value = true
    setTimeout(() => { llmSaveOk.value = false }, 3000)
    toast.success('LLM configuration saved.')
  } catch (e) {
    toast.error('Could not save LLM config.', String(e))
  } finally {
    savingLLM.value = false }
}

function toggleCascade(key: string) {
  const i = llmCascade.value.indexOf(key)
  if (i >= 0) llmCascade.value.splice(i, 1)
  else llmCascade.value.push(key)
}

// Fetch live model list from the provider's /v1/models endpoint
async function fetchCloudModels(key: string) {
  const apiKey = llmApiKeys.value[key] || ''
  if (apiKey.length < 10) return
  cloudModelFetching.value[key] = true
  cloudModelError.value[key] = null
  try {
    const r = await fetch(
      `/api/v1/platform/cloud-models?provider=${encodeURIComponent(key)}&api_key=${encodeURIComponent(apiKey)}`
    )
    const data = await r.json()
    if (data.error) {
      cloudModelError.value[key] = data.error
      cloudModelList.value[key] = []
    } else {
      cloudModelList.value[key] = data.models || []
      // Keep existing selection if it's in the new list; else pick first
      if (cloudModelList.value[key].length && !cloudModelList.value[key].includes(llmModels.value[key])) {
        llmModels.value[key] = cloudModelList.value[key][0]
      }
    }
  } catch (e) {
    cloudModelError.value[key] = String(e)
    cloudModelList.value[key] = []
  } finally {
    cloudModelFetching.value[key] = false
  }
}

// Re-fetch when the primary provider or its API key changes
watch(
  [llmPrimary, llmApiKeys],
  ([newProvider]) => {
    const apiKey = llmApiKeys.value[String(newProvider)] || ''
    if (apiKey.length >= 10) fetchCloudModels(String(newProvider))
  },
  { deep: true }
)

async function loadSecrets() {
  loadingSecrets.value = true
  try {
    const res = await fetch('/api/v1/settings/secrets')
    const data = await res.json()
    secrets.value = data.secrets
    secretsEnvFile.value = data.env_file
    secretEdits.value = {}
    secretVisible.value = {}
  } catch (e) {
    toast.error('Could not load secrets.', e instanceof Error ? e.message : String(e))
  } finally {
    loadingSecrets.value = false
  }
}

async function saveSecrets() {
  savingSecrets.value = true
  secretsSaved.value = false
  try {
    const updates: Record<string, string> = {}
    for (const [key, val] of Object.entries(secretEdits.value)) {
      if (val.trim()) updates[key] = val.trim()
    }
    if (Object.keys(updates).length === 0) {
      toast.info('No changes to save.')
      return
    }
    const res = await fetch('/api/v1/settings/secrets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    })
    if (!res.ok) throw new Error((await res.json()).detail)
    secretsSaved.value = true
    toast.success('Secrets saved. Restart the service to apply.')
    await loadSecrets()
  } catch (e) {
    toast.error('Could not save secrets.', e instanceof Error ? e.message : String(e))
  } finally {
    savingSecrets.value = false
  }
}


async function loadCloudLLM() {
  loadingCloud.value = true
  try {
    const res = await fetch('/api/v1/settings/cloud-llm')
    cloudLLM.value = await res.json()
    cloudMonthlyLimit.value = cloudLLM.value.monthly_limit_usd
  } catch {
    toast.error('Could not load cloud LLM settings.')
  } finally {
    loadingCloud.value = false
  }
}

async function saveCloudLimit() {
  try {
    await fetch('/api/v1/settings/cloud-llm', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ monthly_limit_usd: cloudMonthlyLimit.value }),
    })
    if (cloudLLM.value) cloudLLM.value.monthly_limit_usd = cloudMonthlyLimit.value
    toast.success('Monthly limit updated.')
  } catch {
    toast.error('Could not save limit.')
  }
}

async function loadGhosts() {
  loadingGhosts.value = true
  try {
    const r = await fetch('/api/v1/health/ghost-resources')
    ghosts.value = await r.json()
  } catch {
    toast.error('Could not load ghost resource data.')
  } finally {
    loadingGhosts.value = false
  }
}

async function ghostAction(resourceType: string, name: string, action: string) {
  try {
    const r = await fetch('/api/v1/health/ghost-resources/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resource_type: resourceType, name, action }),
    })
    const data = await r.json()
    if (r.ok) {
      toast.success(data.message)
      await loadGhosts()  // Refresh
    } else {
      toast.error('Action failed.', data.detail || '')
    }
  } catch (e) {
    toast.error('Action failed.', String(e))
  }
}

async function loadSafety() {
  try {
    const res = await fetch('/api/v1/settings/ai-safety')
    const data = await res.json()
    safetyLevels.value = data.levels
  } catch {
    toast.error('Could not load AI safety settings.')
  }
}

async function setSafetyLevel(actionType: string, level: string) {
  try {
    await fetch('/api/v1/settings/ai-safety', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_type: actionType, level }),
    })
    if (safetyLevels.value?.[actionType]) {
      safetyLevels.value[actionType].level = level
    }
    if (level === 'act') {
      toast.warn(`Auto-act enabled for "${actionType.replace(/_/g, ' ')}" — AI will execute this automatically.`)
    }
  } catch {
    toast.error('Could not update safety level.')
  }
}

async function loadPreApproval() {
  try {
    const res = await fetch('/api/v1/settings/preapproval')
    preApproval.value = await res.json()
  } catch {
    toast.error('Could not load pre-approval policy.')
  }
}

async function setTierDefault(tier: number, preApproved: boolean) {
  try {
    const res = await fetch('/api/v1/settings/preapproval/tier', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tier, pre_approved: preApproved }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      toast.error(err.detail || 'Could not update tier policy.')
      return
    }
    preApproval.value = await res.json()
    if (preApproved) {
      toast.warn(`Tier T${tier} pre-approved — the agent may act on these without asking.`)
    }
  } catch {
    toast.error('Could not update tier policy.')
  }
}

async function loadTraefik() {
  try {
    const res = await fetch('/api/v1/settings/traefik')
    traefikSettings.value = await res.json()
  } catch { toast.error('Could not load Traefik settings.') }
}

async function saveTraefik() {
  savingTraefik.value = true
  try {
    const res = await fetch('/api/v1/settings/traefik', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(traefikSettings.value),
    })
    const data = await res.json()
    if (data.ok) toast.success(data.message)
    else toast.error('Save failed.', data.detail)
  } catch { toast.error('Could not save Traefik settings.') }
  finally { savingTraefik.value = false }
}

async function savePuidOverrides() {
  savingPuid.value = true
  try {
    const overrides: Record<string, string> = {}
    for (const line of puidOverrides.value.split('\n')) {
      const [key, val] = line.trim().split('=')
      if (key && val) overrides[key.trim()] = val.trim()
    }
    await fetch('/api/v1/settings/secrets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { PUID_OVERRIDES: JSON.stringify(overrides) } }),
    })
    toast.success('PUID/PGID overrides saved.')
  } catch { toast.error('Could not save overrides.') }
  finally { savingPuid.value = false }
}

async function saveDockerSocket() {
  savingSocket.value = true
  try {
    await fetch('/api/v1/settings/secrets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { DOCKER_SOCKET: dockerSocket.value } }),
    })
    toast.success('Docker socket path saved. Restart the service to apply.')
  } catch { toast.error('Could not save socket path.') }
  finally { savingSocket.value = false }
}


async function doRemoveAll() {
  removingAll.value = true
  removeCount.value = 0
  removeTotal.value = 0
  showRemoveAllConfirm.value = false
  try {
    const installed = (await apps.list()).filter(a => a.tier !== 0)
    if (!installed.length) {
      toast.info('No installed apps to remove.')
      return
    }
    removeTotal.value = installed.length
    const results = await Promise.allSettled(
      installed.map(async (app) => {
        try {
          await apps.remove(app.key, false)
        } finally {
          removeCount.value++
        }
        return app
      })
    )
    let fulfilled = 0
    for (let i = 0; i < results.length; i++) {
      const r = results[i]
      if (r.status === 'fulfilled') {
        fulfilled++
      } else {
        const errorMsg = r.reason instanceof Error ? r.reason.message : String(r.reason)
        toast.error(`Failed to remove ${installed[i].key}`, errorMsg)
      }
    }
    const total = installed.length
    const summary = `Removed ${fulfilled} / ${total} apps.`
    if (fulfilled === 0 && total > 0) {
      toast.error(summary)
    } else {
      toast.success(summary)
    }
  } finally {
    removingAll.value = false
    removeCount.value = 0
    removeTotal.value = 0
  }
}

async function doReset() {
  showResetConfirm.value = false
  try {
    await platformApi.reset()
    // Refresh store BEFORE navigating so SetupView sees isReady=false immediately
    const { usePlatformStore } = await import('../stores/platform')
    await usePlatformStore().fetchStatus()
    toast.info('Platform reset.')
    router.push('/setup')
  } catch (e) {
    toast.error('Could not reset platform.', e instanceof Error ? e.message : String(e))
  }
}

async function load() {
  loading.value = true
  try {
    const data = await settings.get() as any
    form.value = {
      health_check_interval_secs: data.health_check_interval_secs ?? 30,
      ntfy_topic: data.ntfy_topic ?? 'slop',
      ntfy_url: data.ntfy_url ?? 'http://ntfy:80',
      ntfy_enabled: data.ntfy_enabled ?? true,
      llm_enabled: data.llm_enabled ?? true,
      llm_backend: data.llm_backend ?? 'ollama',
      llm_ollama_url: data.llm_ollama_url ?? 'http://ollama:11434',
      llm_llamacpp_url: data.llm_llamacpp_url ?? 'http://localhost:8081',
      llm_model: data.llm_model ?? 'phi4-mini',
      cf_auto_register_hostnames: data.cf_auto_register_hostnames ?? false,
      disk_warn_percent: data.disk_warn_percent ?? 80,
      disk_error_percent: data.disk_error_percent ?? 90,
    }
    schedulerRunning.value = data.scheduler_running ?? false
    lastCycle.value = data.health_last_cycle_summary ?? null
    lastCycleAt.value = data.health_last_cycle_at ? Number(data.health_last_cycle_at) : null
  } catch { /* intentional: health settings load failure is non-fatal */ }
  finally { loading.value = false }
}

async function save() {
  saving.value = true; saveSuccess.value = false; saveError.value = null
  try {
    await settings.update(form.value as any)
    saveSuccess.value = true
    toast.success('Settings saved.')
    setTimeout(() => { saveSuccess.value = false }, 2000)
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    saveError.value = msg
    toast.error('Could not save settings.', msg)
  } finally { saving.value = false }
}

async function saveHFToken() {
  if (!hfTokenInput.value) return
  savingHFToken.value = true
  try {
    const r = await fetch('/api/v1/settings/secrets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { HF_TOKEN: hfTokenInput.value } }),
    })
    if (r.ok) {
      hfTokenSaved.value = true
      toast.success('HuggingFace token saved.')
      setTimeout(() => { hfTokenSaved.value = false }, 3000)
    } else {
      toast.error('Could not save token.')
    }
  } catch (e) {
    toast.error('Save failed.', String(e))
  } finally {
    savingHFToken.value = false
  }
}


function clearCustomApp() {
  customYamlInput.value = ''
  customLintResult.value = null
  customMissingVars.value = []
  customVarValues.value = {}
}

function lintCustomYaml() {
  if (_customLintTimer) clearTimeout(_customLintTimer)
  if (!customYamlInput.value.trim()) {
    customLintResult.value = null
    customMissingVars.value = []
    customVarValues.value = {}
    return
  }
  _customLintTimer = setTimeout(async () => {
    try {
      const res = await fetch('/api/v1/apps/lint-compose', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml: customYamlInput.value }),
      })
      const result = await res.json()
      customLintResult.value = result
      // Populate missing-vars state from scanner result
      const newMissing: string[] = result.missing_vars || []
      customMissingVars.value = newMissing
      // Preserve any values the user already filled in, initialise new ones to ''
      const prev = customVarValues.value
      const next: Record<string, string> = {}
      for (const v of newMissing) {
        next[v] = prev[v] ?? ''
      }
      customVarValues.value = next
    } catch (e) {
      customLintResult.value = { valid: false, errors: [String(e)], warnings: [] }
      customMissingVars.value = []
      customVarValues.value = {}
    }
  }, 400)
}

async function installCustomYaml() {
  if (!customLintResult.value?.valid) return
  installingCustom.value = true
  try {
    const preview = customLintResult.value.manifest_preview
    // Step 1: register manifest in community catalog
    const regRes = await fetch('/api/v1/apps/install-custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest: preview, compose_yaml: customYamlInput.value }),
    })
    if (!regRes.ok) {
      const err = await regRes.json()
      toast.error('Registration failed.', err.detail ?? String(err))
      return
    }
    const regData = await regRes.json()
    const appKey = regData.key as string

    // Step 2: start the actual install, passing user-supplied var values
    const extraEnv = Object.fromEntries(
      Object.entries(customVarValues.value).filter(([, v]) => v.trim() !== '')
    )
    const instRes = await fetch(`/api/v1/apps/${appKey}/install`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ extra_env: Object.keys(extraEnv).length > 0 ? extraEnv : null }),
    })
    if (instRes.ok) {
      toast.success(`${preview?.display_name ?? appKey} install started — check Dashboard for progress.`)
      customYamlInput.value = ''
      customLintResult.value = null
      customMissingVars.value = []
      customVarValues.value = {}
    } else {
      const err = await instRes.json()
      toast.error('Install failed.', err.detail ?? String(err))
    }
  } catch (e) {
    toast.error('Install failed.', String(e))
  } finally {
    installingCustom.value = false
  }
}

async function installCustomGithub() {
  if (!customGithubUrl.value) return
  installingCustom.value = true
  customGithubResult.value = null
  githubRegisteredKey.value = null
  githubMissingVars.value = []
  githubVarValues.value = {}
  try {
    const res = await fetch('/api/v1/apps/install-from-github', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_url: customGithubUrl.value }),
    })
    const data = await res.json()
    customGithubResult.value = data
    if (data.ok) {
      toast.success(`Manifest fetched: ${data.key}`)
      githubRegisteredKey.value = data.key as string
      const newMissing: string[] = data.missing_vars ?? []
      githubMissingVars.value = newMissing
      githubVarValues.value = Object.fromEntries(newMissing.map((v: string) => [v, '']))
    } else {
      toast.error('Fetch failed.', data.detail ?? '')
    }
  } catch (e) {
    customGithubResult.value = { ok: false, message: String(e) }
  } finally {
    installingCustom.value = false
  }
}

async function installGithubApp() {
  if (!githubRegisteredKey.value) return
  installingCustom.value = true
  try {
    const extraEnv = Object.fromEntries(
      Object.entries(githubVarValues.value).filter(([, v]) => (v as string).trim() !== '')
    )
    const instRes = await fetch(`/api/v1/apps/${githubRegisteredKey.value}/install`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ extra_env: Object.keys(extraEnv).length > 0 ? extraEnv : null }),
    })
    if (instRes.ok) {
      toast.success(`${githubRegisteredKey.value} install started — check Dashboard for progress.`)
      clearGithubApp()
    } else {
      const err = await instRes.json()
      toast.error('Install failed.', err.detail ?? String(err))
    }
  } catch (e) {
    toast.error('Install failed.', String(e))
  } finally {
    installingCustom.value = false
  }
}

function clearGithubApp() {
  customGithubUrl.value = ''
  customGithubResult.value = null
  githubRegisteredKey.value = null
  githubMissingVars.value = []
  githubVarValues.value = {}
}


async function loadProfile() {
  loadingProfile.value = true
  try { profile.value = await settings.system() as any } catch { /* intentional: profile load failure is non-fatal */ } finally { loadingProfile.value = false }
}

// ── Quick Stacks management ───────────────────────────────────────────────
const stacksList = ref<any[]>([])
const stackEditId = ref<string | null>(null)
const stackEditForm = ref<{label: string; app_keys: string; ram_note: string}>({ label: '', app_keys: '', ram_note: '' })
const stackAddMode = ref(false)
const stackAddForm = ref<{label: string; app_keys: string; ram_note: string}>({ label: '', app_keys: '', ram_note: '' })
const stackSaving = ref(false)

async function loadStacks() {
  try {
    const r = await fetch('/api/v1/platform/stacks')
    const d = await r.json()
    stacksList.value = d.stacks || []
  } catch { /* intentional: stacks load failure is non-fatal */ }
}

function editStack(stack: any) {
  stackEditId.value = stack.id
  stackEditForm.value = {
    label: stack.label,
    app_keys: (stack.app_keys || []).join(', '),
    ram_note: stack.ram_note || '',
  }
}

async function saveStack(stackId: string) {
  stackSaving.value = true
  try {
    const keys = stackEditForm.value.app_keys.split(',').map((k: string) => k.trim()).filter(Boolean)
    const r = await fetch(`/api/v1/platform/stacks/${stackId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: stackEditForm.value.label, app_keys: keys, ram_note: stackEditForm.value.ram_note }),
    })
    if (r.ok) {
      toast.success('Stack updated.')
      stackEditId.value = null
      await loadStacks()
    } else {
      const err = await r.json()
      toast.error('Update failed.', err.detail ?? String(err))
    }
  } catch (e) { toast.error('Update failed.', String(e)) }
  finally { stackSaving.value = false }
}

async function deleteStack(stackId: string) {
  try {
    const r = await fetch(`/api/v1/platform/stacks/${stackId}`, { method: 'DELETE' })
    if (r.ok) {
      const d = await r.json()
      toast.success(d.action === 'hidden' ? 'Stack hidden from wizard.' : 'Stack deleted.')
      await loadStacks()
    }
  } catch (e) { toast.error('Delete failed.', String(e)) }
}

async function restoreStack(stackId: string) {
  try {
    const r = await fetch(`/api/v1/platform/stacks/${stackId}/restore`, { method: 'POST' })
    if (r.ok) {
      toast.success('Stack restored to default.')
      await loadStacks()
    }
  } catch (e) { toast.error('Restore failed.', String(e)) }
}

async function addStack() {
  stackSaving.value = true
  try {
    const keys = stackAddForm.value.app_keys.split(',').map((k: string) => k.trim()).filter(Boolean)
    const r = await fetch('/api/v1/platform/stacks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: stackAddForm.value.label, app_keys: keys, ram_note: stackAddForm.value.ram_note }),
    })
    if (r.ok) {
      toast.success('Custom stack created.')
      stackAddMode.value = false
      stackAddForm.value = { label: '', app_keys: '', ram_note: '' }
      await loadStacks()
    } else {
      const err = await r.json()
      toast.error('Create failed.', err.detail ?? String(err))
    }
  } catch (e) { toast.error('Create failed.', String(e)) }
  finally { stackSaving.value = false }
}

onMounted(async () => { await load(); await loadStacks() })
</script>
