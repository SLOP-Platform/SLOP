<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <!-- Header: title + search + selection actions — all one row -->
    <div class="flex items-center gap-3 mb-3">
      <div class="shrink-0">
        <h1 class="page-title leading-none">
          Catalog
        </h1>
        <p class="text-xs text-slate-400 mt-0.5">
          {{ loading ? 'Loading…' : totalVisible + ' apps' }}
          <RouterLink
            to="/settings?tab=system"
            class="text-amber-600 hover:text-amber-700 ml-2"
          >
            + custom
          </RouterLink>
        </p>
      </div>
      <div class="relative flex-1 max-w-xs">
        <span class="absolute left-3 top-1/2 -translate-y-1/2 text-slate-300 text-sm pointer-events-none">⌕</span>
        <input
          v-model="search"
          type="text"
          placeholder="Search apps…"
          class="input pl-8 w-full"
          @input="onSearch"
        >
      </div>
      <!-- View toggle (list / grid) -->
      <div class="flex items-center gap-0.5 shrink-0 border border-slate-200 rounded-lg p-0.5">
        <button
          :class="['px-2 py-1 rounded text-xs transition-colors', viewMode === 'list' ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-slate-600']"
          title="List view"
          @click="viewMode = 'list'"
        >
          ☰
        </button>
        <button
          :class="['px-2 py-1 rounded text-xs transition-colors', viewMode === 'grid' ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-slate-600']"
          title="Grid view"
          @click="viewMode = 'grid'"
        >
          ⊞
        </button>
      </div>
      <!-- Selection actions -->
      <Transition
        enter-from-class="opacity-0 scale-95"
        enter-active-class="transition-all duration-150"
        leave-to-class="opacity-0 scale-95"
        leave-active-class="transition-all duration-150"
      >
        <div
          v-if="selectedKeys.size > 0"
          class="flex items-center gap-2 shrink-0"
        >
          <span class="text-xs text-slate-500">{{ selectedKeys.size }} selected</span>
          <button
            class="btn-secondary btn-sm text-xs"
            @click="clearSelection"
          >
            ✕
          </button>
          <button
            :disabled="batchInstalling"
            class="btn-primary btn-sm text-xs"
            @click="openPreflight"
          >
            Install {{ selectedKeys.size }} →
          </button>
        </div>
      </Transition>
    </div>

    <!-- Category pills — tight, single row -->
    <div class="flex gap-1 flex-wrap mb-4 items-center">
      <button
        :class="['pill', activeCats.size === 0 ? 'pill-active' : '']"
        @click="resetCats"
      >
        All
      </button>
      <button
        v-for="cat in Object.keys(allApps)"
        :key="cat"
        :class="['pill', activeCats.has(cat) ? 'pill-active' : '']"
        @click="toggleCat(cat)"
      >
        {{ CAT_LABELS[cat] || cat }}
        <span class="opacity-50 text-xs">{{ allApps[cat]?.length || 0 }}</span>
      </button>
      <button
        v-if="activeCats.size > 0"
        class="text-xs text-slate-400 hover:text-slate-600 ml-1"
        @click="resetCats"
      >
        ✕
      </button>
    </div>

    <!-- Failed installs banner — shown on return to view if any installs failed -->
    <Transition
      enter-from-class="opacity-0 -translate-y-1"
      enter-active-class="transition-all duration-200"
      leave-to-class="opacity-0 -translate-y-1"
      leave-active-class="transition-all duration-200"
    >
      <div
        v-if="showFailedBanner"
        class="mb-4"
      >
        <div class="rounded-lg bg-red-50 border border-red-200 p-3">
          <div class="flex items-center justify-between gap-3">
            <button
              class="flex-1 text-left text-sm font-medium text-red-700 hover:text-red-900"
              @click="failedBannerExpanded = !failedBannerExpanded"
            >
              {{ failedInstalls.length }} install{{ failedInstalls.length !== 1 ? 's' : '' }} failed — click to see details
            </button>
            <button
              class="text-xs text-red-400 hover:text-red-600 shrink-0"
              title="Dismiss"
              @click="showFailedBanner = false"
            >
              ✕
            </button>
          </div>
          <div
            v-if="failedBannerExpanded"
            class="mt-2 space-y-2"
          >
            <div
              v-for="item in failedInstalls"
              :key="item.key"
              class="rounded bg-red-100 border border-red-200 px-3 py-2"
            >
              <div class="text-xs font-medium text-red-800">
                {{ item.key }}
              </div>
              <div class="text-xs text-red-600 mt-0.5 break-all">
                {{ item.error || 'Install failed' }}
              </div>
            </div>
          </div>
        </div>
      </div>
    </Transition>

    <!-- Diagnoses panel — LLM-generated fix suggestions for install failures -->
    <Transition
      enter-from-class="opacity-0 -translate-y-1"
      enter-active-class="transition-all duration-200"
      leave-to-class="opacity-0 -translate-y-1"
      leave-active-class="transition-all duration-200"
    >
      <div
        v-if="diagnoses.length > 0"
        class="mb-4 space-y-3"
      >
        <DiagnosisCard
          v-for="d in diagnoses"
          :id="d.id"
          :key="d.id"
          :app-key="d.app_key"
          :problem="d.problem"
          :diagnosis-class="d.diagnosis_class"
          :suggested-fix="d.suggested_fix"
          :confidence="d.confidence"
          :status="d.status"
          :created-at="d.created_at"
          @dismiss="dismissDiagnosis"
        />
      </div>
    </Transition>

    <!-- App list — compact rows grouped by category (list) or compact grid -->
    <div style="min-height: 60vh">
      <!-- Stale-data notice when background refresh fails -->
      <Transition
        enter-from-class="opacity-0 -translate-y-1"
        enter-active-class="transition-all duration-200"
        leave-to-class="opacity-0 -translate-y-1"
        leave-active-class="transition-all duration-200"
      >
        <div
          v-if="installedListStale"
          class="mb-4"
        >
          <div class="rounded-lg bg-amber-50 border border-amber-200 p-3">
            <div class="flex items-center justify-between gap-3">
              <span class="text-sm text-amber-700">
                ⚠️ Couldn't refresh app list — installation status may be stale. Try reloading the page.
              </span>
              <button
                class="text-xs text-amber-400 hover:text-amber-600 shrink-0"
                title="Dismiss"
                @click="installedListStale = false"
              >
                ✕
              </button>
            </div>
          </div>
        </div>
      </Transition>

      <!-- Loading skeleton -->
      <div
        v-if="loading"
        class="space-y-px"
      >
        <div
          v-for="n in 12"
          :key="n"
          class="flex items-center gap-3 px-3 py-2 rounded-lg border border-slate-100 animate-pulse"
        >
          <div class="w-6 h-6 rounded bg-slate-100 shrink-0" />
          <div class="h-3 bg-slate-100 rounded flex-1 max-w-32" />
          <div class="h-3 bg-slate-100 rounded w-16 ml-auto" />
          <div class="h-5 bg-slate-100 rounded w-12" />
        </div>
      </div>

      <!-- ── Grid view: compact tiles across all visible apps ── -->
      <template v-else-if="viewMode === 'grid'">
        <div
          class="grid gap-2"
          style="grid-template-columns: repeat(auto-fill, minmax(160px, 1fr))"
        >
          <template
            v-for="(entries, cat) in filtered"
            :key="cat"
          >
            <div
              v-for="app in entries"
              :key="app.key"
              :class="[
                'relative flex flex-col items-center gap-1.5 p-3 rounded-xl border cursor-pointer transition-all select-none',
                installing === app.key
                  ? 'border-amber-300 bg-amber-50'
                  : selectedKeys.has(app.key)
                    ? 'border-sky-400 bg-sky-50 ring-2 ring-sky-200'
                    : isInstalled(app.key)
                      ? 'border-slate-100 bg-slate-50/60 opacity-70'
                      : 'border-slate-100 bg-white hover:border-slate-300 hover:shadow-sm'
              ]"
              @click="toggleSelect(app.key)"
            >
              <!-- Selection badge -->
              <div
                v-if="selectedKeys.has(app.key)"
                class="absolute top-1.5 right-1.5 w-4 h-4 rounded-full bg-sky-500 flex items-center justify-center shadow-sm"
              >
                <span class="text-white text-xs leading-none font-bold">✓</span>
              </div>

              <!-- Installed badge -->
              <div
                v-if="isInstalled(app.key)"
                class="absolute top-1.5 left-1.5 w-3 h-3 rounded-full bg-green-400"
                title="Installed"
              />

              <!-- App icon -->
              <div class="w-10 h-10 rounded-lg bg-slate-100 flex items-center justify-center overflow-hidden shrink-0">
                <img
                  :src="iconUrl(app)"
                  :alt="app.display_name"
                  class="w-9 h-9 object-contain"
                  @error="(e: Event) => { const t = e.target as HTMLImageElement; t.style.display='none'; (t.nextElementSibling as HTMLElement).style.display='block' }"
                >
                <span class="text-xl hidden">{{ app.icon }}</span>
              </div>

              <!-- Name -->
              <div class="text-center w-full">
                <span
                  :class="['text-xs font-medium leading-tight line-clamp-2',
                           isInstalled(app.key) ? 'text-slate-500' : 'text-slate-800']"
                >
                  {{ app.display_name }}
                </span>
                <span
                  v-if="(app as any).is_new"
                  class="block text-xs px-1.5 py-0 rounded-full bg-emerald-100 text-emerald-700 font-medium mt-0.5 mx-auto w-fit"
                >NEW</span>
              </div>

              <!-- Hover action: Install/Reinstall button -->
              <div
                class="w-full"
                @click.stop
              >
                <button
                  v-if="isInstalled(app.key)"
                  class="w-full text-xs py-0.5 rounded border border-slate-200 text-slate-400 hover:border-slate-300 transition-colors"
                  :disabled="installing === app.key || removing === app.key"
                  @click="singleInstall(app)"
                >
                  <span v-if="installing === app.key">…</span>
                  <span v-else>Reinstall</span>
                </button>
                <button
                  v-else
                  class="w-full text-xs py-0.5 rounded border border-slate-200 text-slate-500 hover:bg-orange-500 hover:border-orange-500 hover:text-white transition-colors"
                  :class="{ 'opacity-50 cursor-wait': installing === app.key }"
                  :disabled="installing === app.key"
                  @click="singleInstall(app)"
                >
                  <span v-if="installing === app.key">…</span>
                  <span v-else>Install</span>
                </button>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- ── List view: compact rows grouped by category (default) ── -->
      <template
        v-for="(entries, cat) in filtered"
        v-else
        :key="cat"
      >
        <div class="flex items-center gap-2 mt-4 mb-1 first:mt-0">
          <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">{{ CAT_LABELS[cat] || cat }}</span>
          <span class="text-xs text-slate-300">{{ entries.length }}</span>
        </div>

        <!-- One card containing all rows for this category -->
        <div class="card overflow-hidden">
          <div
            v-for="(app, idx) in entries"
            :key="app.key"
            :class="[
              'flex items-center gap-3 px-3 py-2 cursor-pointer transition-colors',
              idx < entries.length - 1 ? 'border-b border-slate-50' : '',
              installing === app.key
                ? 'bg-amber-50'
                : selectedKeys.has(app.key)
                  ? 'bg-sky-50'
                  : isInstalled(app.key)
                    ? 'bg-slate-50/60'
                    : 'hover:bg-slate-50'
            ]"
            @click="toggleSelect(app.key)"
          >
            <!-- Selection checkmark -->
            <div
              v-if="selectedKeys.has(app.key)"
              class="w-4 h-4 rounded-full bg-sky-500 flex items-center justify-center shrink-0"
            >
              <span class="text-white text-xs leading-none font-bold">✓</span>
            </div>

            <!-- App icon -->
            <div
              :class="['w-6 h-6 rounded flex items-center justify-center shrink-0 overflow-hidden',
                       selectedKeys.has(app.key) ? '' : 'bg-slate-100']"
            >
              <img
                :src="iconUrl(app)"
                :alt="app.display_name"
                class="w-5 h-5 object-contain"
                @error="(e: Event) => { const t = e.target as HTMLImageElement; t.style.display='none'; (t.nextElementSibling as HTMLElement).style.display='block' }"
              >
              <span class="text-sm hidden">{{ app.icon }}</span>
            </div>

            <!-- Name + badges -->
            <div class="flex-1 min-w-0 flex items-center gap-2">
              <span
                :class="['text-sm font-medium truncate',
                         isInstalled(app.key) ? 'text-slate-500' : 'text-slate-800']"
              >
                {{ app.display_name }}
              </span>
              <span
                v-if="(app as any).is_new"
                class="text-xs px-1.5 py-0.5 rounded-full bg-emerald-100 text-emerald-700 font-medium shrink-0"
              >NEW</span>
              <span
                v-if="sourceIssues.has(app.key)"
                class="text-xs text-red-500 shrink-0"
                title="Source URL issue"
              >⚠</span>
            </div>

            <!-- Actions -->
            <div
              class="flex items-center gap-2 shrink-0"
              @click.stop
            >
              <RouterLink
                v-if="isInstalled(app.key)"
                :to="`/apps/${app.key}`"
                class="text-xs text-slate-400 hover:text-slate-600"
              >
                Manage
              </RouterLink>

              <!-- Reinstall / Install button -->
              <button
                v-if="isInstalled(app.key)"
                class="text-xs px-2.5 py-0.5 rounded border border-slate-200 text-slate-500 hover:border-slate-300 transition-colors"
                :disabled="installing === app.key || removing === app.key"
                @click="singleInstall(app)"
              >
                <span
                  v-if="installing === app.key"
                  class="flex items-center gap-1"
                >
                  <span class="inline-block w-2.5 h-2.5 border border-slate-400 border-t-transparent rounded-full animate-spin" />
                  Installing…
                </span>
                <span v-else>Reinstall</span>
              </button>
              <button
                v-else
                class="text-xs px-2.5 py-0.5 rounded border border-slate-200 text-slate-500 hover:bg-orange-500 hover:border-orange-500 hover:text-white transition-colors"
                :class="{ 'opacity-50 cursor-wait': installing === app.key }"
                :disabled="installing === app.key"
                @click="singleInstall(app)"
              >
                <span
                  v-if="installing === app.key"
                  class="flex items-center gap-1"
                >
                  <span class="inline-block w-2.5 h-2.5 border border-orange-300 border-t-transparent rounded-full animate-spin" />
                  Installing…
                </span>
                <span v-else>Install</span>
              </button>

              <!-- Remove button / inline confirm -->
              <template v-if="isInstalled(app.key)">
                <!-- Normal remove trigger -->
                <button
                  v-if="removeTarget !== app.key && removing !== app.key"
                  class="text-xs px-2.5 py-0.5 rounded border border-slate-200 text-slate-400 hover:border-red-300 hover:text-red-500 transition-colors"
                  title="Remove this app"
                  @click="removeTarget = app.key; removeDelConfig = false"
                >
                  Remove
                </button>
                <!-- Spinner while removing -->
                <span
                  v-else-if="removing === app.key"
                  class="flex items-center gap-1 text-xs text-red-400"
                >
                  <span class="inline-block w-2.5 h-2.5 border border-red-300 border-t-transparent rounded-full animate-spin" />
                  Removing…
                </span>
                <!-- Inline confirm -->
                <div
                  v-else-if="removeTarget === app.key"
                  class="flex items-center gap-1.5"
                >
                  <label
                    class="flex items-center gap-1 text-xs text-slate-500 cursor-pointer"
                    title="Also delete config folder on disk"
                  >
                    <input
                      v-model="removeDelConfig"
                      type="checkbox"
                      class="w-3 h-3"
                    >
                    <span>+cfg</span>
                  </label>
                  <button
                    class="text-xs px-2 py-0.5 rounded border border-slate-200 text-slate-400 hover:border-slate-300"
                    @click="removeTarget = null"
                  >
                    Cancel
                  </button>
                  <button
                    class="text-xs px-2 py-0.5 rounded border border-red-300 text-red-500 hover:bg-red-500 hover:text-white transition-colors"
                    @click="doRemove"
                  >
                    Confirm
                  </button>
                </div>
              </template>
            </div>
          </div>
        </div>
      </template>

      <!-- Empty state -->
      <div
        v-if="!loading && totalVisible === 0"
        class="text-center py-16 text-slate-400 text-sm"
      >
        No apps match "{{ search }}"
      </div>
    </div><!-- /min-height wrapper -->
  </div><!-- /root -->

  <!-- ── Single-app install modal ── -->
  <Teleport to="body">
    <div
      v-if="installTarget"
      class="fixed inset-0 z-50 flex items-center justify-center"
    >
      <div
        class="absolute inset-0 bg-black/30 backdrop-blur-sm"
        @click="installTarget = null"
      />
      <div class="relative card w-full max-w-md mx-4 card-body">
        <!-- Header -->
        <div class="flex items-center gap-3 mb-4">
          <span class="text-2xl">{{ installTarget.icon }}</span>
          <div>
            <h3 class="font-semibold text-slate-900">
              {{ installTarget.display_name }}
            </h3>
            <p class="text-xs text-slate-400 capitalize">
              {{ installTarget.category }}
            </p>
          </div>
        </div>

        <!-- Description -->
        <p class="text-sm text-slate-500 mb-4 leading-relaxed">
          {{ installTarget.description }}
        </p>

        <!-- Hardware warnings -->
        <div
          v-if="installTarget.has_gpu || installTarget.hardware_note"
          class="space-y-2 mb-4"
        >
          <div
            v-if="installTarget.has_gpu"
            class="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-800"
          >
            <span class="shrink-0 text-base leading-none">⚡</span>
            <span v-if="installTarget.gpu_optional">
              <strong>GPU recommended</strong> — hardware acceleration (transcoding / inference) requires a
              compatible NVIDIA or AMD GPU. Works without one but at reduced performance.
            </span>
            <span v-else>
              <strong>GPU required</strong> — this app needs a compatible NVIDIA or AMD GPU to function.
            </span>
          </div>
          <div
            v-if="installTarget.hardware_note"
            class="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-800"
          >
            <span class="shrink-0 text-base leading-none">🖴</span>
            <span>{{ installTarget.hardware_note }}</span>
          </div>
        </div>

        <!-- Options (only when not installing) -->
        <template v-if="!installing">
          <div class="space-y-3 mb-4">
            <div v-if="installTarget.web_port">
              <label class="text-xs font-medium text-slate-600">Host port override <span class="text-slate-400">(optional)</span></label>
              <input
                v-model.number="installOpts.host_port"
                type="number"
                placeholder="leave blank for default"
                class="input w-full mt-1 text-sm"
              >
            </div>
          </div>

          <div
            v-if="installError"
            class="rounded-lg bg-red-50 border border-red-100 p-3 text-xs text-red-700 mb-4"
          >
            {{ installError }}
          </div>

          <div class="flex gap-3">
            <button
              class="btn-secondary flex-1"
              @click="installTarget = null"
            >
              Cancel
            </button>
            <button
              class="btn-primary flex-1"
              @click="confirmInstall"
            >
              Install
            </button>
          </div>
        </template>

        <!-- Progress (while installing) -->
        <template v-else>
          <div class="mb-3">
            <div class="flex items-center justify-between text-xs text-slate-500 mb-1">
              <span>{{ installTimeLabel }}</span>
              <span>{{ installProgress }}%</span>
            </div>
            <div class="h-1.5 bg-slate-100 rounded-full overflow-hidden">
              <div
                class="h-full bg-orange-500 rounded-full transition-all duration-300"
                :style="{ width: installProgress + '%' }"
              />
            </div>
          </div>

          <div
            v-if="installSteps.length"
            class="space-y-1 max-h-48 overflow-y-auto mb-3 pr-1"
          >
            <div
              v-for="step in installSteps"
              :key="step.step ?? step.name"
              :class="[
                'flex items-start gap-2 text-xs rounded px-2 py-1',
                step.status === 'error' ? 'bg-red-50 text-red-700' :
                step.status === 'warning' ? 'bg-amber-50 text-amber-700' :
                step.status === 'ok' ? 'text-slate-600' :
                step.status === 'skipped' ? 'text-slate-400' : 'text-slate-600'
              ]"
            >
              <!-- Status icon: done / in-progress / skipped / warning / error -->
              <span class="shrink-0 w-4 text-center leading-none mt-0.5">
                <span
                  v-if="step.status === 'ok'"
                  class="text-green-500 font-bold"
                >✓</span>
                <span
                  v-else-if="step.status === 'error'"
                  class="text-red-500 font-bold"
                >✗</span>
                <span
                  v-else-if="step.status === 'warning'"
                  class="text-amber-500 font-bold"
                >!</span>
                <span
                  v-else-if="step.status === 'skipped'"
                  class="text-slate-400"
                >—</span>
                <span
                  v-else
                  class="inline-block w-3 h-3 border border-blue-400 border-t-transparent rounded-full animate-spin"
                />
              </span>
              <!-- Step label + message -->
              <div class="flex-1 min-w-0">
                <span class="font-medium">{{ STEP_LABELS[step.step ?? step.name] ?? formatStepName(step.step ?? step.name) }}</span>
                <span
                  v-if="step.message"
                  class="ml-1 opacity-75"
                >— {{ step.message }}</span>
              </div>
            </div>
          </div>

          <div
            v-if="installError"
            class="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700 mb-3"
          >
            <div class="font-medium mb-0.5">
              <span v-if="installFailedStep">Failed at: {{ STEP_LABELS[installFailedStep] ?? formatStepName(installFailedStep) }}</span>
              <span v-else>Installation failed</span>
            </div>
            <div class="opacity-90">
              {{ installError }}
            </div>
            <button
              class="block mt-2 text-red-500 hover:text-red-700 font-medium"
              @click="installTarget = null"
            >
              Dismiss
            </button>
          </div>

          <p
            v-else
            class="text-xs text-slate-400 text-center"
          >
            Installing — please wait…
          </p>
        </template>
      </div>
    </div>
  </Teleport>

  <!-- ── Batch install preflight modal ── -->
  <Teleport to="body">
    <div
      v-if="showPreflight"
      class="fixed inset-0 z-50 flex items-center justify-center"
    >
      <div
        class="absolute inset-0 bg-black/30 backdrop-blur-sm"
        @click="showPreflight = false"
      />
      <div class="relative card w-full max-w-lg mx-4 card-body max-h-[90vh] overflow-y-auto">
        <!-- Header -->
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-semibold text-slate-900">
            Install {{ selectedKeys.size }} app{{ selectedKeys.size !== 1 ? 's' : '' }}
          </h3>
          <button
            class="text-slate-400 hover:text-slate-600 text-lg leading-none"
            @click="showPreflight = false"
          >
            ✕
          </button>
        </div>

        <!-- Loading state -->
        <div
          v-if="!preflightResult"
          class="flex items-center gap-2 text-sm text-slate-500 py-4"
        >
          <span class="inline-block w-4 h-4 border border-slate-400 border-t-transparent rounded-full animate-spin" />
          Checking dependencies…
        </div>

        <!-- Preflight result -->
        <template v-else-if="!batchInstalling && batchProgress.length === 0">
          <!-- Required companions warning — apps with required wiring peers not in install set -->
          <div
            v-if="batchCompanionWarning.length > 0"
            class="rounded-lg bg-amber-50 border border-amber-200 p-3 mb-4"
          >
            <div class="flex items-start gap-2 text-amber-800">
              <span class="text-base shrink-0 leading-none mt-0.5">⚠</span>
              <div>
                <p class="text-sm font-medium mb-1">
                  Required companion apps not selected
                </p>
                <ul class="text-xs space-y-1">
                  <li
                    v-for="c in batchCompanionWarning"
                    :key="c.source + c.companion"
                  >
                    <span class="font-medium">{{ c.source }}</span> requires
                    <span class="font-medium">{{ c.companion }}</span>
                    <span
                      v-if="c.wire_type"
                      class="text-amber-600"
                    > ({{ c.wire_type }})</span>
                    — {{ c.description }}
                  </li>
                </ul>
                <div class="flex gap-2 mt-3">
                  <button
                    class="btn-primary btn-sm text-xs"
                    @click="addCompanionsAndInstall"
                  >
                    Add companions &amp; install
                  </button>
                  <button
                    class="btn-secondary btn-sm text-xs"
                    @click="runBatchInstall"
                  >
                    Install anyway
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- Preflight issues (errors / warnings / info) -->
          <div
            v-if="preflightResult.issues?.length"
            class="space-y-2 mb-4"
          >
            <div
              v-for="issue in preflightResult.issues"
              :key="issue.message"
              :class="[
                'rounded-lg p-3 text-xs',
                issue.level === 'error' ? 'bg-red-50 border border-red-200 text-red-700' :
                issue.level === 'warning' ? 'bg-amber-50 border border-amber-200 text-amber-800' :
                'bg-slate-50 border border-slate-200 text-slate-600'
              ]"
            >
              {{ issue.message }}
            </div>
          </div>

          <!-- Install order -->
          <div
            v-if="preflightResult.install_order?.length"
            class="mb-4"
          >
            <p class="text-xs font-medium text-slate-600 mb-2">
              Install order
            </p>
            <div class="flex flex-wrap gap-1.5">
              <span
                v-for="key in preflightResult.install_order"
                :key="key"
                class="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 font-mono"
              >
                {{ key }}
              </span>
            </div>
          </div>

          <!-- No apps to install -->
          <div
            v-else
            class="text-sm text-slate-500 mb-4"
          >
            All selected apps are already installed.
          </div>

          <!-- Actions — only shown when no companion warning (warning has its own buttons) -->
          <div
            v-if="batchCompanionWarning.length === 0"
            class="flex gap-3"
          >
            <button
              class="btn-secondary flex-1"
              @click="showPreflight = false"
            >
              Cancel
            </button>
            <button
              :disabled="!preflightResult.can_proceed || !preflightResult.install_order?.length"
              class="btn-primary flex-1"
              @click="runBatchInstall"
            >
              Install {{ preflightResult.install_order?.length }} app{{ preflightResult.install_order?.length !== 1 ? 's' : '' }}
            </button>
          </div>
        </template>

        <!-- Install in progress -->
        <template v-else>
          <!-- Required companions warning shown alongside install progress -->
          <div
            v-if="batchCompanionWarning.length > 0"
            class="rounded-lg bg-amber-50 border border-amber-200 p-3 mb-4"
          >
            <div class="flex items-start gap-2 text-amber-800">
              <span class="text-base shrink-0 leading-none mt-0.5">⚠</span>
              <div>
                <p class="text-sm font-medium mb-1">
                  Wiring pending — companion apps not installed
                </p>
                <ul class="text-xs space-y-1">
                  <li
                    v-for="c in batchCompanionWarning"
                    :key="c.source + c.companion"
                  >
                    <span class="font-medium">{{ c.source }}</span> requires
                    <span class="font-medium">{{ c.companion }}</span>
                    <span
                      v-if="c.wire_type"
                      class="text-amber-600"
                    > ({{ c.wire_type }})</span>
                    — wiring will complete once {{ c.companion }} is installed
                  </li>
                </ul>
              </div>
            </div>
          </div>

          <div class="space-y-2 mb-4">
            <div
              v-for="item in batchProgress"
              :key="item.key"
              :class="[
                'flex items-center gap-2 text-xs rounded px-2 py-1.5',
                item.status === 'error' ? 'bg-red-50 text-red-700' :
                item.status === 'ok' ? 'bg-green-50 text-green-700' :
                item.status === 'running' ? 'bg-blue-50 text-blue-700' :
                'text-slate-500'
              ]"
            >
              <span class="shrink-0 w-4 text-center leading-none">
                <span
                  v-if="item.status === 'ok'"
                  class="text-green-500 font-bold"
                >✓</span>
                <span
                  v-else-if="item.status === 'error'"
                  class="text-red-500 font-bold"
                >✗</span>
                <span
                  v-else-if="item.status === 'running'"
                  class="inline-block w-3 h-3 border border-blue-400 border-t-transparent rounded-full animate-spin"
                />
                <span
                  v-else
                  class="text-slate-300"
                >·</span>
              </span>
              <span class="font-mono font-medium">{{ item.key }}</span>
              <span class="opacity-75 ml-1">{{ item.message }}</span>
            </div>
          </div>

          <p
            v-if="batchInstalling"
            class="text-xs text-slate-400 text-center"
          >
            Installing — please wait…
          </p>
          <button
            v-else
            class="btn-secondary w-full text-sm"
            @click="showPreflight = false"
          >
            Close
          </button>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import { catalog, apps as appsApi, health as healthApi, agent as agentApi } from '../api/client'
import { catalogCache, installedCache, setCatalogCache, setInstalledCache } from '../catalogCache'
import { useToast } from '@/composables/useToast'
import type { CatalogEntry, AppStatus } from '../api/client'
import DiagnosisCard from '../components/DiagnosisCard.vue'

const toast = useToast()

// ── Diagnoses (LLM-generated fix suggestions) ────────────────────────────
interface DiagnosisItem {
  id: number
  app_key: string
  problem: string
  diagnosis_class: string
  suggested_fix: string
  confidence: number
  status: string
  created_at: number
}

const diagnoses = ref<DiagnosisItem[]>([])

// ── Failed installs banner ───────────────────────────────────────────────
interface FailedInstall {
  key: string
  error: string | null
}

const failedInstalls = ref<FailedInstall[]>([])
const showFailedBanner = ref(false)
const failedBannerExpanded = ref(false)
const installedListStale = ref(false)  // set when installed-list refresh fails

async function loadFailedInstalls() {
  try {
    const data = await appsApi.installsProgress()
    const apps: Record<string, any> = data.apps ?? {}
    const failed = Object.entries(apps)
      .filter(([, info]) => info.done === true && info.ok === false)
      .map(([key, info]) => ({ key, error: info.error ?? null }))
    failedInstalls.value = failed
    if (failed.length > 0) {
      showFailedBanner.value = true
    }
  } catch {
    // Banner is best-effort — never surface errors to the user
  }
}

async function loadDiagnoses() {
  try {
    const data = await agentApi.diagnoses()
    diagnoses.value = data.diagnoses ?? []
  } catch {
    // Diagnoses panel is best-effort — never surface errors to the user
  }
}

function dismissDiagnosis(id: number) {
  diagnoses.value = diagnoses.value.filter(d => d.id !== id)
}

const CAT_LABELS: Record<string, string> = {
  arr: 'Arr', media: 'Media', ai: 'AI',
  monitoring: 'Monitoring', productivity: 'Productivity', tools: 'Tools',
}

// Human-readable labels for install step names emitted by executor.py
const STEP_LABELS: Record<string, string> = {
  queued:               'Queued',
  validate:             'Validating',
  load_manifest:        'Loading manifest',
  deps:                 'Checking dependencies',
  deps_postgres:        'Starting PostgreSQL',
  deps_redis:           'Starting Redis',
  deps_app:             'Checking app dependencies',
  companions:           'Starting companion services',
  config_dir:           'Preparing config directory',
  seed_config:          'Seeding config files',
  port_check:           'Checking port availability',
  fragment:             'Writing compose fragment',
  deploy:               'Pulling image & starting container',
  post_wait_healthy:    'Waiting for health check',
  post_api_ready:       'Waiting for API readiness',
  post_wire:            'Wiring app connections',
  post_wire_prowlarr:   'Wiring Prowlarr',
  post_wire_qbt:        'Wiring qBittorrent',
  post_wire_nzbget:     'Wiring NZBGet',
  post_wire_sabnzbd:    'Wiring SABnzbd',
  hostname_register:    'Registering hostname',
  smoke_test:           'Running smoke test',
  register:             'Registering app',
  unexpected:           'Unexpected error',
  // Remove steps
  stop:                 'Stopping container',
  hostname_unregister:  'Removing hostname',
  unwire:               'Removing wiring',
  unregister:           'Removing from registry',
  config:               'Cleaning config',
  state:                'Removing from SLOP',
  companions_remove:    'Removing companions',
  // Update steps
  update:               'Updating',
  install_new:          'Installing new version',
}

/** Convert a raw step name like "post_api_ready" → "Post api ready" when no label exists */
function formatStepName(name: string): string {
  if (!name) return ''
  return name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

const search = ref('')
const viewMode = ref<'list' | 'grid'>('list')  // 'list' = category rows, 'grid' = compact tiles
// Use shared cache module — may already be primed by App.vue prefetch
const allApps = ref<Record<string, CatalogEntry[]>>(catalogCache ?? {})
const loading = ref(catalogCache === null)
const installedKeys = ref<Set<string>>(new Set())
const selectedKeys = ref<Set<string>>(new Set())
const activeCats = ref<Set<string>>(new Set())

// Active interval/timer refs for cleanup
let _activePoll: ReturnType<typeof setInterval> | null = null
let _activeEs: EventSource | null = null

// Uninstall (inline confirmation)
const removeTarget = ref<string | null>(null)   // key waiting for confirm
const removing = ref<string | null>(null)        // key currently being removed
const removeDelConfig = ref(false)               // "also delete config folder" option

async function doRemove() {
  const key = removeTarget.value
  if (!key) return
  removeTarget.value = null
  removing.value = key
  try {
    await appsApi.remove(key, removeDelConfig.value)
    installedKeys.value.delete(key)
    installedKeys.value = new Set(installedKeys.value) // trigger reactivity
    toast.success(`${key} removed.`)
  } catch (e) {
    toast.error(`Could not remove ${key}.`, e instanceof Error ? e.message : String(e))
  } finally {
    removing.value = null
    removeDelConfig.value = false
  }
}

// Single install
const installing = ref<string | null>(null)
const installTarget = ref<CatalogEntry | null>(null)
const installOpts = ref({ host_port: null as number | null, vpn_scoped: false })
const installError = ref<string | null>(null)
const installFailedStep = ref<string | null>(null)  // step name that caused the failure
const installSteps = ref<any[]>([])

// Time-based progress — fills over expected duration regardless of step count
const installProgress = ref(5)
const installTimeLabel = ref('Starting…')
let _installTimer: ReturnType<typeof setInterval> | null = null

function startInstallProgress(expectedSeconds: number) {
  installProgress.value = 5
  installTimeLabel.value = 'Starting…'
  if (_installTimer) clearInterval(_installTimer)
  const start = Date.now()
  const total = expectedSeconds * 1000
  _installTimer = setInterval(() => {
    const elapsed = Date.now() - start
    const pct = Math.min(90, 5 + (elapsed / total) * 85) // fills to 90% max
    const rem = Math.max(0, Math.round((total - elapsed) / 1000))
    installProgress.value = Math.round(pct)
    installTimeLabel.value = elapsed < 3000
      ? 'Starting…'
      : rem > 5
        ? `~${rem}s remaining`
        : 'Almost done…'
  }, 250)
}

function finishInstallProgress() {
  if (_installTimer) { clearInterval(_installTimer); _installTimer = null }
  installProgress.value = 100
  installTimeLabel.value = 'Complete'
}

function stopInstallProgress() {
  if (_installTimer) { clearInterval(_installTimer); _installTimer = null }
}

// Batch install
const showPreflight = ref(false)
const preflightResult = ref<any>(null)
const batchInstalling = ref(false)
// required_companions entries returned by /batch/install for wiring peers not in the install set
const batchCompanionWarning = ref<{ source: string; companion: string; wire_type: string; description: string }[]>([])
const batchProgress = ref<{ key: string; status: string; message: string }[]>([])
let _batchPollFailures = 0  // consecutive poll failures in batch install loop

const filtered = computed(() => {
  const q = search.value.toLowerCase().trim()
  const result: Record<string, CatalogEntry[]> = {}
  for (const [cat, entries] of Object.entries(allApps.value)) {
    if (activeCats.value.size > 0 && !activeCats.value.has(cat)) continue
    const visible = entries.filter(app =>
      !q ||
      app.display_name.toLowerCase().includes(q) ||
      app.description.toLowerCase().includes(q) ||
      (app.tags || []).some((t: string) => t.toLowerCase().includes(q))
    )
    if (visible.length) result[cat] = visible
  }
  return result
})

const totalVisible = computed(() =>
  Object.values(filtered.value).reduce((s, a) => s + a.length, 0)
)

function iconUrl(app: any): string {
  const name = (app.dashboard_icon || app.key).replace(/_/g, '-').toLowerCase()
  return `https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/${name}.png`
}

function isInstalled(key: string) { return installedKeys.value.has(key) }

const sourceIssues = ref<Set<string>>(new Set())

async function loadSourceIssues() {
  try {
    const d = await healthApi.sources()
    sourceIssues.value = new Set(
      (d.issues || [])
        .filter((i: any) => i.source_type === 'docker_image')
        .map((i: any) => i.resource_key)
    )
  } catch { /* source issues are advisory; ignore fetch errors */ }
}
function onSearch() {}

function toggleCat(cat: string) {
  activeCats.value.has(cat) ? activeCats.value = new Set([...activeCats.value].filter(c => c !== cat)) : activeCats.value = new Set([...activeCats.value, cat])
}
function resetCats() { activeCats.value = new Set() }

function toggleSelect(key: string) {
  if (isInstalled(key)) return
  selectedKeys.value.has(key) ? selectedKeys.value = new Set([...selectedKeys.value].filter(k => k !== key)) : selectedKeys.value = new Set([...selectedKeys.value, key])
}

function clearSelection() { selectedKeys.value = new Set() }

async function openPreflight() {
  showPreflight.value = true
  preflightResult.value = null
  batchProgress.value = []
  batchCompanionWarning.value = []
  try {
    const { data } = await appsApi.batchPreflight([...selectedKeys.value])
    preflightResult.value = data
  } catch {
    toast.error('Could not run pre-flight check.')
  }
}

/** Add all missing companion keys to the selection and re-run preflight, then install. */
async function addCompanionsAndInstall() {
  for (const c of batchCompanionWarning.value) {
    selectedKeys.value = new Set([...selectedKeys.value, c.companion])
  }
  batchCompanionWarning.value = []
  await openPreflight()
  // openPreflight is async and re-fetches preflight; install only when result is ready
  // We wait for the next tick then trigger install if can_proceed
  await new Promise(r => setTimeout(r, 0))
  if (preflightResult.value?.can_proceed && preflightResult.value?.install_order?.length) {
    await runBatchInstall()
  }
}

async function runBatchInstall() {
  if (!preflightResult.value?.can_proceed) return
  batchCompanionWarning.value = []
  batchInstalling.value = true
  batchProgress.value = (preflightResult.value.install_order || []).map((k: string) => ({
    key: k, status: 'pending', message: 'Waiting…'
  }))

  try {
    const { ok: installOk, data: installBody } = await appsApi.batchInstall([...selectedKeys.value])
    const installData = installOk ? installBody : null
    // Surface any required companions not yet installed — install continues in background
    if (installData?.required_companions?.length) {
      batchCompanionWarning.value = installData.required_companions
    }

    // Poll each app
    _batchPollFailures = 0
    for (const item of batchProgress.value) {
      item.status = 'running'
      item.message = 'Installing…'
      const deadline = Date.now() + 600_000
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 800))
        try {
          const prog = await appsApi.installProgress(item.key)
          _batchPollFailures = 0  // reset on success
          if (prog.done) {
            item.status = prog.ok ? 'ok' : 'error'
            item.message = prog.ok ? 'Installed' : (prog.error || 'Failed')
            if (prog.ok) installedKeys.value.add(item.key)
            break
          }
        } catch (err) {
          _batchPollFailures++
          console.error(`Batch install poll failure #${_batchPollFailures} for ${item.key}:`, err)
          if (_batchPollFailures >= 3) {
            toast.error('Installation monitoring lost', `Monitoring stopped after 3 consecutive poll failures. Check app status manually.`, 8000)
            batchInstalling.value = false
            return
          }
        }
      }
    }

    const failed = batchProgress.value.filter(i => i.status === 'error').length
    const installed = batchProgress.value.filter(i => i.status === 'ok').length
    if (failed === 0) {
      toast.success(`${installed} app${installed !== 1 ? 's' : ''} installed successfully.`)
      clearSelection()
      setTimeout(() => { showPreflight.value = false }, 2000)
    } else {
      toast.warn(`${installed} installed, ${failed} failed.`)
    }
  } catch (e) {
    toast.error('Batch install failed.', e instanceof Error ? e.message : String(e))
  } finally {
    batchInstalling.value = false
  }
}

function singleInstall(app: CatalogEntry) {
  installTarget.value = app
  installOpts.value = { host_port: null, vpn_scoped: false }
  installError.value = null
  installFailedStep.value = null
  installSteps.value = []
}

async function confirmInstall() {
  if (!installTarget.value) return
  const key = installTarget.value.key
  installing.value = key
  installError.value = null
  installFailedStep.value = null
  installSteps.value = []
  const graceSecs = (installTarget.value as any).start_grace_s || 60
  startInstallProgress(graceSecs + 30) // grace + pull time estimate

  try {
    const opts: Record<string, unknown> = {}
    if (installOpts.value.host_port) opts.host_port = installOpts.value.host_port
    if (installOpts.value.vpn_scoped) opts.vpn_scoped = true
    await appsApi.install(key, opts)

    const es = new EventSource(`/api/v1/apps/${key}/install/stream`)
    _activeEs = es

    // 5-minute (300_000 ms) install poll timeout. On expiry the UI must surface
    // the failure (installError + "timed out") and reset the progress bar
    // (stopInstallProgress) — never silently stop, or the user is left staring
    // at a stuck progress bar with no error.
    const INSTALL_TIMEOUT_MS = 300_000
    const onInstallTimeout = () => {
      es.close(); _activeEs = null
      if (installing.value === key) {
        installing.value = null
        installError.value = `Install timed out after 5 minutes. The container may still be starting. Check: docker logs ${key}`
        stopInstallProgress()
        toast.error(`Install timed out for ${key}.`, installError.value ?? undefined, 8000)
      }
    }
    const timeout = setTimeout(onInstallTimeout, INSTALL_TIMEOUT_MS)

    es.onmessage = (event) => {
      const step = JSON.parse(event.data)
      if (step.step === '__done__') {
        clearTimeout(timeout)
        es.close(); _activeEs = null
        if (step.status === 'ok') {
          installedKeys.value.add(key)
          finishInstallProgress()
          toast.success(`${installTarget.value?.display_name ?? key} installed.`)
          setTimeout(() => { installTarget.value = null }, 1500)
        } else {
          // Find the last failed step to show "Failed at: <label>"
          const failedStep = [...installSteps.value].reverse().find(s => s.status === 'error')
          installFailedStep.value = failedStep?.step ?? null
          installError.value = step.message ?? 'Installation failed.'
          stopInstallProgress()
          toast.error(`Failed to install ${key}.`, installError.value ?? undefined, 8000)
        }
        installing.value = null
      } else {
        installSteps.value = [...installSteps.value.filter(s => s.step !== step.step), step]
      }
    }

    es.onerror = () => {
      es.close(); _activeEs = null
      // Fall back to 500ms poll (matches backend poll endpoint recommendation)
      _activePoll = setInterval(async () => {
        try {
          const progress = await appsApi.installProgress(key)
          installSteps.value = progress.steps ?? []
          if (progress.done) {
            clearInterval(_activePoll!); _activePoll = null
            clearTimeout(timeout)
            if (progress.ok) {
              installedKeys.value.add(key)
              finishInstallProgress()
              toast.success(`${installTarget.value?.display_name ?? key} installed.`)
              setTimeout(() => { installTarget.value = null }, 1500)
            } else {
              const failedStep = (progress.steps ?? []).reverse().find((s: any) => s.status === 'error')
              installFailedStep.value = failedStep?.step ?? null
              installError.value = progress.error ?? 'Installation failed.'
              stopInstallProgress()
              toast.error(`Failed to install ${key}.`, installError.value, 8000)
            }
            installing.value = null
          }
        } catch { clearInterval(_activePoll!); _activePoll = null; installing.value = null }
      }, 500)
    }

  } catch (e) {
    installError.value = e instanceof Error ? e.message : String(e)
    stopInstallProgress()
    installing.value = null
  }
}

onUnmounted(() => {
  if (_activePoll) clearInterval(_activePoll)
  if (_activeEs) { _activeEs.close(); _activeEs = null }
  if (_installTimer) clearInterval(_installTimer)
})

onMounted(async () => {
  loadDiagnoses()
  loadSourceIssues()
  loadFailedInstalls()

  // If cache already primed (by App.vue prefetch or prior visit), render instantly
  if (catalogCache) {
    allApps.value = catalogCache
    if (installedCache) installedKeys.value = installedCache
    loading.value = false
    // Refresh installed list in background
    appsApi.list().then(list => {
      setInstalledCache(new Set(list.map((a: AppStatus) => a.key)))
      installedKeys.value = installedCache!
      installedListStale.value = false
    }).catch((err) => {
      console.error('Failed to refresh installed apps list:', err)
      installedListStale.value = true
    })
    return
  }

  // Cache not ready — load both in parallel, show skeleton until done
  const [catalogData, appList] = await Promise.allSettled([catalog.all(), appsApi.list()])
  if (catalogData.status === 'fulfilled') {
    setCatalogCache(catalogData.value)
    allApps.value = catalogCache!
  }
  if (appList.status === 'fulfilled') {
    setInstalledCache(new Set(appList.value.map((a: AppStatus) => a.key)))
    installedKeys.value = installedCache!
  }
  loading.value = false
})
</script>
<style scoped>
/* ── Card ── */
/* Category pills */
.pill {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 2px 8px; border-radius: 20px;
  font-size: 11px; font-weight: 500;
  cursor: pointer; border: 0.5px solid var(--color-border-secondary);
  background: var(--color-background-primary); color: var(--color-text-secondary);
  transition: all 0.1s; user-select: none;
}
.pill:hover { border-color: var(--color-border-primary); color: var(--color-text-primary); }
.pill-active { background: #F26419; border-color: #F26419; color: #fff; }
</style>