<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="flex items-baseline gap-3 mb-2">
      <h1 class="page-title">
        LLM Models
      </h1>
      <p class="text-sm text-slate-400">
        AI health agent — diagnose app failures
      </p>
    </div>

    <!-- Agent status card — compact header, expand on click -->
    <div class="card mb-2">
      <div class="card-body !py-2.5">
        <div class="flex items-center gap-3">
          <!-- Status dot -->
          <div
            class="w-7 h-7 rounded-lg flex items-center justify-center text-base shrink-0"
            :class="{
              'bg-green-100': ping?.reachable && ping?.model_loaded,
              'bg-amber-100': ping?.reachable && !ping?.model_loaded,
              'bg-red-100': ping && !ping.reachable,
              'bg-slate-100': !ping,
            }"
          >
            🤖
          </div>

          <div class="flex-1 min-w-0">
            <!-- Title row -->
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-semibold text-slate-900 text-sm">Health Agent</span>
              <!-- Live status from ping -->
              <template v-if="ping">
                <span
                  v-if="ping.reachable && ping.model_loaded"
                  class="badge badge-green text-xs"
                >ready</span>
                <span
                  v-else-if="ping.reachable && !ping.model_loaded"
                  class="badge badge-yellow text-xs"
                >no model</span>
                <span
                  v-else
                  class="badge badge-red text-xs"
                >offline</span>
              </template>

              <div class="flex items-center gap-2 ml-auto">
                <button
                  :disabled="pinging"
                  class="text-xs text-slate-400 hover:text-slate-600"
                  @click="doPing"
                >
                  {{ pinging ? '…' : '↻' }}
                </button>
                <button
                  class="text-xs text-slate-400 hover:text-slate-600"
                  @click="agentExpanded = !agentExpanded"
                >
                  {{ agentExpanded ? '▲' : '▼' }}
                </button>
              </div>
            </div>
          </div>
          <!-- Expanded detail -->
          <div
            v-if="agentExpanded"
            class="mt-2 pt-2 border-t border-slate-100"
          >
            <p
              v-if="!ping"
              class="text-sm text-slate-400 italic"
            >
              Checking Ollama…
            </p>

            <!-- State: all good — expand to show all active models -->
            <template v-else-if="ping.reachable && ping.model_loaded">
              <p class="text-sm text-green-700 mb-1">
                Connected to <code class="font-mono text-xs bg-green-50 px-1 rounded">{{ ping.ollama_url }}</code>
              </p>
              <div class="flex flex-wrap gap-1.5">
                <span
                  v-for="m in ping.loaded_models"
                  :key="m"
                  class="text-xs font-mono px-2 py-0.5 rounded-full bg-green-50 border border-green-200 text-green-700"
                >
                  {{ m }}
                </span>
              </div>
            </template>

            <!-- State: Ollama up but model missing -->
            <template v-else-if="ping.reachable && !ping.model_loaded">
              <p class="text-sm text-amber-700 font-medium">
                Ollama is running but the model isn't loaded.
              </p>
              <div class="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 space-y-1.5">
                <p class="text-xs text-amber-800">
                  {{ ping.error }}
                </p>
                <div class="flex items-center gap-2">
                  <span class="text-xs text-slate-500 shrink-0">Run:</span>
                  <code class="text-xs font-mono bg-white border border-amber-200 px-2 py-0.5 rounded select-all flex-1">{{ ping.fix }}</code>
                </div>
                <div
                  v-if="ggufImportInstructions"
                  class="pt-1 border-t border-amber-100"
                >
                  <p class="text-xs text-amber-700 font-medium">
                    Or import your downloaded GGUF directly:
                  </p>
                  <code class="block text-xs font-mono bg-white border border-amber-200 px-2 py-1.5 rounded select-all leading-relaxed whitespace-pre-wrap mt-1">{{ ggufImportInstructions }}</code>
                </div>
                <p
                  v-if="ping.loaded_models?.length"
                  class="text-xs text-slate-400"
                >
                  Currently loaded: {{ ping.loaded_models.join(', ') }}
                </p>
              </div>
            </template>

            <!-- Cloud LLM configured notice (shown alongside offline state) -->
            <div
              v-if="cloudLLMConfigured"
              class="mt-2 rounded-lg border border-green-200 bg-green-50 px-3 py-2"
            >
              <p class="text-xs text-green-700">
                ✓ AI health agent is active via <strong class="capitalize">{{ cloudLLMProvider }}</strong> (cloud).
                Ollama is optional — the agent works without it.
              </p>
            </div>
            <!-- Ollama not installed — show specific fix -->
            <div
              v-else-if="agentStatus?.last_error_type === 'dns' || agentStatus?.last_error_type === 'connection'"
              class="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2"
            >
              <p class="text-xs text-amber-800 font-medium mb-1">
                Ollama is not installed
              </p>
              <p class="text-xs text-amber-700">
                {{ agentStatus?.description }}
              </p>
              <div class="mt-2 flex gap-2">
                <RouterLink
                  to="/catalog"
                  class="text-xs text-sky-600 font-medium hover:underline"
                >
                  Go to Catalog →
                </RouterLink>
              </div>
            </div>
            <!-- Auth failure -->
            <div
              v-else-if="agentStatus?.last_error_type === 'auth'"
              class="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2"
            >
              <p class="text-xs text-red-800 font-medium mb-1">
                API key issue
              </p>
              <p class="text-xs text-red-700">
                {{ agentStatus?.description }}
              </p>
              <RouterLink
                to="/settings?tab=ai"
                class="text-xs text-sky-600 font-medium hover:underline"
              >
                Update in Settings → AI →
              </RouterLink>
            </div>

            <!-- State: Ollama unreachable -->
            <template v-else>
              <p class="text-sm text-red-700 font-medium">
                {{ ping.error_type === 'connection' ? 'Ollama is not running.' :
                  ping.error_type === 'timeout' ? 'Ollama timed out.' :
                  'Cannot reach Ollama.' }}
              </p>
              <div class="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 space-y-2">
                <!-- URL editor -->
                <div class="flex items-center gap-2">
                  <code class="text-xs font-mono text-red-600 shrink-0">URL:</code>
                  <input
                    v-model="ollamaUrlInput"
                    type="text"
                    class="input !py-0.5 text-xs font-mono flex-1"
                    placeholder="http://localhost:11434"
                  >
                  <button
                    :disabled="savingUrl"
                    class="btn-secondary btn-sm text-xs shrink-0"
                    @click="saveOllamaUrl"
                  >
                    {{ savingUrl ? '…' : 'Save' }}
                  </button>
                </div>
                <p class="text-xs text-red-600">
                  {{ ping.error }}
                </p>
                <template v-if="ping.fix">
                  <p class="text-xs text-slate-500 font-semibold">
                    Run on your server:
                  </p>
                  <code class="block text-xs font-mono bg-white border border-red-200 px-2 py-1.5 rounded select-all leading-relaxed whitespace-pre-wrap">{{ ping.fix }}</code>
                </template>
                <!-- GGUF import hint if local GGUF models are downloaded -->
                <div
                  v-if="ggufImportInstructions"
                  class="pt-1 border-t border-red-100"
                >
                  <p class="text-xs text-slate-500 font-semibold">
                    Or import your downloaded model into Ollama:
                  </p>
                  <code class="block text-xs font-mono bg-white border border-red-200 px-2 py-1.5 rounded select-all leading-relaxed whitespace-pre-wrap mt-1">{{ ggufImportInstructions }}</code>
                </div>
              </div>
            </template>

            <!-- Agent cycle stats -->
            <div
              v-if="agentStatus && agentStatus.status !== 'unknown'"
              class="text-xs text-slate-400 mt-1.5"
            >
              Last run: {{ agentStatus.consecutive_failures === 0 ? 'ok' : agentStatus.consecutive_failures + ' failure(s)' }}
              <span v-if="agentStatus.consecutive_slow"> · {{ agentStatus.consecutive_slow }} slow</span>
            </div>
          </div><!-- /expanded -->
        </div>

        <!-- Action bar always visible -->
        <div class="flex gap-2 mt-2 pt-2 border-t border-slate-100">
          <button
            :disabled="runningAgent"
            class="btn-primary btn-sm text-xs"
            @click="runAgent"
          >
            {{ runningAgent ? 'Running…' : '▶ Run agent' }}
          </button>
          <button
            :disabled="evaluating"
            class="btn-secondary btn-sm text-xs"
            @click="evaluateAgent"
          >
            {{ evaluating ? 'Evaluating…' : 'Evaluate hardware' }}
          </button>
        </div>
      </div>

      <!-- Evaluate steps — shown during evaluation -->
      <div
        v-if="evalSteps.length"
        class="mt-2 pt-2 border-t border-slate-100 space-y-1"
      >
        <template
          v-for="step in evalSteps"
          :key="step.label"
        >
          <!-- Model compatibility table — special render -->
          <div
            v-if="step.label === 'Model compatibility'"
            class="mt-2"
          >
            <div class="text-xs font-medium text-slate-500 mb-1.5">
              Model compatibility
            </div>
            <div class="grid grid-cols-3 gap-1.5">
              <div
                v-for="row in parseModelCompat(step.detail)"
                :key="row.name"
                :class="['rounded-lg border px-2 py-1.5 text-xs',
                         row.status === 'ok' ? 'border-green-200 bg-green-50' :
                         row.status === 'warn' ? 'border-amber-200 bg-amber-50' : 'border-slate-200 bg-slate-50']"
              >
                <div class="font-medium text-slate-800 truncate">
                  {{ row.name }}
                </div>
                <div class="flex items-center justify-between mt-0.5">
                  <span class="text-slate-500">{{ row.size_gb }}GB</span>
                  <span :class="row.status === 'ok' ? 'text-green-600' : row.status === 'warn' ? 'text-amber-600' : 'text-slate-400'">
                    {{ row.mode }}
                  </span>
                </div>
              </div>
            </div>
          </div>
          <!-- Regular step -->
          <div
            v-else
            class="flex items-center gap-2 text-xs"
          >
            <span
              :class="{
                'text-green-500': step.status === 'ok',
                'text-amber-500': step.status === 'warn',
                'text-red-500': step.status === 'error',
                'text-slate-400': step.status === 'info',
              }"
            >{{ step.status === 'ok' ? '✓' : step.status === 'warn' ? '⚠' : step.status === 'error' ? '✗' : '·' }}</span>
            <span class="font-medium text-slate-600 w-16 shrink-0">{{ step.label }}</span>
            <span class="text-slate-500">{{ step.detail }}</span>
          </div>
        </template>
        <div
          v-if="evalSummary"
          class="mt-1.5 pt-1.5 border-t border-slate-100 text-xs font-medium"
          :class="evalVerdict === 'runs_well' ? 'text-green-700' : evalVerdict === 'runs_slowly' ? 'text-amber-600' : 'text-red-600'"
        >
          {{ evalSummary }}
        </div>
      </div>
    </div>

    <!-- 401: token needed — link to Settings > AI -->
    <div
      v-if="needsHFToken"
      class="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 flex items-center gap-2"
    >
      <span class="text-amber-500 shrink-0 text-sm">🔑</span>
      <span class="text-xs text-amber-800 flex-1">
        This model requires a HuggingFace token —
        <RouterLink
          to="/settings"
          class="underline font-medium"
        >add it in Settings → AI</RouterLink>.
      </span>
      <button
        class="text-amber-400 hover:text-amber-600 shrink-0"
        @click="needsHFToken = false; downloadError = null"
      >
        ✕
      </button>
    </div>

    <!-- Download error (non-401) -->
    <div
      v-else-if="downloadError"
      class="mb-4 rounded-xl border border-red-200 bg-red-50 p-3"
    >
      <div class="flex items-center justify-between">
        <p class="text-sm text-red-800">
          {{ downloadError }}
        </p>
        <button
          class="text-xs text-red-400 hover:text-red-600 ml-3 shrink-0"
          @click="downloadError = null"
        >
          ✕
        </button>
      </div>
    </div>

    <!-- Installed models -->
    <div class="flex items-center justify-between mb-2">
      <h2 class="font-semibold text-slate-900">
        Installed Models
      </h2>
      <button
        class="text-xs text-sky-500 hover:text-sky-600"
        @click="refreshModels"
      >
        Refresh
      </button>
    </div>


    <!-- Installed models skeleton while loading -->
    <div
      v-if="!modelsLoaded"
      class="grid grid-cols-2 gap-2 mb-3"
    >
      <div
        v-for="n in 2"
        :key="n"
        class="card card-body !py-2.5 !px-3 animate-pulse"
      >
        <div class="flex items-center gap-2">
          <div class="w-5 h-5 bg-slate-100 rounded shrink-0" />
          <div class="flex-1 space-y-1.5">
            <div class="h-2.5 bg-slate-100 rounded w-4/5" />
            <div class="h-2 bg-slate-100 rounded w-1/2" />
          </div>
        </div>
      </div>
    </div>
    <div
      v-else-if="installedModels.length === 0"
      class="card card-body text-center py-6 text-slate-400 text-sm mb-6"
    >
      No models found. Download one below, or run <code class="font-mono bg-slate-100 px-1 rounded">ollama pull [model-name]</code> on your server.
    </div>
    <div
      v-else
      class="grid grid-cols-2 gap-2 mb-3"
    >
      <div
        v-for="m in installedModels"
        :key="m.filename"
        class="card card-body !py-2.5 !px-3"
      >
        <div class="flex items-center gap-2">
          <span class="text-sm shrink-0">{{ m.valid ? '✅' : '❌' }}</span>
          <div class="flex-1 min-w-0">
            <div class="font-mono text-xs text-slate-900 truncate font-medium">
              {{ m.filename }}
            </div>
            <div class="text-xs text-slate-400 flex items-center gap-1.5 mt-0.5">
              <span>{{ m.size_mb.toFixed(0) }}MB</span>
              <span
                v-if="m.path?.startsWith('ollama://')"
                class="px-1 py-0 rounded bg-sky-100 text-sky-600 text-xs"
              >Ollama</span>
              <span
                v-else
                class="px-1 py-0 rounded bg-slate-100 text-slate-500 text-xs"
              >local</span>
              <span
                v-if="!m.valid"
                class="text-red-500"
              >✗</span>
            </div>
          </div>
          <div class="flex gap-1 shrink-0">
            <button
              :class="['btn-sm text-xs', isModelEnabled(m.filename) ? 'btn-primary' : 'btn-secondary']"
              :title="isModelEnabled(m.filename) ? 'Active in router' : 'Enable for routing'"
              @click="enableModel(m)"
            >
              {{ isModelEnabled(m.filename) ? 'Active' : 'Enable' }}
            </button>
            <button
              class="btn-secondary btn-sm text-xs text-red-500"
              @click="removeModel(m.filename)"
            >
              Remove
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Add custom model -->
    <div class="card mb-3">
      <div
        class="card-header cursor-pointer flex items-center justify-between"
        @click="showAddCustom = !showAddCustom"
      >
        <h2 class="font-semibold text-slate-900">
          Add Custom Model
        </h2>
        <span class="text-slate-400 text-sm">{{ showAddCustom ? '▲' : '▼' }}</span>
      </div>
      <div
        v-if="showAddCustom"
        class="card-body space-y-4"
      >
        <!-- Tabs -->
        <div class="flex gap-2 border-b border-slate-100 pb-3">
          <button
            v-for="tab in ['URL', 'HuggingFace Search', 'Local file']"
            :key="tab"
            :class="['text-sm px-3 py-1 rounded-full transition-colors', addTab === tab ? 'bg-slate-900 text-white' : 'text-slate-500 hover:text-slate-700']"
            @click="addTab = tab"
          >
            {{ tab }}
          </button>
        </div>

        <!-- URL tab -->
        <div
          v-if="addTab === 'URL'"
          class="space-y-3"
        >
          <div>
            <label class="label">Download URL</label>
            <input
              v-model="customUrl"
              type="text"
              class="input"
              placeholder="https://huggingface.co/[user]/[repo]/resolve/main/model.gguf"
            >
            <p class="text-xs text-slate-400 mt-1">
              Direct link to a .gguf file from HuggingFace or any host
            </p>
          </div>
          <!-- Preflight result -->
          <div
            v-if="preflight"
            :class="['rounded-lg p-3 text-sm', preflight.ok ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']"
          >
            <span v-if="preflight.ok">
              ✓ {{ preflight.filename }} · {{ preflight.size_mb?.toFixed(0) }} MB · Ready to download
            </span>
            <span v-else>{{ preflight.error }}</span>
          </div>
          <div class="flex gap-2">
            <button
              :disabled="!customUrl || preflighting"
              class="btn-secondary btn-sm"
              @click="runPreflight"
            >
              {{ preflighting ? '…' : 'Validate URL' }}
            </button>
            <button
              :disabled="!preflight?.ok || !!downloadingModel"
              class="btn-primary btn-sm"
              @click="startCustomDownload"
            >
              Download
            </button>
          </div>
        </div>

        <!-- HuggingFace Search tab -->
        <div
          v-if="addTab === 'HuggingFace Search'"
          class="space-y-3"
        >
          <div class="flex gap-2">
            <input
              v-model="hfQuery"
              type="text"
              class="input flex-1"
              placeholder="phi-4 mini, llama, mistral…"
              @keyup.enter="searchHF"
            >
            <button
              :disabled="searching"
              class="btn-secondary btn-sm"
              @click="searchHF"
            >
              {{ searching ? '…' : 'Search' }}
            </button>
          </div>
          <div
            v-if="hfResults.length"
            class="space-y-2"
          >
            <div
              v-for="r in hfResults"
              :key="r.id"
              class="flex items-center gap-3 px-3 py-2 rounded-lg border border-slate-100 hover:border-slate-200"
            >
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium text-slate-900 truncate">
                  {{ r.id }}
                </div>
                <div class="text-xs text-slate-400">
                  {{ r.downloads?.toLocaleString() }} downloads
                </div>
              </div>
              <a
                :href="`https://huggingface.co/${r.id}`"
                target="_blank"
                rel="noopener"
                class="text-xs text-sky-500 hover:text-sky-600 shrink-0"
              >View ↗</a>
            </div>
          </div>
        </div>

        <!-- Local file tab -->
        <div
          v-if="addTab === 'Local file'"
          class="space-y-3"
        >
          <div>
            <label class="label">File path on server</label>
            <input
              v-model="localPath"
              type="text"
              class="input font-mono"
              placeholder="/mnt/media/models/my-model.gguf"
            >
            <p class="text-xs text-slate-400 mt-1">
              Absolute path to an existing .gguf file on the server
            </p>
          </div>
          <button
            :disabled="!localPath || validatingLocal"
            class="btn-secondary btn-sm"
            @click="validateLocal"
          >
            {{ validatingLocal ? '…' : 'Validate & Register' }}
          </button>
          <div
            v-if="localValidation"
            :class="['text-sm rounded-lg p-3', localValidation.valid ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']"
          >
            {{ localValidation.valid ? '✓ Valid GGUF — registered.' : localValidation.error }}
          </div>
        </div>
      </div>
    </div>




    <!-- Inference provider -->
    <div class="card mb-3">
      <div class="card-body !py-2.5">
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm font-semibold text-slate-900">Inference provider</span>
          <button
            :disabled="savingUrl"
            class="btn-primary btn-sm text-xs"
            @click="saveOllamaUrl"
          >
            {{ savingUrl ? 'Saving…' : 'Save' }}
          </button>
        </div>

        <!-- Provider tabs: local / cloud groups -->
        <div class="space-y-1 mb-2">
          <div
            v-for="grp in ['local','cloud']"
            :key="grp"
          >
            <div class="text-xs text-slate-400 uppercase tracking-wider mb-0.5">
              {{ grp }}
            </div>
            <div class="flex gap-1 flex-wrap">
              <button
                v-for="p in PROVIDERS.filter(x=>x.group===grp)"
                :key="p.id"
                :class="['btn-sm text-xs', inferenceProvider === p.id ? 'btn-primary' : 'btn-secondary']"
                @click="setProvider(p.id)"
              >
                {{ p.label }}
              </button>
            </div>
          </div>
        </div>

        <!-- Free tier badge for cloud providers -->
        <div
          v-if="isCloudProvider && inferenceProvider !== 'openrouter'"
          class="flex items-center gap-2 mb-1"
        >
          <span class="badge badge-green text-xs">Free tier available</span>
          <span class="text-xs text-slate-400">No credit card required</span>
        </div>
        <!-- Config fields -->
        <div class="space-y-2">
          <!-- URL (all except OpenRouter) -->
          <div
            v-if="inferenceProvider !== 'openrouter'"
            class="flex items-center gap-2"
          >
            <label class="text-xs text-slate-500 w-10 shrink-0">URL</label>
            <input
              v-model="ollamaUrlInput"
              type="text"
              class="input text-xs font-mono flex-1"
              :placeholder="providerUrl(inferenceProvider)"
            >
          </div>
          <!-- API key (OpenRouter required, others optional) -->
          <div
            v-if="showApiKey"
            class="flex items-center gap-2"
          >
            <label class="text-xs text-slate-500 w-10 shrink-0">
              {{ inferenceProvider === 'openrouter' ? 'Key' : 'Token' }}
            </label>
            <input
              v-model="providerApiKey"
              type="password"
              class="input text-xs flex-1"
              :placeholder="inferenceProvider === 'openrouter' ? 'sk-or-…' : 'optional'"
            >
          </div>
          <!-- Model override -->
          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 w-10 shrink-0">Model</label>
            <input
              v-model="providerModel"
              type="text"
              class="input text-xs font-mono flex-1"
              :placeholder="inferenceProvider === 'openrouter'
                ? 'meta-llama/llama-3.3-70b-instruct:free'
                : inferenceProvider === 'ollama' ? '[model-name]' : 'auto-detect'"
            >
          </div>
        </div>

        <!-- Setup hint -->
        <p class="text-xs text-slate-400 mt-2">
          {{ PROVIDERS.find(p=>p.id===inferenceProvider)?.hint }}
        </p>
      </div>
    </div>


    <!-- Model Registry — active models + task routing -->
    <!-- Registry skeleton while loading -->
    <div
      v-if="!registryLoaded"
      class="mb-6"
    >
      <div class="flex items-center justify-between mb-2">
        <div class="h-4 bg-slate-100 rounded w-28 animate-pulse" />
      </div>
      <div class="grid grid-cols-2 gap-2 mb-4">
        <div
          v-for="n in 4"
          :key="n"
          class="card card-body !py-2.5 !px-3 animate-pulse"
        >
          <div class="h-3 bg-slate-100 rounded w-3/4 mb-1.5" />
          <div class="h-2.5 bg-slate-100 rounded w-1/2" />
        </div>
      </div>
    </div>
    <div
      v-else-if="registry.models.length"
      class="mb-6"
    >
      <div class="flex items-center justify-between mb-3">
        <div>
          <h2 class="font-semibold text-slate-900">
            Model Routing
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">
            Enable models and see which tasks they handle
          </p>
        </div>
        <span class="text-xs text-slate-400">{{ registry.enabled_count }} active</span>
      </div>

      <!-- Per-model cards: compact 2-col -->
      <div class="grid grid-cols-2 gap-2 mb-4">
        <div
          v-for="m in registry.models"
          :key="m.filename"
          :class="['card card-body !py-2.5 !px-3 transition-all', m.enabled ? 'border-l-2 border-l-sky-400' : '']"
        >
          <div class="flex items-center gap-2">
            <button
              :class="['w-8 h-4 rounded-full transition-colors shrink-0 relative',
                       m.enabled ? 'bg-sky-500' : 'bg-slate-200']"
              @click="toggleModel(m)"
            >
              <span
                :class="['absolute top-0.5 w-3 h-3 bg-white rounded-full shadow transition-transform',
                         m.enabled ? 'translate-x-4' : 'translate-x-0.5']"
              />
            </button>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-1.5">
                <span class="font-mono text-xs text-slate-900 truncate">{{ m.filename }}</span>
                <span
                  v-if="m.enabled"
                  class="badge badge-green text-xs shrink-0"
                >active</span>
              </div>
              <div class="flex flex-wrap gap-1 mt-1">
                <span
                  v-for="cap in m.capabilities"
                  :key="cap"
                  :class="['text-xs px-1 py-0 rounded font-medium', capColor(cap)]"
                >
                  {{ cap }}
                </span>
              </div>
            </div>
            <div class="flex items-center gap-1 shrink-0">
              <select
                :value="m.priority"
                class="input !py-0 !px-1 text-xs w-10"
                title="Priority (1=highest)"
                @change="updatePriority(m, +($event.target as HTMLSelectElement).value)"
              >
                <option
                  v-for="n in 9"
                  :key="n"
                  :value="n"
                >
                  {{ n }}
                </option>
              </select>
              <button
                class="text-xs text-slate-400 hover:text-slate-600 px-1"
                @click="expandedModel = expandedModel === m.filename ? null : m.filename"
              >
                ▾
              </button>
            </div>
          </div>
          <!-- Score bars — expanded -->
          <div
            v-if="expandedModel === m.filename"
            class="mt-2 pt-2 border-t border-slate-100 grid grid-cols-2 gap-x-3 gap-y-1"
          >
            <div
              v-for="(score, task) in m.task_scores"
              :key="task"
              class="flex items-center gap-1.5"
            >
              <div class="flex-1 h-1 bg-slate-100 rounded-full overflow-hidden">
                <div
                  class="h-full bg-sky-400 rounded-full"
                  :style="`width:${Math.round(score*100)}%`"
                />
              </div>
              <span class="text-xs text-slate-400 w-16 truncate">{{ task }} {{ Math.round(score*100) }}%</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Routing table -->
      <div class="card">
        <div class="card-header">
          <span class="font-semibold text-sm">Task Routing</span>
          <span class="text-xs text-slate-400 ml-2">— which model handles each task type</span>
        </div>
        <div class="divide-y divide-slate-50">
          <div
            v-for="row in registry.routing_table"
            :key="row.task_type"
            class="flex items-center gap-3 px-4 py-2.5"
          >
            <span :class="['text-xs font-mono px-2 py-0.5 rounded w-28 text-center', capColor(row.task_type)]">
              {{ row.task_type }}
            </span>
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              class="w-3 h-3 text-slate-300 shrink-0"
            >
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
            <span
              v-if="row.model"
              class="text-sm text-slate-700 font-medium flex-1"
            >
              {{ row.display_name }}
            </span>
            <span
              v-else
              class="text-sm text-slate-400 italic flex-1"
            >no model active</span>
            <span
              v-if="row.model"
              class="text-xs text-slate-400"
            >
              {{ Math.round(row.score * 100) }}% match
            </span>
          </div>
        </div>
      </div>

      <!-- Routing history -->
      <div class="card mt-4">
        <div class="card-header flex items-center justify-between">
          <span class="font-semibold text-sm">Routing History</span>
          <button
            class="text-xs text-slate-400 hover:text-slate-600"
            @click="loadHistory"
          >
            ↻ Refresh
          </button>
        </div>
        <div
          v-if="!routingLog.length"
          class="card-body text-xs text-slate-400 text-center py-4"
        >
          No routing history yet — run the agent to see which model handles each task.
        </div>
        <div
          v-else
          class="divide-y divide-slate-50"
        >
          <div
            v-for="row in routingLog"
            :key="row.id"
            class="flex items-center gap-3 px-4 py-2 text-xs"
          >
            <!-- Time -->
            <span class="text-slate-400 shrink-0 w-14 tabular-nums">
              {{ formatAge(row.ts) }}
            </span>
            <!-- App -->
            <span class="text-slate-600 font-medium w-28 truncate shrink-0">{{ row.app_key }}</span>
            <!-- Task type -->
            <span :class="['px-1.5 py-0.5 rounded font-medium shrink-0', capColor(row.task_type)]">
              {{ row.task_type }}
            </span>
            <!-- Arrow -->
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              class="w-3 h-3 text-slate-300 shrink-0"
            ><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            <!-- Model -->
            <span class="font-mono text-slate-700 truncate flex-1">{{ row.model }}</span>
            <!-- Result -->
            <span
              v-if="row.success"
              class="text-green-600 shrink-0"
            >✓</span>
            <span
              v-else
              :class="['shrink-0', row.error_type === 'connection' ? 'text-red-500' : 'text-amber-500']"
              :title="row.summary || row.error_type"
            >✗</span>
            <!-- Duration -->
            <span
              v-if="row.duration_ms"
              class="text-slate-400 shrink-0 w-14 text-right tabular-nums"
            >
              {{ row.duration_ms > 1000 ? (row.duration_ms/1000).toFixed(1)+'s' : row.duration_ms+'ms' }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- Recommended models -->
    <h2 class="font-semibold text-slate-900 mb-2">
      Recommended Models
    </h2>
    <div class="grid grid-cols-2 gap-2">
      <div
        v-for="m in recommendedModels"
        :key="m.name"
        class="card card-body !py-2.5 !px-3"
      >
        <div class="flex items-center gap-2">
          <!-- Icon -->
          <div class="w-6 h-6 rounded bg-slate-100 flex items-center justify-center shrink-0 overflow-hidden">
            <img
              :src="modelIconUrl(m.name)"
              class="w-5 h-5 object-contain"
              @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t) t.style.display='none' }"
            >
          </div>
          <!-- Name + description -->
          <div class="flex-1 min-w-0">
            <div class="font-medium text-xs text-slate-900 truncate">
              {{ m.name }}
            </div>
            <div class="text-xs text-slate-400 truncate">
              {{ m.notes }}
            </div>
          </div>
          <!-- Right: size + capability + download -->
          <div class="flex flex-col items-end gap-1 shrink-0">
            <div class="flex items-center gap-1">
              <span class="text-xs text-slate-400">{{ m.size_gb.toFixed(1) }}GB</span>
              <span :class="['text-xs px-1 py-0 rounded font-medium', capColor(m.recommended_for)]">
                {{ m.recommended_for }}
              </span>
            </div>
            <button
              :disabled="!!downloadingModel"
              class="btn-primary btn-sm text-xs"
              @click="downloadModel(m)"
            >
              {{ downloadingModel === m.name ? '…' : '↓' }}
            </button>
          </div>
        </div>
      </div>
    </div><!-- /recommended models -->

    <!-- Download progress — slim bar below recommended models -->
    <div
      v-if="downloadingModel"
      class="mt-3 px-1"
    >
      <div class="flex items-center gap-3 mb-1">
        <span class="text-xs text-slate-500 truncate flex-1">↓ {{ downloadingModel }}</span>
        <span class="text-xs text-slate-400 tabular-nums shrink-0">{{ downloadPercent }}% · {{ downloadedMB }} / {{ totalMB || '?' }} MB</span>
      </div>
      <div class="h-1 bg-slate-100 rounded-full overflow-hidden">
        <div
          class="h-full bg-sky-500 transition-all duration-300 rounded-full"
          :style="`width: ${downloadPercent}%`"
        />
      </div>
    </div>

    <div
      v-if="downloadSuccess"
      class="mt-2 flex items-center gap-2 px-1 text-xs text-green-700"
    >
      <span>✓</span><span>{{ downloadSuccess }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useToast } from '@/composables/useToast'
import { models, health, settings } from '../api/client'
import type { GGUFModel, RecommendedModel } from '../api/client'

const installedModels = ref<GGUFModel[]>([])
const modelsLoaded = ref(false)
const registryLoaded = ref(false)
const recommendedModels = ref<RecommendedModel[]>([])
const agentStatus = ref<{ status: string; description: string; last_error: string; last_error_type: string; ollama_url: string; model_tried: string; consecutive_failures: number; consecutive_slow: number; last_success_at: number; configured_provider: string } | null>(null)

// Evaluate state
const evaluating = ref(false)
const evalSteps = ref<{ label: string; status: string; detail: string }[]>([])
const evalSummary = ref('')
const evalVerdict = ref('')

// Download state
const downloadingModel = ref<string | null>(null)
let _activeEs: EventSource | null = null  // tracked for cleanup on unmount
const downloadPercent = ref(0)
const downloadedMB = ref(0)
const totalMB = ref(0)
const downloadError = ref<string | null>(null)
const pendingDownload = ref<any>(null)
const selectedModelFile = ref<string>('')
const registry = ref<{models: any[], routing_table: any[], enabled_count: number}>({
  models: [], routing_table: [], enabled_count: 0
})
const expandedModel = ref<string | null>(null)
const routingLog = ref<any[]>([])
const runningAgent = ref(false)
const ping = ref<any>(null)
const cloudLLMConfigured = ref(false)
const cloudLLMProvider = ref('')
const pinging = ref(false)
const agentExpanded = ref(false)
const ollamaUrlInput = ref('http://localhost:11434')
const savingUrl = ref(false)
const inferenceProvider = ref('ollama')
const providerApiKey = ref('')
const providerModel = ref('')

const PROVIDERS = [
  // Local
  { id: 'ollama',      label: 'Ollama',       group: 'local',  hint: 'Best local option. Install: curl -fsSL https://ollama.com/install.sh | sh' },
  { id: 'llamacpp',   label: 'llama-server',  group: 'local',  hint: 'Bare llama.cpp server. Fastest CPU inference. Run: llama-server -m model.gguf --port 8081' },
  { id: 'shimmy',     label: 'Shimmy',        group: 'local',  hint: '5MB Rust binary, no dependencies. ./shimmy serve (auto GPU, port 11435)' },
  { id: 'localai',    label: 'LocalAI',       group: 'local',  hint: '36+ backends (vLLM, Whisper, images). Install via Catalog → AI → LocalAI' },
  // Cloud — free tiers
  { id: 'groq',       label: 'Groq',          group: 'cloud',  hint: 'Fastest cloud inference (300–1000 TPS). Free: 30 RPM, 14.4K req/day. Sign up at console.groq.com' },
  { id: 'cerebras',   label: 'Cerebras',      group: 'cloud',  hint: 'Fastest cloud inference (3000 TPS on WSE). Free: 30 RPM, 1M tokens/day. Sign up at cloud.cerebras.ai' },
  { id: 'nim',        label: 'NVIDIA NIM',    group: 'cloud',  hint: '100+ models on DGX Cloud. Free tier at build.nvidia.com (nvapi- key)' },
  { id: 'gai',        label: 'Google AI',     group: 'cloud',  hint: 'Free Gemini 2.5 Pro via AI Studio. aistudio.google.com → API key' },
  { id: 'openrouter', label: 'OpenRouter',    group: 'cloud',  hint: '290+ models, one API key. Free models: DeepSeek R1, Llama 3.3 70B' },
]

const showApiKey = computed(() =>
  ['openrouter','groq','cerebras','nim','gai'].includes(inferenceProvider.value) ||
  ['shimmy','localai','llamacpp'].includes(inferenceProvider.value)
)

const isCloudProvider = computed(() =>
  ['groq','cerebras','nim','gai','openrouter'].includes(inferenceProvider.value)
)

function providerUrl(p: string): string {
  const defaults: Record<string, string> = {
    ollama:     'http://localhost:11434',
    llamacpp:   'http://localhost:8081',
    shimmy:     'http://localhost:11435',
    localai:    'http://localhost:8080',
    groq:       'https://api.groq.com/openai/v1',
    cerebras:   'https://api.cerebras.ai/v1',
    nim:        'https://integrate.api.nvidia.com/v1',
    gai:        'https://generativelanguage.googleapis.com/v1beta/openai/',
    openrouter: 'https://openrouter.ai/api/v1',
  }
  return defaults[p] ?? 'http://localhost:11434'
}

const toast = useToast()

const ggufImportInstructions = computed(() => {
  // Only local GGUF files have a filesystem path — skip Ollama-managed models
  // (those have path: "ollama://..." and can't be imported via Modelfile)
  const localModels = installedModels.value.filter(m => !m.path.startsWith('ollama://'))
  if (!localModels.length) return ''
  const model = localModels[0]
  const safeName = model.filename.replace('.gguf', '').replace(/[^a-z0-9_-]/gi, '-').toLowerCase()
  // model.path is the full server-side filesystem path returned by the backend
  // (/api/v1/models/gguf) — e.g. /var/lib/slop/models/model.gguf
  const modelPath = model.path
  return `# Create a Modelfile\necho "FROM ${modelPath}" > /tmp/Modelfile\n# Import into Ollama (creates model named '${safeName}')\nollama create ${safeName} -f /tmp/Modelfile\n# Verify it loaded\nollama list`
})

async function setProvider(p: string) {
  inferenceProvider.value = p
  ollamaUrlInput.value = providerUrl(p)
  await saveOllamaUrl()
}

async function saveOllamaUrl() {
  savingUrl.value = true
  try {
    const params = new URLSearchParams()
    params.set('provider',   inferenceProvider.value)
    params.set('ollama_url', ollamaUrlInput.value || providerUrl(inferenceProvider.value))
    if (providerApiKey.value) params.set('api_key', providerApiKey.value)
    if (providerModel.value)  params.set('model',   providerModel.value)
    await health.setAgentConfig(params.toString())
    toast.success('Provider config saved.')
    await doPing()
    // Check if cloud LLM is configured (agent may work via cloud even if Ollama offline)
    try {
      const d = await settings.get()
      const cfg = JSON.parse((d.llm_agent_config as string) || '{}')
      if (cfg.provider && cfg.provider !== 'ollama' && cfg.provider !== 'none' && cfg.api_key) {
        cloudLLMConfigured.value = true
        cloudLLMProvider.value = cfg.provider
      }
    } catch { /* intentional: non-fatal */ }
  } catch (e) {
    toast.error('Save failed.', String(e))
  } finally {
    savingUrl.value = false
  }
}


function parseModelCompat(detail: string): any[] {
  try { return JSON.parse(detail.replace(/'/g, '"')) } catch { return [] }
}

function capColor(cap: string): string {
  const map: Record<string, string> = {
    reasoning: 'bg-violet-100 text-violet-700',
    json: 'bg-sky-100 text-sky-700',
    code: 'bg-emerald-100 text-emerald-700',
    fast: 'bg-amber-100 text-amber-700',
    classification: 'bg-orange-100 text-orange-700',
    general: 'bg-slate-100 text-slate-600',
  }
  return map[cap] ?? 'bg-slate-100 text-slate-500'
}

async function loadHistory() {
  try {
    routingLog.value = await models.routingLog(40)
  } catch { /* intentional: non-fatal */ }
}

function formatAge(ts: number): string {
  const diff = Math.floor(Date.now() / 1000) - ts
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`
  return `${Math.floor(diff/86400)}d ago`
}

async function loadRegistry() {
  try {
    registry.value = await models.registry()
  } catch { /* intentional: non-fatal */ }
  registryLoaded.value = true
}

function isModelEnabled(filename: string): boolean {
  return registry.value.models.some((m: any) => m.filename === filename && m.enabled)
}

async function enableModel(m: any) {
  // Find in registry and toggle; if not in registry yet, enable it
  const reg = registry.value.models.find((r: any) => r.filename === m.filename)
  if (reg) {
    await toggleModel(reg)
  } else {
    // Auto-register + enable
    try {
      await models.updateRegistryModel(m.filename, { enabled: true })
      await loadRegistry()
      toast.success(`${m.filename} enabled for routing.`)
    } catch (e) {
      toast.error('Could not enable model.', String(e))
    }
  }
}

async function toggleModel(m: any) {
  m.enabled = !m.enabled
  try {
    const d = await models.updateRegistryModel(m.filename, { enabled: m.enabled })
    if (d.routing_table) registry.value.routing_table = d.routing_table
    registry.value.enabled_count = registry.value.models.filter((x: any) => x.enabled).length
    toast.success(m.enabled ? `${m.display_name} enabled` : `${m.display_name} disabled`)
  } catch (e) {
    m.enabled = !m.enabled  // revert on error
    toast.error('Could not update model.', String(e))
  }
}

async function updatePriority(m: any, priority: number) {
  const prevPriority = m.priority
  m.priority = priority
  try {
    const d = await models.updateRegistryModel(m.filename, { priority })
    if (d.routing_table) registry.value.routing_table = d.routing_table
    // Re-sort models by priority
    registry.value.models.sort((a: any, b: any) => a.priority - b.priority)
  } catch (e) {
    m.priority = prevPriority // roll back the optimistic update on error
    toast.error('Could not update priority.', String(e))
  }
}

const _iconUrlCache = new Map<string, string>()
function modelIconUrl(name: string): string {
  if (_iconUrlCache.has(name)) return _iconUrlCache.get(name)!
  const n = (name || '').toLowerCase()
  if (n.includes('llama')) return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/ollama.png'
  if (n.includes('phi')) return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/microsoft.png'
  if (n.includes('qwen')) return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/qwen.png'
  if (n.includes('mistral') || n.includes('mixtral')) return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/mistral.png'
  if (n.includes('gemma')) return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/google.png'
  return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/ollama.png'
}
const downloadSuccess = ref<string | null>(null)
const needsHFToken = ref(false)
const hfTokenInput = ref('')
const hfTokenSaved = ref(false)

// Add custom model
const showAddCustom = ref(false)
const addTab = ref('URL')
const customUrl = ref('')
const preflight = ref<any>(null)
const preflighting = ref(false)
const hfQuery = ref('')
const hfResults = ref<any[]>([])
const searching = ref(false)
const localPath = ref('')
const localValidation = ref<any>(null)
const validatingLocal = ref(false)

async function refreshModels() {
  const list = await models.list().catch(() => [])
  installedModels.value = list
}

async function removeModel(filename: string) {
  try {
    await models.remove(filename)
  } catch (e) {
    toast.error('Could not delete model.', String(e))
  }
  await refreshModels()
}

async function evaluateAgent() {
  evaluating.value = true
  evalSteps.value = []
  evalSummary.value = ''
  evalVerdict.value = ''

  try {
    // Get current model size from installed models
    const sel = selectedModelFile.value
        ? installedModels.value.find(m => m.filename === selectedModelFile.value)
        : installedModels.value[0]
    const modelSizeGb = sel ? sel.size_mb / 1024 : 4.0

    const data = await models.evaluateHardware(Number(modelSizeGb.toFixed(1)))
    evalSteps.value = data.steps ?? []
    evalSummary.value = data.summary ?? ''
    evalVerdict.value = data.verdict ?? ''

    // Also run the agent eval
    await models.evaluate()
    agentStatus.value = await health.llmAgent()
  } catch (e) {
    evalSteps.value = [{ label: 'Error', status: 'error', detail: String(e) }]
  } finally {
    evaluating.value = false
  }
}
async function doPing() {
  pinging.value = true
  try {
    ping.value = await health.llmPing()
    if (ping.value?.ollama_url) ollamaUrlInput.value = ping.value.ollama_url
  } catch { ping.value = null }
  pinging.value = false
}

async function runAgent() {
  runningAgent.value = true
  try {
    const d = await health.runCycle()
    agentStatus.value = await health.llmAgent()
    await doPing()
  // Check if cloud LLM is configured (agent may work via cloud even if Ollama offline)
  try {
    const d = await settings.get()
    const cfg = JSON.parse((d.llm_agent_config as string) || '{}')
    if (cfg.provider && cfg.provider !== 'ollama' && cfg.provider !== 'none' && cfg.api_key) {
      cloudLLMConfigured.value = true
      cloudLLMProvider.value = cfg.provider
    }
  } catch { /* intentional: non-fatal */ }
    await loadHistory()
    const provider = agentStatus.value?.configured_provider || 'ollama'
    const llmInfo = agentStatus.value?.status === 'active'
      ? ` · ${provider}: ${agentStatus.value.model_tried || 'active'}`
      : agentStatus.value?.status === 'offline'
      ? ` · ${provider} offline — ${agentStatus.value.description || agentStatus.value.last_error || 'check Settings → AI'}`
      : agentStatus.value?.status === 'unknown'
      ? ` · AI agent not yet active`
      : ''
    if (d.apps_checked === 0) {
      toast.warn(`No running apps found. Install apps from Catalog first.`)
    } else {
      toast.success(`Agent run complete — ${d.apps_checked ?? 0} apps checked${llmInfo}.`)
    }
  } catch (e) {
    toast.error('Agent run failed.', String(e))
  } finally {
    runningAgent.value = false
  }
}

async function runPreflight() {
  preflighting.value = true
  preflight.value = null
  try {
    preflight.value = await models.ggufPreflight(customUrl.value)
  } catch (e) {
    preflight.value = { ok: false, error: String(e) }
  } finally {
    preflighting.value = false
  }
}

async function startCustomDownload() {
  if (!preflight.value?.ok) return
  downloadError.value = null
  downloadSuccess.value = null
  downloadingModel.value = preflight.value.filename
  downloadPercent.value = 0
  downloadedMB.value = 0
  totalMB.value = preflight.value.size_mb ?? 0

  if (_activeEs) { _activeEs.close(); _activeEs = null }
  const es = models.downloadSSE(customUrl.value, preflight.value.filename)
  _activeEs = es
  es.addEventListener('progress', (e: MessageEvent) => {
    try {
      const d = JSON.parse(e.data)
      downloadPercent.value = d.percent ?? 0
      downloadedMB.value = d.mb_downloaded ?? 0
      totalMB.value = d.total_mb ?? totalMB.value
    } catch { /* intentional: non-fatal */ }
  })
  es.addEventListener('complete', async () => {
    es.close(); _activeEs = null
    downloadingModel.value = null
    downloadSuccess.value = `${preflight.value.filename} downloaded.`
    await refreshModels()
  })
  es.addEventListener('error', (e: MessageEvent) => {
    es.close(); _activeEs = null
    downloadingModel.value = null
    try {
      const d = JSON.parse((e as any).data ?? '{}')
      downloadError.value = d.error ?? 'Download failed.'
      needsHFToken.value = d.error?.includes('401') || d.error?.includes('Unauthorized')
    } catch {
      downloadError.value = 'Download failed.'
    }
  })
}

async function searchHF() {
  searching.value = true
  hfResults.value = []
  try {
    hfResults.value = await models.hfSearch(hfQuery.value)
  } catch { /* intentional: non-fatal */ }
  finally { searching.value = false }
}

async function validateLocal() {
  validatingLocal.value = true
  localValidation.value = null
  try {
    localValidation.value = await models.validate(localPath.value)
    if (localValidation.value?.valid) await refreshModels()
  } catch (e) {
    localValidation.value = { valid: false, error: String(e) }
  } finally {
    validatingLocal.value = false
  }
}


function downloadModel(m: RecommendedModel) {
  if (downloadingModel.value) return
  // Scroll to top before starting download to prevent page jump
  window.scrollTo({ top: 0, behavior: 'smooth' })
  downloadingModel.value = m.name
  downloadPercent.value = 0
  downloadedMB.value = 0
  totalMB.value = 0
  downloadError.value = null
  downloadSuccess.value = null
  needsHFToken.value = false

  if (_activeEs) { _activeEs.close(); _activeEs = null }
  const es = models.downloadSSE(m.hf_url)
  _activeEs = es
  es.addEventListener('progress', (e: MessageEvent) => {
    try {
      const d = JSON.parse(e.data)
      downloadPercent.value = d.percent ?? 0
      downloadedMB.value = d.mb_downloaded ?? 0
      totalMB.value = d.total_mb ?? 0
    } catch { /* intentional: non-fatal */ }
  })
  es.addEventListener('complete', async () => {
    es.close(); _activeEs = null
    downloadingModel.value = null
    downloadSuccess.value = `${m.name} downloaded and validated.`
    await refreshModels()
    await loadRegistry()
  await loadHistory()
  await doPing()
  // Check if cloud LLM is configured (agent may work via cloud even if Ollama offline)
  try {
    const d = await settings.get()
    const cfg = JSON.parse((d.llm_agent_config as string) || '{}')
    if (cfg.provider && cfg.provider !== 'ollama' && cfg.provider !== 'none' && cfg.api_key) {
      cloudLLMConfigured.value = true
      cloudLLMProvider.value = cfg.provider
    }
  } catch { /* intentional: non-fatal */ }
  })
  es.addEventListener('error', (e: MessageEvent) => {
    es.close(); _activeEs = null
    downloadingModel.value = null
    try {
      const d = JSON.parse((e as any).data ?? '{}')
      downloadError.value = d.error ?? 'Download failed.'
      needsHFToken.value = d.error?.includes('401') || d.error?.includes('Unauthorized')
      if (needsHFToken.value) pendingDownload.value = m
    } catch {
      downloadError.value = 'Download failed — check network and try again.'
    }
  })
  es.onerror = () => {
    if (downloadingModel.value === m.name) {
      es.close(); _activeEs = null
      downloadingModel.value = null
      if (!downloadSuccess.value && !downloadError.value) {
        downloadError.value = 'Connection lost. The file may have downloaded — refresh to check.'
      }
    }
  }
}

onUnmounted(() => {
  if (_activeEs) { _activeEs.close(); _activeEs = null }
})

onMounted(async () => {
  const [inst, rec, status, secrets] = await Promise.allSettled([
    models.list(), models.recommended(), health.llmAgent(),
    settings.secrets()
  ])
  await loadRegistry()
  await loadHistory()
  await doPing()
  // Check if cloud LLM is configured (agent may work via cloud even if Ollama offline)
  try {
    const d = await settings.get()
    const cfg = JSON.parse((d.llm_agent_config as string) || '{}')
    if (cfg.provider && cfg.provider !== 'ollama' && cfg.provider !== 'none' && cfg.api_key) {
      cloudLLMConfigured.value = true
      cloudLLMProvider.value = cfg.provider
    }
  } catch { /* intentional: non-fatal */ }
  if (inst.status === 'fulfilled') installedModels.value = inst.value
  modelsLoaded.value = true
  if (rec.status === 'fulfilled') recommendedModels.value = rec.value
  if (status.status === 'fulfilled') agentStatus.value = status.value
  // Load current provider config
  try {
    const cfg = await health.agentConfig()
    inferenceProvider.value = cfg.provider || 'ollama'
    ollamaUrlInput.value    = cfg.ollama_url || providerUrl(cfg.provider || 'ollama')
    if (cfg.api_key) providerApiKey.value = cfg.api_key
    if (cfg.model)   providerModel.value  = cfg.model
  } catch { /* intentional: non-fatal */ }
  if (secrets.status === 'fulfilled') {
    const tokenInfo = secrets.value?.secrets?.HF_TOKEN
    if (tokenInfo?.is_set && tokenInfo.value) {
      hfTokenInput.value = tokenInfo.value
      hfTokenSaved.value = true
    }
  }
})
</script>
