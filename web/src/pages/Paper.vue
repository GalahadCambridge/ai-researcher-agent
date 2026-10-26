<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import PdfViewer from '../components/PdfViewer.vue'
import MarkdownView from '../components/MarkdownView.vue'
import EvidenceChain from '../components/EvidenceChain.vue'
import TimeText from '../components/TimeText.vue'
import { claimTrace, getPaper, listClaims, pdfUrl } from '../api/paper'
import { readFile } from '../api/artifacts'
import { exportUrl } from '../api/projects'
import { SECTION, SUPPORT, label } from '../constants/labels'
import { renderClaimText } from '../utils/format'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P10 论文与论断：左侧 PDF（可选版本），右侧论断 / 核验报告 / 编译报告
const { pid } = useProject()
const paper = useLoader(() => getPaper(pid.value))
const claims = useLoader(() => listClaims(pid.value))
const loadAll = (silent) => Promise.all([paper.load(silent), claims.load(silent)])
onMounted(() => loadAll())
useProjectRefresh(loadAll, ['event'])

// ---------------------------------------------------------------- PDF 版本
const version = ref(null)
const pdfVersions = computed(() => [...(paper.data.value?.pdf_versions || [])].reverse())
watch(pdfVersions, (v) => { if (v.length && !v.some((x) => x.ref.version === version.value)) version.value = v[0].ref.version })

// ---------------------------------------------------------------- 论断
const tab = ref('claims')
const filter = reactive({ support: '', section: '' })
const rows = computed(() => {
  const d = claims.data.value
  if (!d) return []
  return d.claims.map((c) => {
    const chk = d.checks[c.claim_id]
    return { ...c, display: renderClaimText(c), support: chk?.support || null, issues: chk?.issues || [] }
  })
})
const filtered = computed(() => rows.value.filter((r) => (!filter.support || r.support === filter.support) && (!filter.section || r.section === filter.section)))
const sections = computed(() => [...new Set(rows.value.map((r) => r.section))])
const BAD = ['unsupported', 'contradicted']
const badCount = computed(() => rows.value.filter((r) => BAD.includes(r.support)).length)

const trace = reactive({ open: false, loading: false, data: null, id: '', error: false })
async function openTrace(id) {
  Object.assign(trace, { open: true, loading: true, data: null, id, error: false })
  try {
    trace.data = await claimTrace(pid.value, id)
  } catch {
    trace.error = true
  } finally {
    trace.loading = false
  }
}

// ---------------------------------------------------------------- 核验报告
const reviewMd = ref('')
watch(() => paper.data.value?.review_summary?.file, async (f) => {
  reviewMd.value = ''
  if (!f) return
  try {
    reviewMd.value = (await readFile(pid.value, f.replace(/\.json$/, '.md'))).content
  } catch {
    reviewMd.value = ''
  }
}, { immediate: true })
const compile = computed(() => paper.data.value?.compile_report)
const counts = computed(() => paper.data.value?.review_summary?.counts || {})
</script>

<template>
  <div>
    <div class="page-title">
      <h2>{{ t('paper.title') }}</h2>
      <span class="spacer" />
      <a :href="exportUrl(pid, 'paper')"><el-button icon="Download">{{ t('paper.exportPaper') }}</el-button></a>
      <a :href="exportUrl(pid, 'archive')"><el-button icon="Box">{{ t('paper.exportArchive') }}</el-button></a>
    </div>
    <PageState :loading="paper.loading.value && !paper.data.value" :error="paper.error.value" @retry="loadAll()">
      <div class="cols">
        <el-card shadow="never" class="left">
          <template #header>
            <div class="ch">
              <b>PDF</b>
              <el-select v-if="pdfVersions.length" v-model="version" size="small" style="width: 220px">
                <el-option v-for="v in pdfVersions" :key="v.ref.version" :value="v.ref.version"
                  :label="`v${v.ref.version}${v.note ? ' · ' + v.note : ''}`">
                  <span>v{{ v.ref.version }}</span>
                  <span class="muted small opt"><TimeText :value="v.created_at" /> {{ v.producer }}</span>
                </el-option>
              </el-select>
            </div>
          </template>
          <PdfViewer v-if="version" :src="pdfUrl(pid, version)" height="78vh" />
          <el-empty v-else :description="t('paper.noPdf')" />
        </el-card>

        <el-card shadow="never" class="right">
          <el-tabs v-model="tab">
            <el-tab-pane name="claims">
              <template #label>{{ t('paper.claims') }} <el-badge v-if="badCount" :value="badCount" type="danger" class="tb" /></template>
              <div class="toolbar">
                <el-select v-model="filter.support" :placeholder="t('paper.allSupport')" clearable size="small" style="width: 150px">
                  <el-option v-for="k in Object.keys(SUPPORT)" :key="k" :value="k" :label="label(SUPPORT, k)" />
                </el-select>
                <el-select v-model="filter.section" :placeholder="t('paper.allSections')" clearable size="small" style="width: 130px">
                  <el-option v-for="s in sections" :key="s" :value="s" :label="label(SECTION, s)" />
                </el-select>
              </div>
              <PageState :loading="claims.loading.value && !claims.data.value" :error="claims.error.value" :empty="!rows.length"
                :empty-text="t('paper.noClaims')" @retry="claims.load()">
                <div v-for="c in filtered" :key="c.claim_id" :class="['claim', { bad: BAD.includes(c.support) }]" @click="openTrace(c.claim_id)">
                  <div class="row">
                    <b class="mono">{{ c.claim_id }}</b>
                    <el-tag size="small" effect="plain">{{ label(SECTION, c.section) }}</el-tag>
                    <StateTag v-if="c.support" kind="support" :value="c.support" size="small" />
                    <el-tag v-if="c.issues.length" type="danger" size="small" effect="plain">{{ t('paper.issues', { n: c.issues.length }) }}</el-tag>
                  </div>
                  <div class="text">{{ c.display }}</div>
                  <div v-for="(i, k) in c.issues" :key="k" class="issue">
                    <StateTag kind="severity" :value="i.severity" size="small" /> <span class="mono">{{ i.code }}</span> {{ i.detail }}
                  </div>
                </div>
                <div v-if="!filtered.length" class="muted">{{ t('paper.noMatch') }}</div>
              </PageState>
            </el-tab-pane>

            <el-tab-pane :label="t('paper.review')" name="review">
              <div v-if="paper.data.value?.review_summary" class="counts">
                <el-tag type="danger">{{ t('approval.blocker', { n: counts.blocker || 0 }) }}</el-tag>
                <el-tag type="warning">{{ t('approval.major', { n: counts.major || 0 }) }}</el-tag>
                <el-tag type="info">{{ t('approval.minor', { n: counts.minor || 0 }) }}</el-tag>
                <span class="muted mono small">{{ paper.data.value.review_summary.file }}</span>
              </div>
              <MarkdownView v-if="reviewMd" :source="reviewMd" />
              <el-empty v-else :description="t('paper.noReview')" />
            </el-tab-pane>

            <el-tab-pane :label="t('paper.compile')" name="compile">
              <template v-if="compile">
                <el-descriptions :column="2" border size="small" class="mb">
                  <el-descriptions-item :label="t('paper.result')"><el-tag :type="compile.ok ? 'success' : 'danger'">{{ compile.ok ? t('paper.ok') : t('paper.failed') }}</el-tag></el-descriptions-item>
                  <el-descriptions-item :label="t('paper.pages')">{{ compile.pages ?? '—' }}</el-descriptions-item>
                  <el-descriptions-item :label="t('paper.engine')"><span class="mono">{{ compile.engine }}</span></el-descriptions-item>
                  <el-descriptions-item :label="t('paper.entry')"><span class="mono">{{ compile.entry }}</span></el-descriptions-item>
                  <el-descriptions-item :label="t('paper.seconds')">{{ t('paper.secondsValue', { n: compile.seconds }) }}</el-descriptions-item>
                  <el-descriptions-item :label="t('paper.overfull')">{{ compile.overfull_boxes }}</el-descriptions-item>
                </el-descriptions>
                <h4>{{ t('paper.errors', { n: compile.errors.length }) }}</h4>
                <pre v-for="(e, i) in compile.errors" :key="i" class="code mono err">{{ typeof e === 'string' ? e : JSON.stringify(e, null, 2) }}</pre>
                <div v-if="!compile.errors.length" class="muted">{{ t('common.none') }}</div>
                <h4>{{ t('paper.warnings') }}</h4>
                <div>{{ t('paper.undefCite') }}<span class="mono">{{ compile.undefined_citations.join(', ') || t('common.none') }}</span></div>
                <div>{{ t('paper.undefRef') }}<span class="mono">{{ compile.undefined_refs.join(', ') || t('common.none') }}</span></div>
                <div>{{ t('paper.missingFiles') }}<span class="mono">{{ compile.missing_files.join(', ') || t('common.none') }}</span></div>
                <router-link :to="`/projects/${pid}/files?path=${encodeURIComponent(compile.log_path)}`" class="link">{{ t('paper.compileLog') }}</router-link>
              </template>
              <el-empty v-else :description="t('paper.noCompile')" />
            </el-tab-pane>
          </el-tabs>
        </el-card>
      </div>
    </PageState>

    <el-drawer v-model="trace.open" :title="t('paper.traceTitle', { id: trace.id })" size="50%">
      <div class="toolbar"><router-link :to="`/projects/${pid}/claims/${trace.id}`" class="link">{{ t('paper.openStandalone') }}</router-link></div>
      <el-skeleton v-if="trace.loading" :rows="6" animated />
      <el-result v-else-if="trace.error" icon="error" :title="t('paper.traceFailed')">
        <template #extra><el-button @click="openTrace(trace.id)">{{ t('common.retry') }}</el-button></template>
      </el-result>
      <EvidenceChain v-else-if="trace.data" :pid="pid" :trace="trace.data" />
    </el-drawer>
  </div>
</template>

<style scoped>
.cols { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(340px, 1fr); gap: 16px; align-items: start; }
@media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }
.ch { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.small { font-size: 12px; }
.opt { margin-left: 8px; }
.tb :deep(.el-badge__content) { position: static; transform: none; vertical-align: middle; }
.claim { padding: 10px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; margin-bottom: 8px; cursor: pointer; }
.claim:hover { background: var(--el-fill-color-light); }
.claim.bad { border-color: var(--el-color-danger-light-5); background: var(--el-color-danger-light-9); }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.text { margin-top: 6px; line-height: 1.6; }
.issue { margin-top: 4px; font-size: 13px; color: var(--el-color-danger); }
.counts { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.mb { margin-bottom: 12px; }
.err { color: var(--el-color-danger); margin-bottom: 6px; }
.link { color: var(--el-color-primary); }
</style>
