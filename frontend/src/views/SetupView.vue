<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="mb-6">
      <h1 class="page-title">
        Platform Setup
      </h1>
      <p class="page-subtitle">
        Configure your platform before installing apps.
      </p>
      <div
        v-if="hasDraft"
        class="mt-2 flex items-center gap-2 text-xs text-amber-600 bg-amber-50 border border-amber-100 rounded px-3 py-1.5"
      >
        <span>📋 Progress restored from your last session.</span>
        <button
          class="underline hover:no-underline font-medium shrink-0"
          @click="clearDraftAndReset"
        >
          Start fresh
        </button>
      </div>
    </div>

    <!-- Already configured -->
    <div
      v-if="platformStore.isReady && !forceSetup"
      class="card card-body text-center py-10"
    >
      <div class="text-3xl mb-3">
        ✅
      </div>
      <h2 class="font-semibold text-slate-900">
        Platform is configured
      </h2>
      <p class="text-sm text-slate-500 mt-1">
        Domain: <strong>{{ platformStore.domain }}</strong>
      </p>
      <div class="flex gap-3 justify-center mt-4">
        <RouterLink
          to="/catalog"
          class="btn-primary"
        >
          Install apps →
        </RouterLink>
        <RouterLink
          to="/settings?tab=platform"
          class="btn-secondary"
        >
          ← Settings
        </RouterLink>
        <button
          class="btn-secondary text-red-600"
          @click="showReset = true"
        >
          Reset platform
        </button>
      </div>
    </div>

    <template v-else>
      <!-- Stage progress bar -->
      <div
        ref="stageNavRef"
        class="flex items-center gap-1.5 mb-6 overflow-x-auto pb-1 scroll-smooth"
      >
        <template
          v-for="(stage, i) in STAGES"
          :key="stage.id"
        >
          <button
            :ref="el => { if (currentStage === i) activeStageBtn = el as HTMLElement }"
            :disabled="i > maxReachedStage"
            :class="['flex items-center gap-1.5 shrink-0 px-2 py-1 rounded-lg transition-all text-xs',
                     currentStage === i ? 'bg-sky-100 text-sky-700 font-semibold' :
                     i < currentStage ? 'text-green-600 hover:bg-green-50' :
                     i <= maxReachedStage ? 'text-slate-500 hover:bg-slate-50' :
                     'text-slate-300 cursor-default']"
            @click="goToStage(i)"
          >
            <span
              :class="['w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold',
                       i < currentStage ? 'bg-green-500 text-white' :
                       currentStage === i ? 'bg-sky-500 text-white' :
                       'bg-slate-100 text-slate-400']"
            >
              {{ i < currentStage ? '✓' : i + 1 }}
            </span>
            <span class="whitespace-nowrap hidden sm:inline">{{ stage.label }}</span>
          </button>
          <div
            v-if="i < STAGES.length - 1"
            class="h-px w-3 bg-slate-200 shrink-0"
          />
        </template>
      </div>

      <!-- Stage content -->
      <div class="card mb-4">
        <div class="card-header flex items-center justify-between">
          <div>
            <div class="font-semibold text-sm">
              {{ STAGES[currentStage].label }}
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              {{ STAGES[currentStage].description }}
            </div>
          </div>
          <span class="text-xs text-slate-400">{{ currentStage + 1 }} / {{ STAGES.length }}</span>
        </div>
        <div class="card-body space-y-4" style="min-height: 340px">
          <!-- Stage 0: Prerequisites -->
          <template v-if="currentStage === 0">
            <div class="space-y-3">
              <div class="rounded-lg bg-slate-50 border border-slate-200 p-4">
                <!-- Loading skeleton — shown while fetch is in flight -->
                <div
                  v-if="prereqRunning"
                  class="space-y-3"
                >
                  <div class="flex items-center gap-2 text-xs text-slate-500">
                    <span class="w-3.5 h-3.5 border-2 border-sky-400 border-t-transparent rounded-full animate-spin shrink-0" />
                    Checking your system…
                  </div>
                  <div
                    v-for="n in 6"
                    :key="n"
                    class="flex items-center gap-3 py-0.5"
                  >
                    <div class="w-4 h-3 bg-slate-200 rounded animate-pulse shrink-0" />
                    <div class="flex-1 flex gap-3">
                      <div
                        :style="{width: (40 + n * 8) + 'px'}"
                        class="h-3 bg-slate-200 rounded animate-pulse"
                      />
                      <div
                        :style="{width: (30 + n * 5) + 'px'}"
                        class="h-3 bg-slate-100 rounded animate-pulse"
                      />
                    </div>
                  </div>
                </div>

                <!-- Results -->
                <div
                  v-else-if="prereqChecks.length"
                  class="space-y-1.5"
                >
                  <div
                    v-for="check in prereqChecks"
                    :key="check.key"
                    class="flex items-start gap-2 text-sm py-0.5"
                  >
                    <span
                      :class="['shrink-0 mt-0.5 font-bold text-xs w-4 text-center',
                               check.status === 'ok' ? 'text-green-500' :
                               check.status === 'error' ? 'text-red-500' :
                               check.status === 'warning' ? 'text-amber-500' : 'text-slate-400']"
                    >
                      {{ check.status === 'ok' ? '✓' : check.status === 'error' ? '✗' : check.status === 'warning' ? '!' : '○' }}
                    </span>
                    <div class="flex-1 min-w-0">
                      <div class="flex items-baseline gap-2 flex-wrap">
                        <span
                          :class="['font-medium text-xs',
                                   check.status === 'error' ? 'text-red-700' :
                                   check.status === 'warning' ? 'text-amber-700' : 'text-slate-700']"
                        >
                          {{ check.label }}
                        </span>
                        <span class="text-slate-400 text-xs font-mono">{{ check.value }}</span>
                      </div>
                      <div
                        v-if="check.detail"
                        class="text-xs text-slate-400 mt-0.5"
                      >
                        {{ check.detail }}
                      </div>
                    </div>
                  </div>
                </div>

                <!-- Empty state — before first run (shouldn't be visible long) -->
                <div
                  v-else
                  class="flex items-center gap-2 text-xs text-slate-400 py-1"
                >
                  <span class="text-slate-300">○</span>
                  Click below to check your system
                </div>
              </div>

              <p
                v-if="prereqError"
                class="text-sm text-red-600 bg-red-50 rounded-lg p-3"
              >
                {{ prereqError }}
              </p>

              <button
                :disabled="prereqRunning"
                class="btn-primary btn-sm"
                @click="runPrereqChecks"
              >
                {{ prereqRunning ? 'Checking…' : prereqChecks.length ? 'Re-check' : 'Run checks' }}
              </button>
            </div>
          </template>

          <!-- Stage 1: Core Config -->
          <template v-if="currentStage === 1">
            <div>
              <label class="label">Base domain <span class="text-red-400">*</span></label>
              <input
                v-model="form.domain"
                type="text"
                placeholder="example.com"
                class="input"
                autocomplete="off"
              >
              <p class="text-xs text-slate-400 mt-1">
                Apps will be accessible at app.example.com
              </p>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="label">Config root <span class="text-red-400">*</span></label>
                <input
                  v-model="form.config_root"
                  type="text"
                  class="input"
                >
              </div>
              <div>
                <label class="label">Media root</label>
                <input
                  v-model="form.media_root"
                  type="text"
                  class="input"
                >
              </div>
            </div>
            <div class="grid grid-cols-3 gap-3">
              <div>
                <label class="label">PUID
                  <span
                    v-if="systemProfile?.puid_username"
                    class="text-slate-400 font-normal ml-1 text-xs"
                  >({{ systemProfile.puid_username }})</span>
                </label>
                <input
                  v-model.number="form.puid"
                  type="number"
                  class="input"
                >
              </div>
              <div>
                <label class="label">PGID</label>
                <input
                  v-model.number="form.pgid"
                  type="number"
                  class="input"
                >
              </div>
              <div class="tz-wrapper">
                <label class="label">Timezone</label>
                <p class="text-xs text-slate-400 mt-1 mb-1">
                  {{ form.timezone && form.timezone !== 'Etc/UTC' ? 'Detected: ' + form.timezone : 'Browser-detected; type to change' }}
                </p>
                <input
                  v-model="timezoneFilter"
                  type="search"
                  class="timezone-search"
                  autocomplete="off"
                  placeholder="Type to search timezones (e.g. Los_Angeles)"
                  @focus="(e: any) => { e.target.select(); timezoneFilter = '' }"
                  @input="timezoneFilter = ($event.target as HTMLInputElement).value"
                >
                <div v-if="timezoneFilter.length >= 2" class="timezone-dropdown">
                  <div
                    v-for="tz in filteredTimezones"
                    :key="tz"
                    class="timezone-option"
                    @mousedown.prevent="selectTimezone(tz)"
                  >
                    {{ tz }}
                  </div>
                  <div v-if="filteredTimezones.length === 0" class="timezone-option text-slate-400">
                    No matches
                  </div>
                </div>
                <input v-model="form.timezone" type="hidden">
              </div>
            </div>
            <div>
              <label class="label">ACME email</label>
              <input
                v-model="form.acme_email"
                type="email"
                class="input"
                :placeholder="form.domain ? 'admin@' + form.domain : 'admin@yourdomain.com'"
              >
            </div>
          </template>

          <!-- Stage 2: TLS / DNS -->
          <template v-if="currentStage === 2">
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="label">Certificate authority</label>
                <select
                  v-model="form.cert_resolver"
                  class="input"
                >
                  <option value="letsencrypt">
                    Let's Encrypt (free, 90-day)
                  </option>
                  <option value="zerossl">
                    ZeroSSL (free, 90-day, separate limits)
                  </option>
                  <option value="buypass">
                    Buypass (free, 180-day)
                  </option>
                  <option value="staging">
                    Staging — testing only
                  </option>
                </select>
              </div>
              <div>
                <label class="label">DNS provider</label>
                <select
                  v-model="form.dns_provider"
                  class="input"
                >
                  <option
                    value=""
                    disabled
                  >
                    — Select DNS provider —
                  </option>
                  <option
                    v-for="(meta, key) in effectiveDnsProviders"
                    :key="key"
                    :value="key"
                  >
                    {{ meta.label }}
                  </option>
                </select>
                <p
                  v-if="effectiveDnsProviders[form.dns_provider]"
                  class="text-xs text-slate-400 mt-1"
                >
                  Requires: <span class="font-mono">{{ effectiveDnsProviders[form.dns_provider].vars.join(', ') }}</span>
                  — <a
                    :href="effectiveDnsProviders[form.dns_provider].link"
                    target="_blank"
                    rel="noopener"
                    class="underline"
                  >get credentials ↗</a>
                </p>
              </div>
            </div>
            <div
              v-if="form.cert_resolver === 'zerossl'"
              class="rounded-xl border border-amber-200 bg-amber-50 p-4 space-y-3"
            >
              <p class="text-sm text-amber-800">
                ⚠ ZeroSSL requires EAB credentials from
                <a
                  href="https://app.zerossl.com/developer"
                  target="_blank"
                  rel="noopener"
                  class="underline"
                >app.zerossl.com/developer</a>
              </p>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="label text-xs">EAB Key ID</label>
                  <input
                    v-model="form.eab_kid"
                    type="text"
                    class="input font-mono text-xs"
                    placeholder="kid_xxx…"
                  >
                </div>
                <div>
                  <label class="label text-xs">EAB HMAC Key</label>
                  <input
                    v-model="form.eab_hmac"
                    type="password"
                    class="input font-mono text-xs"
                  >
                </div>
              </div>
            </div>
          </template>

          <!-- Stage 3: Infrastructure -->
          <template v-if="currentStage === 3">
            <p class="text-xs text-slate-500">
              Select infrastructure apps to deploy automatically during setup. These are installed alongside Traefik. You can change them later via the Infrastructure page.
            </p>
            <div class="space-y-2">
              <div
                v-for="slot in INFRA_SLOTS"
                :key="slot.slot"
                :class="['rounded-lg p-3 border',
                         slot.core ? 'border-sky-200 bg-sky-50/50' : 'border-slate-200']"
              >
                <div class="mb-2">
                  <div class="flex items-center gap-2">
                    <div class="text-sm font-medium text-slate-700">
                      {{ slot.label }}
                    </div>
                    <span
                      v-if="slot.core"
                      class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-sky-100 text-sky-700"
                    >Core</span>
                  </div>
                  <div class="text-xs text-slate-400">
                    {{ slot.description }}
                  </div>
                </div>
                <!-- Multi-select (tunnel) -->
                <div
                  v-if="slot.multi"
                  class="flex gap-2 flex-wrap"
                >
                  <button
                    v-for="opt in slot.options"
                    :key="opt.value"
                    :class="['text-xs px-3 py-1.5 rounded-lg border transition-all',
                             (opt.value === 'none' ? form.tunnels.length === 0 : form.tunnels.includes(opt.value))
                               ? 'bg-sky-500 text-white border-sky-500'
                               : 'bg-white text-slate-600 border-slate-200 hover:border-slate-300']"
                    @click="opt.value === 'none' ? form.tunnels.splice(0) : (form.tunnels.includes(opt.value) ? form.tunnels.splice(form.tunnels.indexOf(opt.value),1) : form.tunnels.push(opt.value))"
                  >
                    {{ opt.label }}
                  </button>
                </div>
                <!-- Single-select (everything else) -->
                <div
                  v-else
                  class="flex gap-2 flex-wrap"
                >
                  <button
                    v-for="opt in slot.options"
                    :key="opt.value"
                    :class="['text-xs px-3 py-1.5 rounded-lg border transition-all flex items-center gap-1',
                             form.infra[slot.slot] === opt.value
                               ? 'bg-sky-500 text-white border-sky-500'
                               : 'bg-white text-slate-600 border-slate-200 hover:border-slate-300']"
                    @click="form.infra[slot.slot] = opt.value"
                  >
                    <span v-if="form.infra[slot.slot] === opt.value">✓</span>
                    <span>{{ opt.label }}</span>
                  </button>
                </div>
              </div>
            </div>

            <!-- *arr auth bypass — enable to let SLOP manage *arr apps without per-app API keys -->
            <div class="rounded-lg border border-slate-200 p-3 mt-3">
              <label class="flex items-start gap-3 cursor-pointer">
                <input
                  v-model="form.arr_auth_bypass"
                  type="checkbox"
                  class="mt-0.5 w-4 h-4 rounded border-slate-300 text-sky-500 focus:ring-sky-400 shrink-0"
                >
                <div>
                  <div class="text-sm font-medium text-slate-700">
                    Disable *arr apps' internal authentication
                  </div>
                  <div class="text-xs text-slate-400 mt-0.5">
                    When enabled, Sonarr/Radarr/Lidarr/Prowlarr config files are edited during setup to set
                    AuthenticationMethod=External so SLOP can manage them via API without a separate API key.
                    Apps remain protected by {{ form.infra.auth === 'tinyauth' ? 'TinyAuth' : form.infra.auth === 'authelia' ? 'Authelia' : form.infra.auth === 'authentik' ? 'Authentik' : 'oauth2-proxy' }}.
                  </div>
                </div>
              </label>
            </div>
          </template>

          <!-- Stage 4: Quick Stacks -->
          <template v-if="currentStage === 4">
            <p class="text-xs text-slate-500">
              Select pre-configured app bundles or search for individual apps below.
            </p>
            <div class="space-y-2">
              <div
                v-for="stack in allStacks"
                :key="stack.id"
                :class="['border rounded-lg px-3 py-2 cursor-pointer transition-all flex items-center gap-2.5',
                         form.selectedStacks.includes(stack.id)
                           ? 'border-sky-300 bg-sky-50'
                           : systemProfile && (systemProfile.ram_gb || 0) < (stack.ram_gb || 0)
                             ? 'border-amber-200 bg-amber-50 opacity-75'
                             : 'border-slate-200 hover:border-slate-300']"
                @click="toggleStack(stack.id)"
              >
                <input
                  type="checkbox"
                  :checked="form.selectedStacks.includes(stack.id)"
                  class="w-3.5 h-3.5 rounded border-slate-300 shrink-0"
                  readonly
                >
                <div class="flex-1 min-w-0">
                  <div class="flex items-baseline gap-2">
                    <span class="text-sm font-medium text-slate-800">{{ stack.label }}</span>
                    <span
                      v-if="stack.is_custom"
                      class="text-xs text-sky-500 font-medium"
                    >custom</span>
                    <span
                      :class="['text-xs', systemProfile && (systemProfile.ram_gb || 99) < (stack.ram_gb || 0)
                        ? 'text-amber-600 font-medium' : 'text-slate-400']"
                    >
                      {{ stack.ram_note }}
                      {{ systemProfile && (systemProfile.ram_gb || 99) < (stack.ram_gb || 0) ? '⚠ low RAM' : '' }}
                    </span>
                  </div>
                  <div class="text-xs text-slate-500 truncate">
                    {{ stack.app_keys.map(displayAppKey).join(' · ') }}
                  </div>
                </div>
              </div>
            </div>
            <!-- Individual app search -->
            <div class="border-t border-slate-100 pt-3 mt-3">
              <label class="label text-xs mb-1">Add individual apps</label>
              <div class="relative">
                <input
                  v-model="appSearch"
                  type="text"
                  class="input text-xs pr-8"
                  placeholder="Search catalog (sonarr, immich, jellyfin…)"
                >
              </div>
              <div
                v-if="appSearch && catalogSearchResults.length"
                class="mt-1.5 space-y-1"
              >
                <button
                  v-for="app in catalogSearchResults"
                  :key="app.key"
                  :class="['w-full flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs text-left transition-all',
                           form.individualApps.includes(app.key)
                             ? 'bg-sky-50 border-sky-300 text-sky-700'
                             : 'bg-white border-slate-200 text-slate-700 hover:border-slate-300']"
                  @click="toggleIndividualApp(app.key)"
                >
                  <span>{{ form.individualApps.includes(app.key) ? '✓' : '+' }}</span>
                  <span class="font-medium">{{ app.display_name }}</span>
                  <span class="text-slate-400 ml-auto">{{ app.category }}</span>
                </button>
              </div>
              <div
                v-if="form.individualApps.length"
                class="flex flex-wrap gap-1.5 mt-2"
              >
                <span
                  v-for="key in form.individualApps"
                  :key="key"
                  class="flex items-center gap-1 text-xs bg-sky-50 border border-sky-200 text-sky-700 rounded-full px-2 py-0.5"
                >
                  {{ appDisplayName(key) }}
                  <button
                    class="hover:text-red-500"
                    @click="form.individualApps.splice(form.individualApps.indexOf(key),1)"
                  >✕</button>
                </span>
              </div>
            </div>
          </template>

          <!-- Stage 5: Secrets -->
          <template v-if="currentStage === 5">
            <p class="text-xs text-slate-500 mb-2">
              Based on your selections, these secrets are required. Auto-generated values are pre-filled — review and change if needed. External credentials must be obtained from the links provided.
            </p>
            <div
              v-if="requiredSecrets.length === 0"
              class="text-sm text-green-600 bg-green-50 rounded-lg p-3"
            >
              ✓ No external credentials required for your selections.
            </div>
            <div
              v-else
              class="space-y-3"
            >
              <div
                v-for="secret in requiredSecrets"
                :key="secret.key"
                class="space-y-1"
              >
                <div class="flex items-center justify-between">
                  <label class="label text-xs">
                    {{ secret.label }}
                    <span
                      v-if="secret.required"
                      class="text-red-400"
                    >*</span>
                    <span
                      v-else
                      class="text-slate-400 font-normal"
                    >(optional)</span>
                  </label>
                  <a
                    v-if="secret.link"
                    :href="secret.link"
                    target="_blank"
                    rel="noopener"
                    class="text-xs text-sky-600 underline hover:text-sky-700"
                  >
                    Get credentials ↗
                  </a>
                </div>
                <div class="flex gap-2">
                  <select
                    v-if="secret.type === 'select'"
                    v-model="form.secrets[secret.key]"
                    class="input text-xs flex-1"
                  >
                    <option
                      v-for="opt in secret.options"
                      :key="opt"
                      :value="opt"
                    >
                      {{ opt }}
                    </option>
                  </select>
                  <input
                    v-else
                    v-model="form.secrets[secret.key]"
                    :type="secret.type || (secretVisible[secret.key] ? 'text' : 'password')"
                    :placeholder="secret.placeholder || ''"
                    class="input font-mono text-xs flex-1"
                  >
                  <button
                    v-if="secret.type !== 'text' && secret.type !== 'select'"
                    class="btn-secondary btn-sm text-xs shrink-0"
                    @click="secretVisible[secret.key] = !secretVisible[secret.key]"
                  >
                    {{ secretVisible[secret.key] ? 'Hide' : 'Show' }}
                  </button>
                  <button
                    v-if="secret.auto_generated"
                    class="btn-secondary btn-sm text-xs shrink-0"
                    title="Generate a new random value"
                    @click="regenerateSecret(secret.key)"
                  >
                    ↺ New
                  </button>
                </div>
                <p
                  v-if="secret.note"
                  class="text-xs text-slate-400"
                >
                  {{ secret.note }}
                </p>
              </div>
            </div>

            <!-- Install prompts — per-app configuration collected before install (id=816) -->
            <div
              v-if="appsWithInstallPrompts.length > 0"
              class="mt-4 space-y-3"
            >
              <div class="border-t border-slate-100 pt-3">
                <p class="text-xs font-medium text-slate-700 mb-2">
                  App configuration
                </p>
                <p class="text-xs text-slate-500 mb-3">
                  These apps need additional information before they can be installed.
                </p>
              </div>
              <div
                v-for="app in appsWithInstallPrompts"
                :key="app.key"
                class="rounded-lg border border-slate-200 p-3 space-y-2"
              >
                <p class="text-xs font-medium text-slate-700">
                  {{ app.display_name }}
                </p>
                <div
                  v-for="prompt in app.prompts"
                  :key="prompt.key"
                  class="space-y-1"
                >
                  <label class="label text-xs">
                    {{ prompt.label }}
                    <span
                      v-if="prompt.required"
                      class="text-red-400"
                    >*</span>
                    <span
                      v-else
                      class="text-slate-400 font-normal"
                    >(optional)</span>
                  </label>
                  <input
                    :value="form.installPromptValues[app.key]?.[prompt.key] ?? prompt.default ?? ''"
                    type="text"
                    :placeholder="prompt.type === 'path' ? '/path/on/server' : prompt.default || ''"
                    class="input font-mono text-xs w-full"
                    @input="(e: Event) => {
                      if (!form.installPromptValues[app.key]) form.installPromptValues[app.key] = {}
                      form.installPromptValues[app.key][prompt.key] = (e.target as HTMLInputElement).value
                    }"
                  >
                  <p
                    v-if="prompt.description"
                    class="text-xs text-slate-400"
                  >
                    {{ prompt.description }}
                  </p>
                </div>
              </div>
            </div>

            <!-- Secrets validation feedback -->
            <div
              v-if="secretsValidating"
              class="flex items-center gap-2 text-sm text-slate-500 mt-3"
            >
              <div class="w-4 h-4 border-2 border-sky-400 border-t-transparent rounded-full animate-spin shrink-0" />
              Validating credentials…
            </div>
            <div
              v-if="secretsValidationResult && secretsValidationResult.errors && secretsValidationResult.errors.length"
              class="mt-3 rounded-lg border border-red-200 bg-red-50 p-3 space-y-1"
            >
              <p class="text-xs font-medium text-red-800">
                Validation failed — fix these errors before continuing:
              </p>
              <ul class="text-xs text-red-700 list-disc list-inside space-y-0.5">
                <li
                  v-for="err in secretsValidationResult.errors"
                  :key="err"
                >
                  {{ err }}
                </li>
              </ul>
            </div>
            <div
              v-if="secretsValidationResult && secretsValidationResult.warnings && secretsValidationResult.warnings.length
                && !(secretsValidationResult.errors && secretsValidationResult.errors.length)"
              class="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3"
            >
              <ul class="text-xs text-amber-700 list-disc list-inside space-y-0.5">
                <li
                  v-for="warn in secretsValidationResult.warnings"
                  :key="warn"
                >
                  {{ warn }}
                </li>
              </ul>
            </div>

            <p class="text-xs text-slate-400">
              AI Monitoring Agent is enabled automatically. Configure it in
              <RouterLink
                to="/settings"
                class="text-sky-500 hover:underline"
              >
                Settings → Health
              </RouterLink>.
            </p>
          </template>

          <!-- Stage 6: Review -->
          <template v-if="currentStage === 6">
            <div class="space-y-3">
              <div class="rounded-lg bg-slate-50 border border-slate-200 p-4 space-y-2 text-sm">
                <div class="font-medium text-slate-700 mb-2">
                  Deployment summary
                </div>
                <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  <span class="text-slate-500">Domain</span><span class="font-mono">{{ form.domain }}</span>
                  <span class="text-slate-500">Config root</span><span class="font-mono">{{ form.config_root }}</span>
                  <span class="text-slate-500">DNS provider</span><span>{{ effectiveDnsProviders[form.dns_provider]?.label }}</span>
                  <span class="text-slate-500">Cert authority</span><span>{{ form.cert_resolver }}</span>
                  <span class="text-slate-500">Infra</span>
                  <span>{{ Object.entries(form.infra).filter(([,v]) => v && v !== 'none').map(([k,v]) => v).join(', ') || 'Traefik only' }}</span>
                  <span class="text-slate-500">Quick stacks</span>
                  <span>{{ form.selectedStacks.length ? form.selectedStacks.map(id => allStacks.find(s => s.id === id)?.label ?? id).join(', ') : 'None selected' }}</span>
                  <span class="text-slate-500">Notifications</span>
                  <span>{{ form.ntfy_enabled ? 'ntfy on ' + (form.ntfy_url || 'http://ntfy:80') + ' → ' + form.ntfy_topic : 'Will enable after installing ntfy from Catalog' }}</span>
                </div>
              </div>
              <!-- Deploy manifest — what will actually be started -->
              <div class="rounded-lg bg-slate-50 border border-slate-200 p-3 space-y-1 text-xs">
                <p class="font-medium text-slate-700 mb-1.5">
                  📦 Will be deployed now
                </p>
                <div class="flex items-center gap-2 text-slate-600">
                  <span class="text-green-500 shrink-0">✓</span>
                  <span>Traefik — reverse proxy</span>
                </div>
                <div
                  v-if="form.infra.auth && form.infra.auth !== 'none'"
                  class="flex items-center gap-2 text-slate-600"
                >
                  <span class="text-green-500 shrink-0">✓</span>
                  <span>{{ form.infra.auth === 'tinyauth' ? 'TinyAuth' : form.infra.auth === 'authelia' ? 'Authelia' : form.infra.auth === 'authentik' ? 'Authentik' : 'oauth2-proxy' }} — authentication</span>
                </div>
                <div
                  v-for="t in form.tunnels"
                  :key="t"
                  class="flex items-center gap-2 text-slate-600"
                >
                  <span class="text-green-500 shrink-0">✓</span>
                  <span>{{ t === 'cloudflared' ? 'Cloudflare Tunnel' : t === 'tailscale' ? 'Tailscale' : t === 'headscale' ? 'Headscale (self-hosted)' : t }}</span>
                </div>
                <div
                  v-if="form.infra.vpn && form.infra.vpn !== 'none'"
                  class="flex items-center gap-2 text-slate-600"
                >
                  <span class="text-green-500 shrink-0">✓</span>
                  <span>Gluetun — VPN gateway</span>
                </div>
                <div
                  v-if="form.infra.dashboard && form.infra.dashboard !== 'none'"
                  class="flex items-center gap-2 text-slate-600"
                >
                  <span class="text-green-500 shrink-0">✓</span>
                  <span>{{ form.infra.dashboard === 'glance' ? 'Glance' : 'Homepage' }} — homelab dashboard</span>
                </div>
                <div
                  v-if="form.infra.management && form.infra.management !== 'none'"
                  class="flex items-center gap-2 text-slate-600"
                >
                  <span class="text-green-500 shrink-0">✓</span>
                  <span>Komodo — container management</span>
                </div>
              </div>
              <!-- Quick stacks — scheduled for Stage 8 after deploy -->
              <div
                v-if="form.selectedStacks.length > 0"
                class="rounded-lg bg-sky-50 border border-sky-100 p-3 text-xs space-y-1"
              >
                <p class="font-medium text-sky-700 mb-1">
                  🔜 App installs after deploy (Stage 8)
                </p>
                <div
                  v-for="id in form.selectedStacks"
                  :key="id"
                  class="flex items-center gap-2 text-sky-600"
                >
                  <span class="shrink-0">›</span>
                  <span>{{ allStacks.find(s => s.id === id)?.label ?? id }}</span>
                </div>
              </div>
              <div class="text-xs text-amber-700 rounded-lg bg-amber-50 border border-amber-100 p-2">
                ⚠ Existing installed apps are not affected by this deployment.
              </div>
            </div>
          </template>

          <!-- Stage 7: Platform Deploy -->
          <template v-if="currentStage === 7">
            <div class="space-y-2">
              <!-- Live progress: show the most recent step while running -->
              <div
                v-if="running && stepResults.length"
                class="flex items-center gap-3"
              >
                <div class="w-3 h-3 border-2 border-sky-400 border-t-transparent rounded-full animate-spin shrink-0" />
                <div class="flex-1 min-w-0">
                  <div class="text-sm font-medium text-slate-700">
                    {{ stepResults[stepResults.length - 1].message }}
                  </div>
                </div>
              </div>
              <div
                v-if="running"
                class="text-xs text-slate-400"
              >
                Deploying platform…
              </div>

              <!-- Running spinner when no steps yet -->
              <div
                v-if="running && !stepResults.length"
                class="flex items-center gap-2 text-sm text-slate-400"
              >
                <div class="w-4 h-4 border-2 border-sky-400 border-t-transparent rounded-full animate-spin" />
                Deploying platform…
              </div>
            </div>

            <!-- Detailed step log — collapsed by default after success -->
            <details
              v-if="!running && stepResults.length"
              class="mt-3"
            >
              <summary class="text-xs text-slate-400 cursor-pointer hover:text-slate-600 select-none">
                Show deployment log ({{ stepResults.length }} steps)
              </summary>
              <div class="space-y-1.5 mt-2 pl-2 border-l-2 border-slate-100">
                <div
                  v-for="step in stepResults"
                  :key="step.step"
                  class="flex items-start gap-2 text-xs"
                >
                  <span
                    :class="['shrink-0 mt-px',
                             step.status === 'ok' ? 'text-green-500' :
                             step.status === 'skipped' ? 'text-slate-300' :
                             'text-red-500']"
                  >
                    {{ step.status === 'ok' ? '✓' : step.status === 'skipped' ? '−' : '✗' }}
                  </span>
                  <span :class="step.status === 'skipped' ? 'text-slate-400' : 'text-slate-600'">
                    {{ step.message }}
                  </span>
                </div>
              </div>
            </details>

            <!-- Running progress (expanded view) -->
            <div
              v-if="running && stepResults.length"
              class="mt-2 space-y-1.5 pl-2 border-l-2 border-sky-100"
            >
              <div
                v-for="step in stepResults"
                :key="step.step"
                class="flex items-start gap-2 text-xs"
              >
                <span
                  :class="['shrink-0 mt-px',
                           step.status === 'ok' ? 'text-green-500' :
                           step.status === 'skipped' ? 'text-slate-300' :
                           'text-red-500']"
                >
                  {{ step.status === 'ok' ? '✓' : step.status === 'skipped' ? '−' : '✗' }}
                </span>
                <span :class="step.status === 'skipped' ? 'text-slate-400' : 'text-slate-600'">
                  {{ step.message }}
                </span>
              </div>
            </div>

            <!-- Error block -->
            <div
              v-if="setupError"
              class="rounded-xl border border-red-200 bg-red-50 p-4 mt-3"
            >
              <p class="text-sm font-medium text-red-800">
                Deployment failed
              </p>
              <p class="text-sm text-red-700 mt-1">
                {{ setupError }}
              </p>
              <button
                :disabled="running"
                class="btn-primary btn-sm mt-3"
                @click="runWizard"
              >
                ↺ Retry
              </button>
            </div>
            <div
              v-if="setupSuccess"
              class="rounded-xl border border-green-200 bg-green-50 p-4"
            >
              <p class="text-green-800 font-medium text-center mb-2">
                ✅ Platform deployed!
              </p>
              <div class="space-y-2">
                <div class="flex items-center gap-2 text-xs text-green-700">
                  <span>✓</span><span>SSL certificates issued for *.{{ form.domain }}</span>
                </div>
                <div
                  v-if="certStatus && !certStatus.cert_found"
                  class="flex items-center gap-2 text-xs text-sky-600"
                >
                  <span class="w-3 h-3 border-2 border-sky-400 border-t-transparent rounded-full animate-spin shrink-0" />
                  <span>Checking certificate status…</span>
                </div>
                <div
                  v-if="form.infra.auth && form.infra.auth !== 'none'"
                  class="text-xs text-slate-600 bg-slate-50 rounded px-2 py-1"
                >
                  {{ form.infra.auth === 'tinyauth' ? 'TinyAuth' : form.infra.auth === 'authelia' ? 'Authelia' : form.infra.auth === 'authentik' ? 'Authentik' : 'oauth2-proxy' }}
                  is protecting your apps. Log in with the username / password you chose on the Secrets page.
                </div>
                <div
                  v-if="form.tunnels.length > 0"
                  class="text-xs text-slate-600"
                >
                  External access via {{ form.tunnels.join(", ") }}.
                </div>
              </div>
            </div>
          </template>

          <!-- Stage 8: App Deploy -->
          <template v-if="currentStage === 8">
            <div
              v-if="form.selectedStacks.length === 0 && form.individualApps.length === 0"
              class="text-center py-8 text-slate-400"
            >
              <div class="text-2xl mb-2">
                📦
              </div>
              <div class="text-sm font-medium">
                No apps selected
              </div>
              <div class="text-xs mt-1">
                You can install apps anytime from the Catalog.
              </div>
            </div>
            <div
              v-else
              class="space-y-3"
            >
              <!-- Progress header -->
              <div
                v-if="installStarted"
                class="space-y-1.5"
              >
                <div class="flex items-center justify-between">
                  <span class="text-xs font-medium text-slate-600">Installing apps…</span>
                  <div class="flex items-center gap-2">
                    <span class="text-xs text-slate-500">
                      {{ installDoneCount }} / {{ installTotalCount }}
                    </span>
                    <div
                      v-if="!stackInstallDone"
                      class="flex items-center gap-1.5 text-xs text-sky-500"
                    >
                      <span class="w-3 h-3 border-2 border-sky-400 border-t-transparent rounded-full animate-spin" />
                      Installing…
                    </div>
                    <div
                      v-else-if="stackAppsToInstall.every(k => appInstallStatus[k] === 'ok')"
                      class="text-xs text-green-500 font-medium"
                    >
                      ✓ All installed
                    </div>
                  </div>
                </div>
                <div class="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                  <div
                    class="h-full bg-sky-500 transition-all duration-300 rounded-full"
                    :style="`width: ${installTotalCount ? (installDoneCount / installTotalCount) * 100 : 0}%`"
                  />
                </div>
              </div>

              <!-- App cards grid — compact multi-column layout -->
              <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
                <div
                  v-for="app in stackAppsToInstall"
                  :key="app"
                  :class="['rounded-lg border px-3 py-2.5 flex items-center gap-2.5 transition-colors duration-300',
                           appInstallStatus[app] === 'ok' ? 'border-green-200 bg-white' :
                           appInstallStatus[app] === 'error' ? 'border-red-200 bg-red-50' :
                           appInstallStatus[app] === 'running' ? 'border-sky-200 bg-white' :
                           'border-slate-100 bg-white']"
                >
                  <div class="text-lg shrink-0">
                    {{ appIcon(app) }}
                  </div>
                  <div class="flex-1 min-w-0">
                    <div class="text-xs font-medium text-slate-700 truncate">
                      {{ appDisplayName(app) }}
                    </div>
                    <div class="flex items-center gap-1.5 mt-0.5">
                      <span
                        v-if="appInstallStatus[app] === 'ok'"
                        class="w-1.5 h-1.5 rounded-full bg-green-500 shrink-0"
                      />
                      <span
                        v-else-if="appInstallStatus[app] === 'error'"
                        class="w-1.5 h-1.5 rounded-full bg-red-500 shrink-0"
                      />
                      <span
                        v-else-if="appInstallStatus[app] === 'running'"
                        class="w-1.5 h-1.5 rounded-full bg-sky-400 shrink-0 animate-pulse"
                      />
                      <span
                        v-else
                        class="w-1.5 h-1.5 rounded-full bg-slate-200 shrink-0"
                      />
                      <span
                        :class="['text-[11px]',
                                 appInstallStatus[app] === 'ok' ? 'text-green-600' :
                                 appInstallStatus[app] === 'error' ? 'text-red-500' :
                                 appInstallStatus[app] === 'running' ? 'text-sky-500' :
                                 'text-slate-400']"
                      >
                        {{ appInstallStatus[app] === 'ok' ? 'Done' :
                           appInstallStatus[app] === 'error' ? 'Failed' :
                           appInstallStatus[app] === 'running' ? appInstallProgress[app] || 'Installing…' :
                           'Queued' }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Error detail for failed apps — collapsed -->
              <details
                v-if="stackAppsToInstall.some(k => appInstallStatus[k] === 'error')"
                class="mt-2"
              >
                <summary class="text-xs text-slate-400 cursor-pointer hover:text-slate-600 select-none">
                  Show error details
                </summary>
                <div
                  v-for="app in stackAppsToInstall.filter(k => appInstallStatus[k] === 'error')"
                  :key="app"
                  class="text-xs text-red-500 mt-1 break-all"
                >
                  <span class="font-medium">{{ app }}:</span> {{ appInstallError[app] }}
                </div>
              </details>

              <!-- Footer actions -->
              <div
                v-if="stackInstallDone && stackAppsToInstall.some(k => appInstallStatus[k] === 'error')"
                class="flex items-center justify-between pt-2 border-t border-slate-100"
              >
                <p class="text-xs text-slate-400">
                  Failed apps can be installed from Catalog after setup.
                </p>
                <button
                  class="btn-secondary btn-sm text-xs"
                  @click="retryFailedApps"
                >
                  ↺ Retry failed
                </button>
              </div>
            </div>
          </template>

          <!-- Stage 9: AI Agent — mandatory; content varies by provider -->
          <template v-if="currentStage === 9">
            <p class="text-xs text-slate-500 mb-3">
              The SLOP AI agent monitors your stack and diagnoses problems. It requires a local LLM.
            </p>
            <div class="border border-sky-200 rounded-lg p-3 bg-sky-50 space-y-3">
              <div class="flex items-center gap-2 text-sm font-medium text-sky-700">
                <span>🤖</span><span>Ollama AI Agent</span>
              </div>
              <div class="space-y-2">
                <div class="flex items-center gap-2">
                  <label class="text-xs text-slate-500 w-14 shrink-0">Server:</label>
                  <select
                    v-model="form.ollama_server"
                    class="input text-xs w-52 shrink-0"
                  >
                    <option value="local">
                      This server (install now)
                    </option>
                    <option value="remote">
                      Remote Ollama server
                    </option>
                  </select>
                  <input
                    v-if="form.ollama_server === 'remote'"
                    v-model="form.ollama_url"
                    type="text"
                    placeholder="http://192.168.1.x:11434"
                    class="input text-xs flex-1 font-mono"
                  >
                </div>
                <div class="flex items-center gap-2">
                  <label class="text-xs text-slate-500 w-14 shrink-0">Model:</label>
                  <select
                    v-model="form.ollama_model"
                    class="input text-xs flex-1"
                  >
                    <option
                      v-for="m in ollamaModelOptions"
                      :key="m.value"
                      :value="m.value"
                    >
                      {{ m.label }}
                    </option>
                  </select>
                </div>
                <div class="text-xs text-slate-400">
                  <span v-if="form.ollama_server === 'local'">Ollama will be installed on this server and the model downloaded during setup.</span>
                  <span v-else>Model will be pulled on your remote Ollama server.</span>
                </div>
                <!-- Estimated download time for known models (local only) -->
                <div
                  v-if="form.ollama_server === 'local' && ollamaModelSizeInfo"
                  class="text-xs text-slate-400 italic"
                >
                  {{ ollamaModelSizeInfo }}
                </div>

                <!-- Install + pull progress -->
                <div
                  v-if="ollamaSetupJob"
                  class="rounded-lg bg-white border border-slate-100 p-3 space-y-2"
                >
                  <div class="flex items-center gap-2 text-xs">
                    <span
                      v-if="!ollamaSetupJob.done"
                      class="w-3 h-3 border border-sky-400 border-t-transparent rounded-full animate-spin shrink-0"
                    />
                    <span
                      v-else-if="ollamaSetupJob.ok"
                      class="text-green-500 shrink-0"
                    >✓</span>
                    <span
                      v-else
                      class="text-red-500 shrink-0"
                    >✗</span>
                    <span :class="ollamaSetupJob.ok ? 'text-green-700' : ollamaSetupJob.phase === 'error' ? 'text-red-600' : 'text-slate-600'">
                      {{ ollamaSetupJob.message }}
                    </span>
                  </div>
                  <div
                    v-if="!ollamaSetupJob.done || ollamaSetupJob.ok"
                    class="w-full bg-slate-100 rounded-full h-1.5"
                  >
                    <div
                      class="bg-sky-500 h-1.5 rounded-full transition-all duration-500"
                      :style="{ width: ollamaSetupJob.progress + '%' }"
                    />
                  </div>
                  <details
                    v-if="ollamaSetupJob.phase === 'error' && ollamaSetupJob.errorDetail"
                    class="text-xs"
                  >
                    <summary class="cursor-pointer text-slate-500 hover:text-slate-700 select-none">
                      Show error log
                    </summary>
                    <pre class="mt-1 p-2 bg-slate-50 border border-slate-200 rounded text-red-700 text-[11px] leading-relaxed overflow-x-auto whitespace-pre-wrap max-h-40 overflow-y-auto">{{ ollamaSetupJob.errorDetail }}</pre>
                  </details>
                  <button
                    v-if="ollamaSetupJob.phase === 'error'"
                    class="btn-secondary btn-sm text-xs"
                    @click="startOllamaSetup"
                  >
                    ↺ Retry
                  </button>
                </div>

                <!-- AI is mandatory — no skip option -->

                <!-- Start button (before job begins) -->
                <button
                  v-if="!ollamaSetupJob && !ollamaSetupJobId"
                  class="btn-primary btn-sm text-xs"
                  @click="startOllamaSetup"
                >
                  ▶ Install Ollama + download {{ form.ollama_model }}
                </button>
                <div
                  v-if="ollamaSetupJob?.ok"
                  class="flex items-center gap-1.5 text-xs text-green-600"
                >
                  <span>✓</span><span>Ready — activating AI agent now</span>
                </div>
              </div>
            </div>
          </template>

          <!-- Stage 10: Storage -->
          <template v-if="currentStage === 10">
            <p class="text-xs text-slate-500">
              Configure shared storage sources. Media files will be accessible to all arr apps and media servers.
            </p>
            <div class="space-y-3">
              <div class="border border-slate-200 rounded-lg p-3">
                <div class="text-sm font-medium mb-2">
                  Media directory
                </div>
                <input
                  v-model="form.media_root"
                  type="text"
                  class="input text-xs"
                >
                <p class="text-xs text-slate-400 mt-1">
                  Local path to your media files. All apps will mount this as /media.
                </p>
              </div>
              <div class="border border-slate-200 rounded-lg p-3">
                <div class="flex items-center justify-between mb-2">
                  <div class="text-sm font-medium">
                    NFS mount
                  </div>
                  <label class="flex items-center gap-2 cursor-pointer">
                    <input
                      v-model="form.nfs_enabled"
                      type="checkbox"
                      class="w-3.5 h-3.5"
                    >
                    <span class="text-xs">Enable</span>
                  </label>
                </div>
                <div
                  v-if="form.nfs_enabled"
                  class="grid grid-cols-2 gap-2"
                >
                  <input
                    v-model="form.nfs_server"
                    type="text"
                    class="input text-xs"
                    placeholder="192.168.1.10"
                  >
                  <input
                    v-model="form.nfs_export"
                    type="text"
                    class="input text-xs"
                    placeholder="/exports/media"
                  >
                </div>
              </div>
              <p class="text-xs text-slate-400">
                NFS settings will be saved when you click Finish. rclone and other sources can be added later via the Storage page.
              </p>
            </div>
          </template>
        </div>
      </div>

      <!-- Navigation buttons -->
      <div class="flex gap-3">
        <button
          v-if="currentStage > 0"
          class="btn-secondary"
          @click="prevStage"
        >
          ← Back
        </button>
        <div class="flex-1" />
        <button
          v-if="currentStage < STAGES.length - 1"
          :disabled="!canAdvance"
          class="btn-primary"
          @click="nextStage"
        >
          {{ currentStage === 6 ? 'Deploy Platform →' : 'Continue →' }}
        </button>
        <button
          v-if="currentStage === STAGES.length - 1"
          :disabled="finishing"
          class="btn-primary"
          @click="finish"
        >
          {{ finishing ? "Finishing…" : "Finish Setup →" }}
        </button>
      </div>

      <!-- Continue-blocked hint — shown when Continue is disabled.
           Pin a stable min-height so the page doesn't jump when the
           message disappears after the last required field is filled. -->
      <div class="text-center mt-2" style="min-height: 2.5rem">
        <p
          v-if="!canAdvance && advanceBlockReason"
          class="text-xs text-amber-600"
        >
          ⚠ {{ advanceBlockReason }}
        </p>
      </div>


      <p
        v-if="currentStage < 6"
        class="text-center text-xs text-slate-400 mt-3"
      >
        <button
          class="text-sky-500 hover:underline"
          @click="router.push('/catalog')"
        >
          Skip to Catalog — configure later
        </button>
      </p>
    </template>

    <!-- Reset confirmation modal -->
    <Teleport to="body">
      <div
        v-if="showReset"
        class="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="showReset = false"
        />
        <div class="relative card w-full max-w-md mx-4 card-body">
          <h3 class="font-semibold text-slate-900">
            Reset platform
          </h3>
          <p class="text-sm text-slate-500 mt-2 mb-4">
            Choose how much to reset:
          </p>
          <div class="space-y-3">
            <div class="rounded-lg border border-slate-200 p-3">
              <div class="font-medium text-sm text-slate-800 mb-1">
                ↺ Re-run wizard
              </div>
              <p class="text-xs text-slate-500">
                Resets platform status to pending so you can reconfigure. Stops and removes all infrastructure containers (Traefik, auth, tunnels, dashboard). Wipes health data and infra state. <strong>Installed apps are not affected.</strong>
              </p>
              <button
                class="btn-secondary btn-sm mt-2 w-full"
                @click="doReset"
              >
                Re-run wizard
              </button>
            </div>
            <div class="rounded-lg border border-red-200 bg-red-50 p-3">
              <div class="font-medium text-sm text-red-800 mb-1">
                ⚠ Factory reset
              </div>
              <p class="text-xs text-red-700">
                Destroys everything. Stops ALL containers, removes all compose fragments, deletes the entire database, and clears .env. You will start completely from scratch. <strong>This cannot be undone.</strong>
              </p>
              <button
                class="btn-danger btn-sm mt-2 w-full"
                @click="doFullReset"
              >
                Factory reset
              </button>
            </div>
          </div>
          <button
            class="btn-secondary w-full mt-3"
            @click="showReset = false"
          >
            Cancel
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import { RouterLink, useRouter, useRoute } from 'vue-router'
import { usePlatformStore } from '../stores/platform'
import { platform as platformApi, catalog as catalogApi, settings as settingsApi, health as healthApi, apps as appsApi, storage as storageApi } from '../api/client'
import { useToast } from '@/composables/useToast'

const platformStore = usePlatformStore()
const router = useRouter()
const route = useRoute()
const toast = useToast()

// Allow forcing wizard open even when platform is ready (from Settings re-run)
const forceSetup = ref(route.query.force === 'true')

// ── DNS Provider metadata ─────────────────────────────────────────────────
// Fetched from GET /api/v1/platform/dns-providers on mount; kept as fallback if fetch fails.
const DNS_PROVIDERS_FALLBACK: Record<string, { label: string; vars: string[]; link: string }> = {
  cloudflare:   { label: "Cloudflare",       vars: ["CF_DNS_API_TOKEN"],                                                      link: "https://dash.cloudflare.com/profile/api-tokens" },
  route53:      { label: "AWS Route 53",      vars: ["AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","AWS_REGION","AWS_HOSTED_ZONE_ID"], link: "https://console.aws.amazon.com/iam/" },
  namecheap:    { label: "Namecheap",         vars: ["NAMECHEAP_API_USER","NAMECHEAP_API_KEY"],                                link: "https://ap.www.namecheap.com/settings/tools/apiaccess/" },
  porkbun:      { label: "Porkbun",           vars: ["PORKBUN_API_KEY","PORKBUN_SECRET_API_KEY"],                              link: "https://porkbun.com/account/api" },
  digitalocean: { label: "DigitalOcean",      vars: ["DO_AUTH_TOKEN"],                                                        link: "https://cloud.digitalocean.com/account/api/tokens" },
  gandi:        { label: "Gandi",             vars: ["GANDI_PERSONAL_ACCESS_TOKEN"],                                          link: "https://account.gandi.net/en/users/_/security" },
  hetzner:      { label: "Hetzner",           vars: ["HETZNER_API_KEY"],                                                      link: "https://dns.hetzner.com/settings/api-token" },
  linode:       { label: "Linode / Akamai",   vars: ["LINODE_TOKEN"],                                                         link: "https://cloud.linode.com/profile/tokens" },
  ovh:          { label: "OVH",               vars: ["OVH_ENDPOINT","OVH_APPLICATION_KEY","OVH_APPLICATION_SECRET","OVH_CONSUMER_KEY"], link: "https://api.ovh.com/createToken/" },
  godaddy:      { label: "GoDaddy",           vars: ["GODADDY_API_KEY","GODADDY_API_SECRET"],                                  link: "https://developer.godaddy.com/keys" },
  duckdns:      { label: "DuckDNS",           vars: ["DUCKDNS_TOKEN"],                                                        link: "https://www.duckdns.org/" },
  desec:        { label: "deSEC",             vars: ["DESEC_TOKEN"],                                                          link: "https://desec.io/tokens" },
  njalla:       { label: "Njalla",            vars: ["NJALLA_TOKEN"],                                                         link: "https://njal.la/settings/api/" },
  inwx:         { label: "INWX",              vars: ["INWX_USERNAME","INWX_PASSWORD"],                                        link: "https://www.inwx.com/en/offer/api" },
  infomaniak:   { label: "Infomaniak",        vars: ["INFOMANIAK_ACCESS_TOKEN"],                                              link: "https://manager.infomaniak.com/v3/profile/api-tokens" },
  azure:        { label: "Azure DNS",         vars: ["AZURE_CLIENT_ID","AZURE_CLIENT_SECRET","AZURE_SUBSCRIPTION_ID","AZURE_RESOURCE_GROUP"], link: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
  google:       { label: "Google Cloud DNS",  vars: ["GCE_PROJECT","GCE_SERVICE_ACCOUNT_FILE"],                               link: "https://console.cloud.google.com/iam-admin/serviceaccounts" },
  vultr:        { label: "Vultr",             vars: ["VULTR_API_KEY"],                                                        link: "https://my.vultr.com/settings/#settingsapi" },
  bunny:        { label: "Bunny DNS",         vars: ["BUNNY_API_KEY"],                                                        link: "https://panel.bunny.net/account" },
  dnspod:       { label: "DNSPod",            vars: ["DNSPOD_API_KEY","DNSPOD_SECRET_ID"],                                    link: "https://console.dnspod.cn/account/token/apikey" },
}

// Reactive ref populated on mount from /api/v1/platform/dns-providers.
// Falls back to DNS_PROVIDERS_FALLBACK if the fetch fails or returns empty.
const dnsProviders = ref<Record<string, { label: string; vars: string[]; link: string }>>({})
const effectiveDnsProviders = computed(() =>
  Object.keys(dnsProviders.value).length > 0 ? dnsProviders.value : DNS_PROVIDERS_FALLBACK
)

// ── Stage definitions ─────────────────────────────────────────────────────
const STAGES = [
  { id: 'prereqs',    label: 'Prerequisites', description: 'Verify Docker and system requirements' },
  { id: 'core',       label: 'Core Config',   description: 'Domain, paths, and system settings' },
  { id: 'tls',        label: 'DNS',            description: 'Certificate authority and DNS provider' },
  { id: 'infra',      label: 'Infrastructure',description: 'Auth, tunnel, VPN, and dashboard apps' },
  { id: 'stacks',     label: 'Quick Stacks',  description: 'Optional: select app bundles to pre-install' },
  { id: 'secrets',    label: 'Secrets',       description: 'Credentials required by your selections' },
  { id: 'review',     label: 'Review',        description: 'Confirm your configuration before deploying' },
  { id: 'deploy',     label: 'Deploy',        description: 'Deploy Traefik and infrastructure' },
  { id: 'apps',       label: 'Apps',          description: 'Install selected quick stacks' },
  { id: 'ai',         label: 'AI Agent',    description: 'Mandatory: configure the SLOP AI agent' },
  { id: 'storage',    label: 'Storage',       description: 'Optional: configure shared storage' },
]

// Tunnel supports multi-select (you can run cloudflared + tailscale simultaneously)
const INFRA_SLOTS = [
  { slot: 'auth', label: 'Authentication', description: 'Enabled by default — protects all apps with a login page. Choose your auth provider.', multi: false, core: true,
    options: [
      { value: 'tinyauth', label: 'TinyAuth (simple)' },
      { value: 'authelia', label: 'Authelia (full SSO)' },
      { value: 'authentik', label: 'Authentik (enterprise)' },
      { value: 'oauth2-proxy', label: 'oauth2-proxy (social login)' }
    ] },
  { slot: 'tunnel', label: 'External Access Tunnel', description: 'Expose apps without port forwarding. Select all that apply.', multi: true,
    options: [
      { value: 'none', label: 'None' },
      { value: 'cloudflared', label: 'Cloudflare Tunnel' },
      { value: 'tailscale', label: 'Tailscale' },
      { value: 'headscale', label: 'Headscale (self-hosted)' },
      { value: 'netbird', label: 'NetBird' },
      { value: 'zerotier', label: 'ZeroTier' },
      { value: 'pangolin', label: 'Pangolin' },
      { value: 'nebula', label: 'Nebula' }
    ] },
  { slot: 'vpn', label: 'VPN (for download clients)', description: 'Route torrent/usenet traffic through VPN', multi: false,
    options: [{ value: 'none', label: 'None' }, { value: 'gluetun', label: 'Gluetun' }] },
  { slot: 'dashboard', label: 'Dashboard', description: 'Homepage for your homelab', multi: false,
    options: [{ value: 'none', label: 'None' }, { value: 'glance', label: 'Glance' }, { value: 'homepage', label: 'Homepage' }] },
  { slot: 'management', label: 'Container Management', description: 'GUI to manage Docker stacks', multi: false,
    options: [{ value: 'none', label: 'None' }, { value: 'dockge', label: 'Dockge' }, { value: 'portainer', label: 'Portainer' }, { value: 'dockhand', label: 'Dockhand' }, { value: 'komodo', label: 'Komodo' }] },
]

// Quick stacks — loaded from /api/v1/platform/stacks on mount (includes custom + defaults).
// Each entry: { id, label, app_keys, ram_note, ram_gb, is_custom, is_default_override }
const allStacks = ref<any[]>([])

// ── Form state ─────────────────────────────────────────────────────────────
const form = reactive({
  domain: '',
  config_root: '/var/lib/slop/config',
  media_root: '/mnt/media',
  puid: 1000,
  pgid: 1000,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  cert_resolver: 'staging',
  network_name: 'slop',
  acme_email: '',
  dns_provider: '',
  eab_kid: '',
  eab_hmac: '',
  ntfy_url: 'http://ntfy:80',
  ntfy_topic: 'slop',
  ntfy_enabled: true,  // pre-enable since http://ntfy:80 is the correct default
  infra: { auth: 'tinyauth', vpn: 'none', dashboard: 'none', management: 'none' } as Record<string, string>,
  tunnels: [] as string[],  // multi-select: can have cloudflared + tailscale simultaneously
  selectedStacks: [] as string[],
  secrets: {} as Record<string, string>,
  arr_auth_bypass: true,
  arr_auth_provider: 'tinyauth',
  llm_provider: 'ollama',
  groq_api_key: '',
  groq_model: 'llama-3.3-70b-versatile',
  awan_model: 'Meta-Llama-3.1-8B-Instruct',
  cerebras_api_key: '',
  cerebras_model: 'llama-3.3-70b',
  openai_api_key: '',
  openai_model: 'gpt-4o-mini',
  llamacpp_url: 'http://localhost:8081',
  llamacpp_model: 'phi-4-mini',
  awan_api_key: '',
  ollama_model: 'phi4-mini',
  ollama_server: 'local',
  ollama_url: 'http://localhost:11434',
  nfs_enabled: false,
  nfs_server: '',
  nfs_export: '',
  individualApps: [] as string[],
  // install_prompts values: { [appKey]: { [promptKey]: value } }
  installPromptValues: {} as Record<string, Record<string, string>>,
})

// ── Stage navigation ───────────────────────────────────────────────────────
const currentStage = ref(0)
const maxReachedStage = ref(0)

function goToStage(i: number) {
  if (i <= maxReachedStage.value) currentStage.value = i
}

function prevStage() {
  if (currentStage.value > 0) currentStage.value--
}

async function nextStage() {
  if (currentStage.value === 6) {
    // Save LLM config before deploy so the AI agent can observe from the start
    await saveLLMConfig()
    // Stage 6 → 7: trigger wizard deployment
    currentStage.value = 7
    maxReachedStage.value = Math.max(maxReachedStage.value, 7)
    await runWizard()
    return
  }
  if (currentStage.value === 7) {
    // Stage 7 → 8: start app installs
    currentStage.value = 8
    maxReachedStage.value = Math.max(maxReachedStage.value, 8)
    if (form.selectedStacks.length > 0 || form.individualApps.length > 0) {
      await installStacks()
    } else {
      stackInstallDone.value = true
    }
    return
  }
  // Before leaving Stage 5 (Secrets): run validation checks
  if (currentStage.value === 5) {
    // Quick connectivity validation — check credentials work before committing to deploy
    const toValidate: string[] = []
    if (form.infra.vpn === 'gluetun') toValidate.push('vpn')
    if (form.tunnels.includes('cloudflared')) toValidate.push('cloudflared')
    if (form.tunnels.includes('tailscale')) toValidate.push('tailscale')
    if (form.dns_provider && form.dns_provider !== 'none') toValidate.push('dns')

    if (toValidate.length > 0) {
      secretsValidating.value = true
      secretsValidationResult.value = null
      try {
        const { ok, data } = await platformApi.validateSecretsRaw({
          checks: toValidate,
          vpn_type: form.secrets['VPN_TYPE'],
          vpn_provider: form.secrets['VPN_SERVICE_PROVIDER'],
          vpn_key: form.secrets['WIREGUARD_PRIVATE_KEY'] || form.secrets['OPENVPN_USER'],
          cf_tunnel_token: form.secrets['CF_TUNNEL_TOKEN'],
          tailscale_key: form.secrets['TAILSCALE_AUTH_KEY'],
          cf_dns_token: form.secrets['CF_DNS_API_TOKEN'],
          dns_provider: form.dns_provider,
        })
        // net-vs-http: HTTP-non-ok → fallbackA; a null body on ok (unparseable 200) or a
        // network error (→ catch below) → the same "proceed anyway" fallback the raw
        // `await r.json()` throw produced. Preserves the two distinct fallbacks exactly.
        const result = ok
          ? (data ?? { ok: true, warnings: ['Could not validate — proceeding anyway'] })
          : { ok: false, warnings: ['Validation endpoint not reachable'] }
        secretsValidationResult.value = result
        // Block on hard failures, warn on soft failures
        if (result.errors?.length) {
          secretsValidating.value = false
          return  // stay on Stage 5 and show errors
        }
      } catch {
        secretsValidationResult.value = { ok: true, warnings: ['Could not validate — proceeding anyway'] }
      }
      secretsValidating.value = false
    }
  }

  if (currentStage.value === 5 && form.infra.auth === 'tinyauth') {
    const user = form.secrets['TINYAUTH_USERNAME']?.trim() || 'admin'
    const pass = form.secrets['TINYAUTH_PASSWORD']?.trim()
    if (pass) {
      try {
        const d = await platformApi.bcryptUsers({ username: user, password: pass })
        form.secrets['TINYAUTH_AUTH_USERS'] = d.users
        form.secrets['TINYAUTH_USERNAME'] = user
      } catch { /* intentional: non-fatal */ }
    }
  }
  // Before advancing to Review (Stage 6): start pre-pulling selected app images.
  // Fire-and-forget — images already on disk are a cache hit (instant return).
  if (currentStage.value === 5) {
    const appsToPrefetch = stackAppsToInstall.value
    if (appsToPrefetch.length > 0) {
      appsApi.batchPrefetch(appsToPrefetch).catch(() => {})
    }
  }

  currentStage.value++
  maxReachedStage.value = Math.max(maxReachedStage.value, currentStage.value)
}

const finishing = ref(false)
const wizardLLMProviders = ref<any>(null)

async function finish() {
  finishing.value = true
  clearDraft()
  saveLLMConfig().catch(() => {})
  if (form.nfs_enabled && form.nfs_server && form.nfs_export) {
    storageApi.add({
      name: 'NFS Media',
      type: 'nfs',
      host: form.nfs_server,
      path: form.nfs_export,
      mount_point: form.media_root,
      enabled: true,
    }).catch(() => {})
  }
  try {
    await platformStore.fetchStatus()
  } catch { /* non-fatal — navigate anyway */ }
  router.push('/')
  finishing.value = false
}

const canAdvance = computed(() => {
  if (currentStage.value === 0)
    return prereqChecks.value.length > 0 && !prereqChecks.value.some(c => c.status === 'error')
  if (currentStage.value === 1) return !!form.domain
  if (currentStage.value === 2)
    return form.cert_resolver !== 'zerossl' || (!!form.eab_kid && !!form.eab_hmac)
  if (currentStage.value === 5) {
    // All required, non-auto-generated secrets must be filled
    const missing = requiredSecrets.value.filter(
      s => s.required && !s.auto_generated && !form.secrets[s.key]?.trim()
    )
    return missing.length === 0
  }
  if (currentStage.value === 7) return setupSuccess.value
  if (currentStage.value === 8) return stackInstallDone.value
  if (currentStage.value === 9) {
    // AI is mandatory: block on 'none', require Ollama setup for local install
    if (form.llm_provider === 'none') return false
    if (form.llm_provider === 'ollama' && form.ollama_server === 'local') {
      return ollamaSetupJob.value?.ok === true
    }
    return true
  }
  return true
})

// Human-readable explanation of why Continue is blocked — shown below the button
const advanceBlockReason = computed((): string => {
  if (canAdvance.value) return ''
  if (currentStage.value === 0) {
    if (prereqChecks.value.length === 0) return 'Run prerequisite checks above before continuing'
    return 'Fix the failing prerequisite checks before continuing'
  }
  if (currentStage.value === 1) return 'Enter your domain name to continue'
  if (currentStage.value === 2) return 'ZeroSSL requires both EAB Key ID and EAB HMAC Key'
  if (currentStage.value === 5) {
    const missing = requiredSecrets.value.filter(
      s => s.required && !s.auto_generated && !form.secrets[s.key]?.trim()
    )
    if (missing.length === 1) return 'Required: ' + missing[0].label
    if (missing.length > 1) return missing.length + ' required fields are empty: ' + missing.map(s => s.label).join(', ')
    return ''
  }
  if (currentStage.value === 7) return 'Waiting for platform deployment to complete'
  if (currentStage.value === 8) return 'Waiting for app installations to complete'
  if (currentStage.value === 9) {
    if (form.llm_provider === 'none') return 'Select an AI provider in the Secrets step before continuing'
    if (form.llm_provider === 'ollama' && form.ollama_server === 'local') return 'Complete the Ollama install and model download above'
    return ''
  }
  return ''
})

// ── Prerequisites ─────────────────────────────────────────────────────────
const prereqChecks = ref<{key:string; label:string; status:string; value?:string; detail?:string}[]>([])
const prereqRunning = ref(false)
const prereqError = ref('')

async function runPrereqChecks() {
  prereqRunning.value = true
  prereqError.value = ''
  try {
    const data = await platformApi.prereqs(true)
    prereqChecks.value = data.checks || []
    // Auto-fill Stage 1 values from real system data regardless of check pass/fail
    // (some checks may warn but values are still valid)
    const sys = data.system || {}
    if (sys.puid) form.puid = sys.puid
    if (sys.pgid) form.pgid = sys.pgid
    if (sys.timezone) form.timezone = sys.timezone
    if (sys.config_root) form.config_root = sys.config_root
    systemProfile.value = {
      ...(systemProfile.value || {}),
      server_ip: sys.server_ip,
      ram_gb: sys.ram_gb,
      recommended_model: sys.recommended_model,
      available_models: sys.available_models || [],
      llm_warning: sys.llm_warning,
      gpu_name: sys.gpu_name,
      gpu_vram_gb: sys.gpu_vram_gb,
      puid_username: sys.puid_username,
      ollama_running: sys.ollama_running || false,
      ollama_models: sys.ollama_models || [],
      ollama_recommended_loaded: sys.ollama_recommended_loaded || false,
    }
    // Auto-select recommended model
    if (sys.recommended_model) form.ollama_model = sys.recommended_model
    // Prerequisite results remain on screen; user clicks Continue → to advance.
    // (canAdvance already enables the button when all checks pass.)
  } catch (e) {
    prereqError.value = String(e)
  } finally {
    prereqRunning.value = false
  }
}

// ── System profile for LLM stage ─────────────────────────────────────────
const systemProfile = ref<any>(null)

// ── Secrets derivation ────────────────────────────────────────────────────
function hex(len = 32) {
  return Array.from(crypto.getRandomValues(new Uint8Array(len)))
    .map(b => b.toString(16).padStart(2, '0')).join('')
}

const requiredSecrets = computed(() => {
  const secrets: any[] = []

  // DNS provider credentials — driven from effectiveDnsProviders so all providers are covered
  const dnsInfo = effectiveDnsProviders.value[form.dns_provider]
  if (dnsInfo) {
    for (const varKey of dnsInfo.vars) {
      secrets.push({
        key: varKey,
        label: `${dnsInfo.label}: ${varKey.replace(/_/g, ' ')}`,
        required: true,
        link: dnsInfo.link,
        placeholder: varKey.toLowerCase().includes('password') ? '••••••••' : `${varKey}…`,
        type: varKey.toLowerCase().includes('password') ? 'password' : 'text',
        note: secrets.length === 0 ? `Get credentials at ${dnsInfo.link}` : undefined,
      })
    }
  }
  // Tunnels are multi-select stored in form.tunnels (not form.infra.tunnel)
  if (form.tunnels.includes('cloudflared')) {
    secrets.push({ key: 'CF_TUNNEL_TOKEN', label: 'Cloudflare Tunnel Token', required: true,
      link: 'https://one.dash.cloudflare.com/', placeholder: 'eyJhbGci… (long base64 token)',
      note: 'one.dash.cloudflare.com → Networks → Tunnels → Create/select tunnel → Overview → copy the token string shown in the cloudflared install command.' })
  }
  if (form.tunnels.includes('tailscale')) {
    secrets.push({ key: 'TAILSCALE_AUTH_KEY', label: 'Tailscale Auth Key', required: true,
      link: 'https://login.tailscale.com/admin/authkeys', placeholder: 'tskey-auth-…' })
  }
  if (form.tunnels.includes('headscale')) {
    secrets.push({ key: 'HEADSCALE_AUTH_KEY', label: 'Headscale Pre-auth Key', required: true,
      link: 'https://headscale.net/docs/ref/preauthkeys/', placeholder: 'xxxx',
      note: 'Generate with: headscale preauthkeys create --reusable' })
  }
  if (form.tunnels.includes('netbird')) {
    secrets.push({ key: 'NETBIRD_SETUP_KEY', label: 'NetBird Setup Key', required: true,
      link: 'https://app.netbird.io/', placeholder: 'setup-key-…' })
  }
  if (form.tunnels.includes('zerotier')) {
    secrets.push({ key: 'ZEROTIER_NETWORK_ID', label: 'ZeroTier Network ID', required: true,
      placeholder: '8056c2e21c000001', note: 'Network ID from your ZeroTier controller.' })
  }
  if (form.tunnels.includes('pangolin')) {
    secrets.push({ key: 'PANGOLIN_CONTROLLER_URL', label: 'Pangolin Controller URL', required: true,
      placeholder: 'https://pangolin.example.com' })
    secrets.push({ key: 'PANGOLIN_ENROLLMENT_TOKEN', label: 'Pangolin Enrollment Token', required: true,
      type: 'password', placeholder: 'pangolin-token-…' })
  }
  if (form.tunnels.includes('nebula')) {
    secrets.push({ key: 'NEBULA_CONFIG_YAML', label: 'Nebula config.yml', required: true,
      type: 'password', placeholder: 'Paste config YAML', note: 'Paste the full node config YAML.' })
    secrets.push({ key: 'NEBULA_CA_CRT', label: 'Nebula CA Certificate', required: true,
      type: 'password', placeholder: '-----BEGIN CERTIFICATE-----' })
    secrets.push({ key: 'NEBULA_HOST_CRT', label: 'Nebula Host Certificate', required: true,
      type: 'password', placeholder: '-----BEGIN NEBULA CERTIFICATE-----' })
    secrets.push({ key: 'NEBULA_HOST_KEY', label: 'Nebula Host Private Key', required: true,
      type: 'password', placeholder: '-----BEGIN PRIVATE KEY-----' })
  }
  // Gluetun VPN credentials — protocol choice determines which fields are needed
  if (form.infra.vpn === 'gluetun') {
    secrets.push({ key: 'VPN_SERVICE_PROVIDER', label: 'VPN Provider', required: true,
      type: 'text', placeholder: '',
      note: 'Provider name: mullvad, nordvpn, surfshark, expressvpn, protonvpn, pia, etc.' })
    secrets.push({ key: 'VPN_TYPE', label: 'VPN Protocol', required: true,
      type: 'select', options: ['wireguard', 'openvpn'],
      note: 'Use WireGuard for Mullvad and most modern providers. OpenVPN for older setups.' })
    secrets.push({ key: 'OPENVPN_USER', label: 'VPN Username / Account number', required: false,
      type: 'text', placeholder: 'Required for OpenVPN. For Mullvad: your account number.',
      note: 'Leave blank if using WireGuard.' })
    secrets.push({ key: 'OPENVPN_PASSWORD', label: 'VPN Password', required: false,
      placeholder: 'Not required for Mullvad. Required for most other OpenVPN providers.',
      note: 'Leave blank for Mullvad or WireGuard.' })
    secrets.push({ key: 'WIREGUARD_PRIVATE_KEY', label: 'WireGuard Private Key', required: form.secrets['VPN_TYPE'] === 'wireguard',
      type: 'text', placeholder: 'base64-encoded key from your VPN provider',
      note: 'Required only for WireGuard protocol. Get from your provider dashboard.' })
    secrets.push({ key: 'SERVER_COUNTRIES', label: 'Server Country (optional)', required: false,
      type: 'text', placeholder: '',
      note: 'Preferred exit country. Leave blank for automatic selection.' })
    secrets.push({ key: 'SERVER_CITIES', label: 'Server City (optional)', required: false,
      type: 'text', placeholder: '',
      note: 'Preferred exit city within the selected country. Leave blank for automatic selection.' })
  }
  // Komodo requires JWT secret and passkey (auto-generated)
  if (form.infra.management === 'komodo') {
    secrets.push({ key: 'KOMODO_JWT_SECRET', label: 'Komodo JWT Secret', required: true,
      auto_generated: true, note: 'Auto-generated. Required for Komodo Core authentication.' })
    secrets.push({ key: 'KOMODO_PASSKEY', label: 'Komodo Passkey', required: true,
      auto_generated: true, note: 'Auto-generated. Core↔Periphery auth key.' })
  }
  if (form.infra.auth === 'tinyauth') {
    secrets.push({ key: 'TINYAUTH_SECRET', label: 'TinyAuth Session Secret', required: true,
      auto_generated: true, note: 'Auto-generated signing key. Do not share.' })
    // Username and password for TinyAuth login
    secrets.push({ key: 'TINYAUTH_USERNAME', label: 'Login username (protects all apps)', required: true,
      placeholder: 'admin', type: 'text',
      note: 'You will use this username and password to access every app that TinyAuth protects.' })
    secrets.push({ key: 'TINYAUTH_PASSWORD', label: 'Login password (protects all apps)', required: true,
      placeholder: 'Choose a strong password',
      note: 'You will use this password to access all your self-hosted apps. This will be bcrypt-hashed.' })
  }
  if (form.infra.auth === 'authelia') {
    secrets.push({ key: 'AUTHELIA_JWT_SECRET', label: 'Authelia JWT Secret', required: true,
      auto_generated: true, note: 'Auto-generated. Change only if migrating existing install.' })
    secrets.push({ key: 'AUTHELIA_SESSION_SECRET', label: 'Authelia Session Secret', required: true,
      auto_generated: true })
  }
  if (form.infra.auth === 'authentik') {
    secrets.push({ key: 'AUTHENTIK_SECRET_KEY', label: 'Authentik Secret Key', required: true,
      auto_generated: true, note: 'Auto-generated. Required for Authentik server.' })
    secrets.push({ key: 'AUTHENTIK_POSTGRES_PASSWORD', label: 'Authentik PostgreSQL Password', required: true,
      auto_generated: true, note: 'Password for the PostgreSQL database.' })
    secrets.push({ key: 'AUTHENTIK_EMAIL', label: 'Admin Email', required: true,
      placeholder: 'admin@example.com', type: 'email',
      note: 'Email address for the initial admin account.' })
    secrets.push({ key: 'AUTHENTIK_PASSWORD', label: 'Admin Password', required: true,
      type: 'password',
      note: 'Password for the initial admin account.' })
  }
  if (form.infra.auth === 'oauth2-proxy') {
    secrets.push({ key: 'OAUTH2_PROXY_PROVIDER', label: 'OAuth Provider', required: true,
      placeholder: 'google', note: 'google, github, azure, or oidc' })
    secrets.push({ key: 'OAUTH2_PROXY_CLIENT_ID', label: 'Client ID', required: true,
      note: 'OAuth2 client ID from your provider.' })
    secrets.push({ key: 'OAUTH2_PROXY_CLIENT_SECRET', label: 'Client Secret', required: true,
      type: 'password', note: 'OAuth2 client secret from your provider.' })
    secrets.push({ key: 'OAUTH2_PROXY_OIDC_ISSUER_URL', label: 'OIDC Issuer URL', required: false,
      placeholder: 'https://accounts.google.com',
      note: 'Required for OIDC provider.' })
    secrets.push({ key: 'OAUTH2_PROXY_COOKIE_SECRET', label: 'Cookie Secret', required: true,
      auto_generated: true, note: 'Auto-generated. Used for cookie encryption.' })
    secrets.push({ key: 'OAUTH2_PROXY_EMAIL_DOMAINS', label: 'Allowed Email Domains', required: false,
      placeholder: 'example.com,example.org',
      note: 'Comma-separated. Blank = any email allowed.' })
  }
  secrets.push({ key: 'POSTGRES_PASSWORD', label: 'PostgreSQL Password', required: true,
    auto_generated: true, note: 'Used by Immich, Paperless-ngx, Authelia if selected.' })

  return secrets
})

const secretVisible = reactive<Record<string, boolean>>({})

// ── Catalog search for individual app selection (Stage 4) ─────────────────
const catalogSearchResults = computed(() => {
  if (!appSearch.value || appSearch.value.length < 2) return []
  const q = appSearch.value.toLowerCase()
  return catalogApps.value
    .filter((a: any) => a.key.includes(q) || a.display_name.toLowerCase().includes(q))
    .slice(0, 8)
})

function toggleIndividualApp(key: string) {
  const idx = form.individualApps.indexOf(key)
  if (idx >= 0) form.individualApps.splice(idx, 1)
  else form.individualApps.push(key)
}

// ── Auto-scroll current stage into view ───────────────────────────────────
import { watch, nextTick } from 'vue'
watch(currentStage, async () => {
  await nextTick()
  if (activeStageBtn.value && stageNavRef.value) {
    activeStageBtn.value.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
  }
})
// ── Auto-initialize secrets when infra choices change ──────────────────────
watch(
  () => [form.infra.vpn, form.infra.management, form.infra.auth],
  ([vpn, management, auth]) => {
    if (vpn === 'gluetun' && !form.secrets['VPN_TYPE']) form.secrets['VPN_TYPE'] = 'wireguard'
    if (management === 'komodo') {
      if (!form.secrets['KOMODO_JWT_SECRET']) form.secrets['KOMODO_JWT_SECRET'] = hex(32)
      if (!form.secrets['KOMODO_PASSKEY'])    form.secrets['KOMODO_PASSKEY']    = hex(16)
    }
    if (auth === 'tinyauth' && !form.secrets['TINYAUTH_SECRET']) form.secrets['TINYAUTH_SECRET'] = hex(32)
    if (auth === 'authelia') {
      if (!form.secrets['AUTHELIA_JWT_SECRET']) form.secrets['AUTHELIA_JWT_SECRET'] = hex(32)
      if (!form.secrets['AUTHELIA_SESSION_SECRET']) form.secrets['AUTHELIA_SESSION_SECRET'] = hex(32)
    }
    if (auth === 'authentik') {
      if (!form.secrets['AUTHENTIK_SECRET_KEY']) form.secrets['AUTHENTIK_SECRET_KEY'] = hex(32)
      if (!form.secrets['AUTHENTIK_POSTGRES_PASSWORD']) form.secrets['AUTHENTIK_POSTGRES_PASSWORD'] = hex(16)
      if (!form.secrets['AUTHENTIK_EMAIL']) form.secrets['AUTHENTIK_EMAIL'] = 'admin@example.com'
      if (!form.secrets['AUTHENTIK_PASSWORD']) form.secrets['AUTHENTIK_PASSWORD'] = hex(12)
    }
    if (auth === 'oauth2-proxy') {
      if (!form.secrets['OAUTH2_PROXY_PROVIDER']) form.secrets['OAUTH2_PROXY_PROVIDER'] = 'google'
      if (!form.secrets['OAUTH2_PROXY_COOKIE_SECRET']) form.secrets['OAUTH2_PROXY_COOKIE_SECRET'] = hex(32)
    }
    if (!form.secrets['POSTGRES_PASSWORD']) form.secrets['POSTGRES_PASSWORD'] = hex(16)
  },
  { immediate: true }
)


// ── Wizard draft persistence (localStorage) ───────────────────────────────
// Saves form + stage on every change so a refresh or sidebar nav restores
// the user to where they were. Cleared automatically on successful deploy.
const WIZARD_DRAFT_KEY = 'slop-wizard-draft'
const hasDraft = ref(false)

// Config-only secrets keys safe to persist across sessions — these are
// user selections (provider name, protocol, location), never credentials.
const _NON_SECRET_CONFIG_KEYS = new Set([
  'VPN_SERVICE_PROVIDER', 'VPN_TYPE', 'SERVER_COUNTRIES', 'SERVER_CITIES',
])

function saveDraft() {
  try {
    const safeSecrets: Record<string, string> = {}
    for (const key of _NON_SECRET_CONFIG_KEYS) {
      const val = form.secrets[key]
      if (val !== undefined && val !== null) safeSecrets[key] = val
    }
    localStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify({
      form: { ...JSON.parse(JSON.stringify(form)), secrets: safeSecrets },
      stage: currentStage.value,
      maxStage: maxReachedStage.value,
    }))
  } catch { /* intentional: non-fatal */ }
}

function clearDraft() {
  try { localStorage.removeItem(WIZARD_DRAFT_KEY) } catch { /* intentional: non-fatal */ }
  hasDraft.value = false
}

function clearDraftAndReset() {
  clearDraft()
  // Reload so the component starts fresh with no draft
  window.location.reload()
}

function restoreDraft() {
  try {
    const raw = localStorage.getItem(WIZARD_DRAFT_KEY)
    if (!raw) return
    const draft = JSON.parse(raw)
    if (draft.form && typeof draft.form === 'object') {
      Object.assign(form, draft.form)
    }
    if (draft.stage != null && Number.isFinite(draft.stage)) {
      currentStage.value = draft.stage
    }
    if (draft.maxStage != null && Number.isFinite(draft.maxStage)) {
      maxReachedStage.value = draft.maxStage
    }
    hasDraft.value = true
  } catch { /* intentional: non-fatal */ }
}

watch(form, saveDraft, { deep: true })
watch(maxReachedStage, saveDraft)

function regenerateSecret(key: string) {
  form.secrets[key] = hex(32)
}

// ── Infra toggles ─────────────────────────────────────────────────────────
function toggleStack(id: string) {
  const idx = form.selectedStacks.indexOf(id)
  if (idx >= 0) form.selectedStacks.splice(idx, 1)
  else form.selectedStacks.push(id)
}

// ── App deploy tracking ───────────────────────────────────────────────────
const appInstallStatus = reactive<Record<string, string>>({})
const appInstallProgress = ref<Record<string, string>>({})
const appInstallSteps = ref<Record<string, any[]>>({})   // per-app step log
const secretsValidating = ref(false)
const secretsValidationResult = ref<any>(null)
const appInstallError = reactive<Record<string, string>>({})
const stackAppsToInstall = computed(() => {
  const apps: string[] = []
  for (const stackId of form.selectedStacks) {
    const stack = allStacks.value.find(s => s.id === stackId)
    if (stack) apps.push(...(stack.app_keys as string[]))
  }
  return [...new Set([...apps, ...form.individualApps])]
})

// Apps among selected that declare install_prompts — used in Stage 5 to collect values.
const appsWithInstallPrompts = computed(() => {
  return stackAppsToInstall.value
    .map(key => {
      const app = catalogApps.value.find((a: any) => a.key === key)
      const prompts: any[] = (app?.install_prompts ?? [])
      return { key, display_name: app?.display_name ?? key, prompts }
    })
    .filter(a => a.prompts.length > 0)
})

// Progress bar counts for Stage 8 install header
const installTotalCount = computed(() => stackAppsToInstall.value.length)
const installDoneCount = computed(() =>
  stackAppsToInstall.value.filter(k => appInstallStatus[k] === 'ok').length
)

// ── Platform wizard deploy ────────────────────────────────────────────────
const running = ref(false)
const stackInstallDone = ref(false)
const installStarted = ref(false)
const appSearch = ref('')
const catalogApps = ref<any[]>([])
// Fallback timezone list — used when the API call fails so the datalist is never empty.
// Kept in sync with the backend's fallback subset (platform.py list_timezones).
const TIMEZONES_FALLBACK: string[] = [
  "Africa/Johannesburg", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "America/New_York", "America/Sao_Paulo", "Asia/Kolkata", "Asia/Seoul", "Asia/Shanghai",
  "Asia/Tokyo", "Australia/Melbourne", "Australia/Sydney", "Europe/Amsterdam",
  "Europe/Berlin", "Europe/London", "Europe/Paris", "Europe/Rome", "Pacific/Auckland",
  "UTC", "Etc/UTC",
]
const timezones = ref<string[]>([])
const effectiveTimezones = computed(() => {
  const list = timezones.value.length > 0 ? timezones.value : TIMEZONES_FALLBACK
  return list.slice(0, 200)
})
const timezoneFilter = ref('')
const filteredTimezones = computed(() => {
  const q = timezoneFilter.value.toLowerCase()
  if (q.length < 2) return effectiveTimezones.value.slice(0, 30)
  const hits = effectiveTimezones.value.filter(tz => tz.toLowerCase().includes(q.replace(' ', '_')))
  return hits.slice(0, 30)
})
function selectTimezone(tz: string) {
  form.timezone = tz
  timezoneFilter.value = tz
}
const activeStageBtn = ref<HTMLElement | null>(null)
const stageNavRef = ref<HTMLElement | null>(null)
const setupError = ref<string | null>(null)
const setupSuccess = ref(false)
const stepResults = ref<any[]>([])
const showReset = ref(false)
const certStatus = ref<{cert_found: boolean; message: string} | null>(null)
let certPollInterval: ReturnType<typeof setInterval> | null = null

async function pollCertStatus() {
  try {
    certStatus.value = await platformApi.certStatus()
    if (certStatus.value?.cert_found && certPollInterval) {
      clearInterval(certPollInterval)
      certPollInterval = null
    }
  } catch { /* intentional: non-fatal */ }
}

async function runWizard() {
  running.value = true
  setupError.value = null
  setupSuccess.value = false
  stepResults.value = []

  // If platform is already marked ready from a previous run, reset first
  // so the wizard can re-run (user clicked Deploy from the setup flow)
  try {
    const statusData = await platformApi.status()
    if (statusData.status === 'ready') {
      // Route through the typed client so the required ?confirm=RESET_PLATFORM
      // token is always sent (raw fetch omitted it → silent 400 in production).
      await platformApi.reset()
      await new Promise(r => setTimeout(r, 500))
    }
  } catch { /* intentional: non-fatal */ }

  try {
    const payload = {
      domain: form.domain,
      config_root: form.config_root,
      media_root: form.media_root,
      puid: form.puid,
      pgid: form.pgid,
      timezone: form.timezone,
      cert_resolver: form.cert_resolver,
      acme_email: form.acme_email || `admin@${form.domain}`,
      dns_provider: form.dns_provider,
      eab_kid: form.eab_kid,
      eab_hmac: form.eab_hmac,
      ntfy_url: form.ntfy_url,
      ntfy_topic: form.ntfy_topic,
      ntfy_enabled: form.ntfy_enabled,
      secrets: form.secrets,
      arr_auth_bypass: form.arr_auth_bypass,
      arr_auth_provider: form.infra.auth,
      infra_selections: { ...form.infra, tunnels: form.tunnels },
      selected_stacks: form.selectedStacks,
    }
    // Start async wizard job — backend runs steps in background thread
    // eslint-disable-next-line no-restricted-syntax -- raw-response: body err.detail + status, returns without polling
    const startRes = await fetch('/api/v1/platform/wizard/run-async', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!startRes.ok) {
      const err = await startRes.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${startRes.status}`)
    }
    const { job_id } = await startRes.json()

    // Poll for step results every 500ms
    const result = await new Promise<any>((resolve, reject) => {
      const poll = setInterval(async () => {
        try {
          const { data: status } = await platformApi.wizardStatusRaw(job_id)
          // Update steps in real time as they come in
          stepResults.value = status.steps || []
          if (status.done) {
            clearInterval(poll)
            resolve(status)
          }
        } catch (e) {
          clearInterval(poll)
          reject(e)
        }
      }, 500)
    })
    // result.steps already set via polling

    if (result.platform_ready) {
      setupSuccess.value = true
      clearDraft()  // wizard complete — no need to restore on next visit
      forceSetup.value = true  // prevent isReady from switching to configured screen
      // Start polling cert status
      pollCertStatus()
      certPollInterval = setInterval(pollCertStatus, 10000) // every 10s
      await platformStore.fetchStatus()
      // Stage 8 is reached manually via Continue — don't auto-advance
    } else {
      // Find the failed step for context
      const failedStep = result.steps?.find((s: any) => s.status === 'error')
      const errMsg = result.error ?? failedStep?.message ?? 'Setup did not complete.'
      const errDetail = failedStep?.detail ? ` — ${failedStep.detail}` : ''
      setupError.value = errMsg + errDetail
    }
  } catch (e) {
    setupError.value = e instanceof Error ? e.message : String(e)
  } finally {
    running.value = false
  }
}


// App display info for Stage 8 install view
const catalogAppInfo = computed(() => {
  // Built-in fallbacks for quick stack apps
  const defaults: Record<string, {name: string, icon: string, category: string}> = {
    sonarr:      { name: 'Sonarr',        icon: '📺', category: 'arr' },
    radarr:      { name: 'Radarr',        icon: '🎬', category: 'arr' },
    prowlarr:    { name: 'Prowlarr',      icon: '🔍', category: 'arr' },
    sabnzbd:     { name: 'SABnzbd',       icon: '⬇️', category: 'downloader' },
    decypharr:   { name: 'Decypharr',     icon: '⚡', category: 'arr' },
    zilean:      { name: 'Zilean',        icon: '🕐', category: 'arr' },
    dumb:        { name: 'DUMB',          icon: '🌐', category: 'arr' },
    jellyfin:    { name: 'Jellyfin',      icon: '🎵', category: 'media' },
    seerr:       { name: 'Jellyseerr',    icon: '🔎', category: 'media' },
    immich:      { name: 'Immich',        icon: '📸', category: 'photos' },
    dozzle:      { name: 'Dozzle',        icon: '🪵', category: 'monitoring' },
    beszel:      { name: 'Beszel',        icon: '📊', category: 'monitoring' },
    scrutiny:    { name: 'Scrutiny',      icon: '💽', category: 'monitoring' },
    vaultwarden: { name: 'Vaultwarden',   icon: '🔐', category: 'productivity' },
    paperless_ngx:{ name: 'Paperless',   icon: '📄', category: 'productivity' },
    mealie:      { name: 'Mealie',        icon: '🍳', category: 'productivity' },
    ollama:      { name: 'Ollama',        icon: '🤖', category: 'ai' },
  }
  return { ...defaults }
})

const APP_KEY_DISPLAY: Record<string, string> = {
  dumb: 'DUMB',
}
function displayAppKey(key: string): string {
  return APP_KEY_DISPLAY[key] ?? key
}

function appDisplayName(key: string): string {
  return catalogAppInfo.value[key]?.name
    || (catalogApps.value.find((a: any) => a.key === key) as any)?.display_name
    || key.charAt(0).toUpperCase() + key.slice(1)
}
function appIcon(key: string): string {
  return catalogAppInfo.value[key]?.icon || '📦'
}

async function installStacks() {
  installStarted.value = true
  stackInstallDone.value = false
  appInstallProgress.value = {}
  appInstallSteps.value = {}

  const data = await platformApi.stackAppKeys(form.selectedStacks)
  const keys: string[] = [...new Set([...(data.keys || []), ...form.individualApps])]

  if (keys.length === 0) {
    stackInstallDone.value = true
    return
  }

  for (const key of keys) appInstallStatus[key] = 'queued'

  // Start all installs concurrently — fire-and-forget via the per-app install endpoint.
  // The backend semaphore caps actual Docker concurrency at 6 regardless of how many
  // requests are sent.
  await Promise.all(keys.map(async (key) => {
    appInstallStatus[key] = 'running'
    appInstallProgress.value[key] = 'Starting…'
    appInstallSteps.value[key] = []
    // Thread install_prompts values into the request body (id=816)
    const userVolumePaths = form.installPromptValues[key] ?? {}
      try {
        await appsApi.install(key, Object.keys(userVolumePaths).length > 0 ? { user_volume_paths: userVolumePaths } : {})
      } catch (e) {
        appInstallStatus[key] = 'error'
        appInstallError[key] = e instanceof Error ? e.message : String(e)
      }
  }))

  // Poll the bulk progress endpoint to update all app statuses from a single request.
  // Falls back to N individual EventSource connections if the bulk endpoint returns 404
  // (backward compatibility with older backend versions).
  const probeRes = await appsApi.installsProgressRaw().catch(() => null)
  if (!probeRes || probeRes.status === 404) {
    // Fallback: N EventSource connections (one per app)
    await _installStacksViaEventSources(keys)
    stackInstallDone.value = true
    return
  }

  // Bulk-poll path: single setInterval polling /installs/progress
  await new Promise<void>((resolve) => {
    const deadline = Date.now() + 600_000 // 10-minute overall timeout
    let pollFailureCount = 0
    const poll = setInterval(async () => {
      if (Date.now() > deadline) {
        clearInterval(poll)
        // Mark any still-running apps as timed out
        for (const key of keys) {
          if (appInstallStatus[key] === 'running' || appInstallStatus[key] === 'queued') {
            appInstallStatus[key] = 'error'
            appInstallError[key] = 'Timed out waiting for install'
          }
        }
        resolve()
        return
      }

      try {
        const pr = await appsApi.installsProgressRaw()
        if (!pr.ok) return
        pollFailureCount = 0  // reset on success
        const appsData: Record<string, any> = pr.data?.apps ?? {}

        for (const key of keys) {
          const info = appsData[key]
          if (!info) continue
          // Update step log
          if (info.steps?.length) {
            appInstallSteps.value[key] = info.steps
            const runningStep = info.steps.filter((s: any) => s.status !== 'skipped').slice(-1)[0]
            if (runningStep && appInstallStatus[key] === 'running') {
              appInstallProgress.value[key] = runningStep.message || runningStep.step
            }
          }
          if (info.done) {
            if (info.ok) {
              appInstallStatus[key] = 'ok'
              appInstallProgress.value[key] = ''
            } else {
              // Health-timeout race: check if container is actually running
              let resolved = false
              try {
                const hd = await healthApi.containerStatus(key)
                if (hd.ready) {
                  appInstallStatus[key] = 'ok'
                  appInstallProgress.value[key] = 'Running (health check passed)'
                  resolved = true
                }
              } catch { /* intentional: non-fatal */ }
              if (!resolved) {
                appInstallStatus[key] = 'error'
                appInstallError[key] = info.error || 'Install failed'
                // Keep progress + steps visible so user sees where it died
              }
            }
          }
        }

        // Stop when all target keys have reached a terminal state
        const allDone = keys.every(
          k => appInstallStatus[k] === 'ok' || appInstallStatus[k] === 'error'
        )
        if (allDone) {
          clearInterval(poll)
          resolve()
        }
      } catch (err) {
        pollFailureCount++
        console.error(`Install progress poll failure #${pollFailureCount}:`, err)
        if (pollFailureCount >= 3) {
          clearInterval(poll)
          toast.error('Installation monitoring lost', `Monitoring stopped after 3 consecutive poll failures. Check app status manually.`, 8000)
          resolve()
        }
      }
    }, 1000)
  })

  stackInstallDone.value = true
}

// Fallback: N EventSource connections — used only when bulk endpoint returns 404.
// Keep this path for backward compat with older backend versions.
async function _installStacksViaEventSources(keys: string[]) {
  async function installOne(key: string) {
    if (appInstallStatus[key] === 'error') return // already failed at POST stage

    await new Promise<void>((resolve) => {
      const handleDone = async (ok: boolean, error: string) => {
        if (ok) {
          appInstallStatus[key] = 'ok'
          appInstallProgress.value[key] = ''
        } else {
          try {
            const hd = await healthApi.containerStatus(key)
            if (hd.ready) {
              appInstallStatus[key] = 'ok'
              appInstallProgress.value[key] = 'Running (health check passed)'
              resolve()
              return
            }
          } catch { /* intentional: non-fatal */ }
          appInstallStatus[key] = 'error'
          appInstallError[key] = error || 'Install failed'
          // Keep progress + steps visible so user sees where it died
        }
        resolve()
      }

      const es = new EventSource(`/api/v1/apps/${key}/install/stream`)
      es.onmessage = (event) => {
        const step = JSON.parse(event.data)
        if (step.step === '__done__') {
          es.close()
          handleDone(step.status === 'ok', step.message || '')
        } else {
          const steps = appInstallSteps.value[key] || []
          appInstallSteps.value[key] = [...steps.filter((s: any) => s.step !== step.step), step]
          const runningStep = appInstallSteps.value[key].filter((s: any) => s.status !== 'skipped').slice(-1)[0]
          if (runningStep) appInstallProgress.value[key] = runningStep.message || runningStep.step
        }
      }
      es.onerror = () => {
        es.close()
        const poll = setInterval(async () => {
          try {
            const { data: pdata } = await appsApi.installProgressRaw(key)
            const steps: any[] = pdata.steps || []
            appInstallSteps.value[key] = steps
            const runningStep = steps.filter((s: any) => s.status !== 'skipped').slice(-1)[0]
            if (runningStep) appInstallProgress.value[key] = runningStep.message || runningStep.step
            if (pdata.done) {
              clearInterval(poll)
              await handleDone(pdata.ok as boolean, pdata.error || '')
            }
          } catch (e) {
            clearInterval(poll)
            appInstallStatus[key] = 'error'
            appInstallError[key] = String(e)
            resolve()
          }
        }, 500)
      }
    })
  }

  await Promise.all(keys.map(installOne))
}

// ── Stage 9: Ollama auto-setup ─────────────────────────────────────────
const ollamaSetupJobId = ref<string | null>(null)
const ollamaSetupJob = ref<any>(null)
let ollamaSetupPoll: ReturnType<typeof setInterval> | null = null
const liveOllamaModels = ref<string[] | null>(null)
const ollamaFetchDone = ref(false)

// Model metadata — kept in sync with backend LLM_MODEL_RAM_MB
const ALL_OLLAMA_MODELS = [
  { value: 'smollm2:1.7b',  label: 'SmolLM2 1.7B (1.1GB)',  ram: 1.2, desc: 'fastest, minimal RAM' },
  { value: 'qwen2.5:3b',    label: 'Qwen 2.5 3B (1.9GB)',    ram: 2.2, desc: 'good balance' },
  { value: 'llama3.2:3b',   label: 'Llama 3.2 3B (2.0GB)',   ram: 2.5, desc: 'strong reasoning' },
  { value: 'phi4-mini',     label: 'Phi-4 Mini (2.5GB)',      ram: 3.0, desc: 'fits ≥8GB RAM' },
  { value: 'llama3.1:8b',   label: 'Llama 3.1 8B (4.7GB)',   ram: 5.0, desc: 'best quality, needs ≥12GB RAM or GPU' },
]

const ollamaModelOptions = computed(() => {
  // Use backend available_models list when present (accounts for GPU, bandwidth, stack)
  const backendModels = systemProfile.value?.available_models as string[] | undefined
  const recommended = systemProfile.value?.recommended_model || ''
  const live = liveOllamaModels.value

  // When live fetch succeeded: show installed models first, then static extras as "not installed"
  if (live !== null && live.length > 0) {
    const liveSet = new Set(live)
    const liveItems = live.map(name => ({
      value: name,
      label: `${name} — ✓ installed`,
      _recommended: name === recommended,
      _loaded: true,
      _approved: true,
    }))
    const staticExtra = ALL_OLLAMA_MODELS
      .filter(m => !liveSet.has(m.value))
      .map(m => ({
        ...m,
        label: `${m.label} — not installed`,
        _recommended: m.value === recommended,
        _loaded: false,
        _approved: !backendModels || backendModels.includes(m.value),
      }))
    return [...liveItems, ...staticExtra]
  }

  // Fallback: static list with backend-based filtering (Ollama not running or not yet fetched)
  const models = ALL_OLLAMA_MODELS.map(m => {
    const backendApproved = !backendModels || backendModels.includes(m.value)
    const isRecommended = m.value === recommended
    const alreadyLoaded = (systemProfile.value?.ollama_models || []).includes(m.value)

    let suffix = m.desc
    if (alreadyLoaded) suffix = '✓ already downloaded'
    else if (isRecommended) suffix = '★ Recommended for your hardware'
    else if (!backendApproved) suffix = `⚠ may not fit (recommended: ${recommended || 'smaller model'})`

    return {
      ...m,
      label: `${m.label} — ${suffix}`,
      _recommended: isRecommended,
      _loaded: alreadyLoaded,
      _approved: backendApproved,
    }
  })
  // Sort: already loaded first, then recommended, then fits, then too large
  return models.sort((a, b) => {
    if (a._loaded !== b._loaded) return a._loaded ? -1 : 1
    if (a._recommended !== b._recommended) return a._recommended ? -1 : 1
    if (a._approved !== b._approved) return a._approved ? -1 : 1
    return 0
  })
})

async function startOllamaSetup() {
  ollamaSetupJob.value = { phase: 'starting', progress: 0, message: 'Starting…', done: false, ok: false }
  try {
    const d = await platformApi.setupOllama(form.ollama_model)
    ollamaSetupJobId.value = d.job_id
    // Poll for progress
    if (ollamaSetupPoll) clearInterval(ollamaSetupPoll)
    ollamaSetupPoll = setInterval(async () => {
      try {
        const status = await platformApi.ollamaStatus(d.job_id)
        ollamaSetupJob.value = status
        if (status.done) {
          clearInterval(ollamaSetupPoll!)
          ollamaSetupPoll = null
          // Trigger a health cycle so the AI agent activates immediately
          // instead of waiting for the next scheduled cycle.
          if (status.ok) {
            try {
              await fetch('/api/v1/health/run', { method: 'POST' })
            } catch { /* intentional: non-fatal background fire-and-forget */ }
          }
        }
      } catch { /* intentional: non-fatal */ }
    }, 1500)
  } catch (e) {
    ollamaSetupJob.value = { phase: 'error', progress: 0, done: true, ok: false,
      message: `Failed to start: ${e}` }
  }
}

// ── Stage 9: Ollama model size + auto-start ──────────────────────────────
const OLLAMA_MODEL_SIZE_GB: Record<string, number> = {
  'smollm2:1.7b': 1.1,
  'qwen2.5:3b': 1.9,
  'llama3.2:3b': 2.0,
  'phi4-mini': 2.5,
  'llama3.1:8b': 4.7,
  'gemma3:1b': 0.8,
  'llama3.2:1b': 1.3,
  'mistral:7b': 4.1,
}

const ollamaModelSizeInfo = computed((): string => {
  const size = OLLAMA_MODEL_SIZE_GB[form.ollama_model]
  if (!size) return ''
  return '~' + size + ' GB — est. ' + Math.round(size / 1.5) + ' min on 100 Mbps'
})

// Auto-start Ollama install when user selects Ollama + local at stage 9.
// Guard: only fires if no job is already running or completed.
watch(
  [() => currentStage.value, () => form.llm_provider, () => form.ollama_server],
  ([stage, provider, server]) => {
    if (stage === 9 && provider === 'ollama' && server === 'local'
        && !ollamaSetupJob.value && !ollamaSetupJobId.value) {
      startOllamaSetup()
    }
  },
  { immediate: true }
)

// Fetch live model list when Ollama is selected. Retries on each provider switch
// while liveOllamaModels is still null (covers the case where Ollama starts up later).
watch(() => form.llm_provider, async (provider) => {
  if (provider === 'ollama' && liveOllamaModels.value === null) {
    ollamaFetchDone.value = false
    try {
      const ollamaBase = form.ollama_url || 'http://localhost:11434'
      const data = await platformApi.ollamaModels(ollamaBase)
      liveOllamaModels.value = data.live ? data.models : null
    } catch { /* fall back to static list */ }
    ollamaFetchDone.value = true
  }
}, { immediate: false })

async function retryFailedApps() {
  const failed = stackAppsToInstall.value.filter(k => appInstallStatus[k] === 'error')
  stackInstallDone.value = false
  for (const key of failed) {
    appInstallStatus[key] = 'queued'
    appInstallError[key] = ''
    appInstallProgress.value[key] = ''
    appInstallSteps.value[key] = []
  }
  // Re-use the same per-app install logic as installStacks: start + poll
  async function retryOne(key: string) {
    appInstallStatus[key] = 'running'
    // Preserve previous step history so the user can see prior attempts;
    // clear only the live progress message.
    appInstallProgress.value[key] = 'Starting…'
    try {
      const r = await appsApi.installRaw(key, {}, true)
      if (!r.ok) {
        appInstallStatus[key] = 'error'
        appInstallError[key] = r.data?.detail || `HTTP ${r.status}`
        // Keep appInstallProgress and appInstallSteps visible so the user
        // can see exactly which step the prior attempt reached.
        return
      }
      await new Promise<void>((resolve) => {
        const poll = setInterval(async () => {
          try {
            const { data: pdata } = await appsApi.installProgressRaw(key)
            const steps: any[] = pdata.steps || []
            appInstallSteps.value[key] = steps
            const latest = steps.filter((s: any) => s.status !== 'skipped').slice(-1)[0]
            if (latest) appInstallProgress.value[key] = latest.message || latest.step
            if (pdata.done) {
              clearInterval(poll)
              if (pdata.ok) {
                appInstallStatus[key] = 'ok'
                appInstallProgress.value[key] = ''
              } else {
                appInstallStatus[key] = 'error'
                appInstallError[key] = pdata.error || 'Install failed'
                // Keep progress + steps visible on failure so user sees where it died
              }
              resolve()
            }
          } catch (e) {
            clearInterval(poll)
            appInstallStatus[key] = 'error'
            appInstallError[key] = String(e)
            // Keep progress + steps visible on failure
            resolve()
          }
        }, 800)
      })
    } catch (e) {
      appInstallStatus[key] = 'error'
      appInstallError[key] = String(e)
      // Keep progress + steps visible on failure
    }
  }
  await Promise.all(failed.map(retryOne))
  stackInstallDone.value = true
}

async function saveLLMConfig() {
  if (form.llm_provider === 'none') return
  // Single source of truth for the per-provider api key. Previously there
  // were two `api_key:` properties on the request body (the second
  // shadowed the first), and the first's `apiKey` fallback defaulted
  // unrelated providers to `form.groq_api_key`. Both bugs gone now —
  // every supported provider has its key explicitly named.
  const apiKey =
    form.llm_provider === 'cerebras' ? form.cerebras_api_key
    : form.llm_provider === 'openai'  ? form.openai_api_key
    : form.llm_provider === 'awan'    ? form.awan_api_key
    : form.llm_provider === 'groq'    ? form.groq_api_key
    : undefined
  try {
    await platformApi.saveLlm({
      provider: form.llm_provider,
      api_key: apiKey,
      model: form.llm_provider === 'ollama' ? form.ollama_model
           : form.llm_provider === 'llamacpp' ? form.llamacpp_model
           : form.llm_provider === 'groq' ? form.groq_model
           : form.llm_provider === 'awan' ? form.awan_model
           : form.llm_provider === 'cerebras' ? form.cerebras_model
           : form.llm_provider === 'openai' ? form.openai_model
           : undefined,
      llamacpp_url: form.llm_provider === 'llamacpp' ? form.llamacpp_url : undefined,
    })
  } catch { /* intentional: non-fatal */ }
}

async function doFullReset() {
  if (!confirm('FULL FACTORY RESET: This stops ALL containers, removes ALL compose fragments, and wipes the database. This cannot be undone. Continue?')) return
  showReset.value = false
  platformStore.clearStatus()  // sidebar clears instantly
  try {
    // Typed client sends the required ?confirm=DESTROY_ALL_DATA token
    // (raw fetch omitted it → silent 400 in production).
    await platformApi.resetFull()
    await platformStore.fetchStatus()
    forceSetup.value = true
    currentStage.value = 0
    maxReachedStage.value = 0
    form.domain = ''
    form.acme_email = ''
    form.eab_kid = ''
    form.eab_hmac = ''
    form.cert_resolver = 'staging'
    form.dns_provider = ''
    stepResults.value = []
    setupError.value = null
    setupSuccess.value = false
    prereqChecks.value = []
    await runPrereqChecks()
  } catch (e) {
    console.error('Full reset failed:', e)
  }
}

async function doReset() {
  showReset.value = false
  platformStore.clearStatus()  // sidebar clears instantly
  await platformApi.reset()
  await platformStore.fetchStatus()
  forceSetup.value = true
  currentStage.value = 0
  maxReachedStage.value = 0
  form.domain = ''
  form.acme_email = ''
  form.eab_kid = ''
  form.eab_hmac = ''
  form.cert_resolver = 'letsencrypt'
  form.dns_provider = ''
  stepResults.value = []
  setupError.value = null
  setupSuccess.value = false
  prereqChecks.value = []
  await runPrereqChecks()
}

async function loadStacks() {
  try {
    const d = await platformApi.stacks()
    allStacks.value = d.stacks || []
  } catch { /* intentional: non-fatal */ }
}

onMounted(async () => {
  restoreDraft()  // restore before prereq checks (may change currentStage)
  // Discard stale draft when platform is pending AND the draft points past
  // the Deploy stage (7), OR when platform is already ready (setup is done —
  // the draft is leftover from the completed session and the banner is confusing
  // on the 'Platform is configured' screen).
  if (hasDraft.value) {
    try {
      const status: any = await platformApi.status()
      if (status.status === 'ready' || (status.status === 'pending' && maxReachedStage.value >= 7)) {
        clearDraft()
        if (status.status === 'pending') {
          currentStage.value = 0
          maxReachedStage.value = 0
          form.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
        }
      }
    } catch { /* intentional: non-fatal */ }
  }
  // Pre-fill from current platform config when re-running wizard with no saved draft.
  // Only fires when platform is already ready (force=true re-run) and no draft exists,
  // so we don't overwrite a partially-completed wizard session.
  if (forceSetup.value && !hasDraft.value) {
    try {
      const d: any = await platformApi.status()
      if (d.domain)        form.domain        = d.domain
      if (d.config_root)   form.config_root   = d.config_root
      if (d.data_dir)      form.config_root   = d.data_dir   // fallback field name
      if (d.media_root)    form.media_root    = d.media_root
      if (d.puid != null)  form.puid          = d.puid
      if (d.pgid != null)  form.pgid          = d.pgid
      if (d.timezone)      form.timezone      = d.timezone
      if (d.cert_resolver) form.cert_resolver = d.cert_resolver
      // Secrets are not stored server-side — user must re-enter them
    } catch { /* intentional: non-fatal */ }
  }
  await loadStacks()
  // Load wizard supporting data unconditionally — needed by all stages regardless
  // of whether this is a fresh setup or a re-run via ?force=true from Settings.
  try {
    const d = await catalogApi.all()
    catalogApps.value = Object.values(d).flat()
  } catch { /* intentional: non-fatal */ }
  try {
    const d = await platformApi.timezones()
    timezones.value = d.timezones || []
  } catch (e) {
    /* non-fatal: fallback TIMEZONES_FALLBACK keeps the datalist usable */
    console.warn('Timezone list fetch failed — using fallback:', e)
  }
  try {
    const d: Array<{ key: string; name: string; env: string[] }> = await platformApi.dnsProviders()
    if (Array.isArray(d) && d.length > 0) {
      const providers: Record<string, { label: string; vars: string[]; link: string }> = {}
      for (const p of d) {
        providers[p.key] = {
          label: p.name,
          vars: p.env,
          link: DNS_PROVIDERS_FALLBACK[p.key]?.link ?? '',
        }
      }
      dnsProviders.value = providers
    }
  } catch { /* intentional: non-fatal */ }
  try {
    wizardLLMProviders.value = await healthApi.llmProviders()
  } catch { /* intentional: non-fatal */ }
  // Always auto-run prereq checks on mount when starting at Stage 0 — this
  // covers both fresh setup and forceSetup re-runs. If checks pass, the user
  // is advanced to Stage 1 automatically (see runPrereqChecks auto-advance logic).
  if (currentStage.value === 0) {
    await runPrereqChecks()
  }
  // Load system profile for AI stage recommendations
  try {
    const d: any = await settingsApi.system()
    systemProfile.value = {
      ram_gb: Math.round((d.total_ram_mb || 0) / 1024),
      recommended_model: d.recommended_llm_model || d.recommended_model || 'phi4-mini',
    }
  } catch { /* intentional: non-fatal */ }
})
</script>
