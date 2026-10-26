<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import TimeText from '../components/TimeText.vue'
import CopyText from '../components/CopyText.vue'
import LogViewer from '../components/LogViewer.vue'
import { cancelRun, getRun, labelRun } from '../api/runs'
import { newRequestId } from '../api/client'
import { FAILURE, RUN_KIND, RUN_LABEL, label } from '../constants/labels'
import { duration, num } from '../utils/format'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P7 运行详情。运行处于活动状态时，除 SSE 外每 5 秒重新请求一次，确保状态不会停在旧值
const route = useRoute()
const { pid } = useProject()
const rid = computed(() => route.params.runId)
const { data, loading, error, load } = useLoader(() => getRun(pid.value, rid.value))
onMounted(load)
watch(rid, () => load())
useProjectRefresh(load, ['run'])

const ACTIVE = ['created', 'preparing', 'queued', 'running', 'unknown']
const active = computed(() => ACTIVE.includes(data.value?.status.state))
const cancellable = computed(() => ['queued', 'running'].includes(data.value?.status.state))
let timer = null
watch(active, (a) => {
  clearInterval(timer)
  if (a) timer = setInterval(() => load(true), 5000)
}, { immediate: true })
onBeforeUnmount(() => clearInterval(timer))

const rec = computed(() => data.value?.record)
const st = computed(() => data.value?.status)
const fileLink = (path) => `/projects/${pid.value}/files?path=${encodeURIComponent(path)}`

// ---------------------------------------------------------------- 取消
const cancelling = ref(false)
async function doCancel() {
  try {
    await ElMessageBox.confirm(t('run.cancelConfirm'), t('run.cancel'), { type: 'warning', confirmButtonText: t('run.cancel'), cancelButtonText: t('run.cancelNo') })
  } catch {
    return
  }
  cancelling.value = true
  try {
    await cancelRun(pid.value, rid.value, newRequestId())
    ElMessage.success(t('run.cancelRequested'))
    load(true)
  } finally {
    cancelling.value = false
  }
}

// ---------------------------------------------------------------- 人工标注
const lbl = reactive({ open: false, label: 'suspicious', reason: '', saving: false, requestId: '' })
function openLabel() {
  Object.assign(lbl, { open: true, label: data.value.label?.label || 'suspicious', reason: '', requestId: newRequestId() })
}
async function saveLabel() {
  if (lbl.saving) return
  if (lbl.label !== 'trusted' && !lbl.reason.trim()) {
    ElMessage.warning(t('run.needReason'))
    return
  }
  lbl.saving = true
  try {
    await labelRun(pid.value, rid.value, { label: lbl.label, reason: lbl.reason.trim(), request_id: lbl.requestId })
    ElMessage.success(t('run.labeled'))
    lbl.open = false
    load(true)
  } finally {
    lbl.saving = false
  }
}
</script>

<template>
  <PageState :loading="loading && !data" :error="error" @retry="load()">
    <div v-if="data" class="card-gap">
      <router-link :to="`/projects/${pid}/runs`" class="back"><el-icon><ArrowLeft /></el-icon>{{ t('run.backToList') }}</router-link>
      <div class="page-title">
        <h2><CopyText :text="rec.run_id" /></h2>
        <StateTag kind="run" :value="st.state" />
        <StateTag v-if="data.label" kind="runLabel" :value="data.label.label" />
        <span class="spacer" />
        <el-button icon="PriceTag" @click="openLabel">{{ t('run.label') }}</el-button>
        <el-button v-if="cancellable" type="danger" plain icon="CircleClose" :loading="cancelling" @click="doCancel">{{ t('run.cancel') }}</el-button>
      </div>

      <el-alert v-if="st.failure_reason" type="error" :closable="false" show-icon
        :title="t('run.failure', { reason: label(FAILURE, st.failure_reason) })"
        :description="[st.exit_code != null ? t('run.exitCode', { code: st.exit_code }) : '', st.error_class || ''].filter(Boolean).join('; ')" />
      <el-alert v-if="data.label" :type="data.label.label === 'trusted' ? 'success' : 'warning'" :closable="false"
        :title="t('run.humanLabel', { label: label(RUN_LABEL, data.label.label) }) + (data.label.reason ? ' — ' + data.label.reason : '')"
        :description="data.label.label === 'invalid' ? t('run.invalidHint') : data.label.label === 'suspicious' ? t('run.suspiciousHint') : ''" />

      <div class="grid">
        <el-card shadow="never">
          <template #header><b>{{ t('run.info') }}</b></template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item :label="t('run.experiment')">{{ rec.experiment_id }} ({{ label(RUN_KIND, rec.kind) }})</el-descriptions-item>
            <el-descriptions-item :label="t('run.taskKey')"><span class="mono">{{ rec.task_key }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('run.seed')">{{ rec.seed ?? '—' }}</el-descriptions-item>
            <el-descriptions-item :label="t('run.evalCondition')">{{ rec.eval_condition === 'final' ? t('run.evalFinal') : t('run.evalExplore') }}</el-descriptions-item>
            <el-descriptions-item :label="t('run.commit')"><CopyText :text="rec.code_commit">{{ rec.code_commit.slice(0, 10) }}</CopyText></el-descriptions-item>
            <el-descriptions-item :label="t('run.config')">
              <router-link :to="fileLink(`runs/${rec.run_id}/config.yaml`)" class="link">config.yaml</router-link>
              <span class="muted mono small"> sha256 {{ rec.config_sha256.slice(0, 12) }}…</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('run.command')"><span class="mono small">{{ rec.command.join(' ') }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('run.basis')">
              <span class="mono small">{{ rec.plan_ref.artifact_id }} v{{ rec.plan_ref.version }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="rec.retry_of" :label="t('run.retryOf')">
              <router-link :to="`/projects/${pid}/runs/${rec.retry_of}`" class="link mono">{{ rec.retry_of }}</router-link>
              <span v-if="rec.repair_attempt" class="muted">{{ t('run.repairAttempt', { n: rec.repair_attempt }) }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="rec.inputs?.length" :label="t('run.inputs')">
              <div v-for="i in rec.inputs" :key="i.name" class="mono small">{{ i.name }}：{{ i.path }}（{{ i.sha256.slice(0, 10) }}…）</div>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never">
          <template #header><b>{{ t('run.status') }}</b></template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item :label="t('run.status')"><StateTag kind="run" :value="st.state" size="small" /></el-descriptions-item>
            <el-descriptions-item :label="t('run.created')"><TimeText :value="rec.created_at" /></el-descriptions-item>
            <el-descriptions-item :label="t('run.started')"><TimeText :value="st.started_at" /></el-descriptions-item>
            <el-descriptions-item :label="t('run.ended')"><TimeText :value="st.ended_at" /></el-descriptions-item>
            <el-descriptions-item :label="t('run.duration')">{{ duration(st.started_at, st.ended_at) || '—' }}</el-descriptions-item>
            <el-descriptions-item :label="t('run.exit')">{{ st.exit_code ?? '—' }}</el-descriptions-item>
            <el-descriptions-item :label="t('run.timeout')">{{ t('run.timeoutValue', { n: rec.timeout_s }) }}</el-descriptions-item>
            <el-descriptions-item v-if="data.resources" :label="t('run.resources')">
              {{ t('run.resourcesValue', { wall: num(data.resources.wall_seconds, 2), cpu: num(data.resources.cpu_seconds, 2) }) }}<template v-if="data.resources.max_rss_mb">{{ t('run.rss', { mb: num(data.resources.max_rss_mb, 1) }) }}</template>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </div>

      <el-card shadow="never">
        <template #header><b>{{ t('run.metrics') }}</b><span class="muted small">{{ t('run.requiredMetrics', { names: rec.required_metrics.join(', ') || t('common.none') }) }}</span></template>
        <el-table v-if="data.metrics.length" :data="data.metrics" size="small" max-height="320">
          <el-table-column label="#" prop="seq" width="60" />
          <el-table-column :label="t('run.colMetric')" prop="name" />
          <el-table-column :label="t('run.colValue')"><template #default="{ row }"><span class="mono">{{ num(row.value, 6) }}</span></template></el-table-column>
          <el-table-column :label="t('run.colSplit')" prop="split" />
          <el-table-column :label="t('run.colStep')" prop="step" />
          <el-table-column :label="t('run.colTime')" width="120"><template #default="{ row }"><TimeText :value="row.ts" /></template></el-table-column>
        </el-table>
        <div v-else class="muted">{{ t('run.noMetrics') }}</div>
      </el-card>

      <el-card shadow="never">
        <template #header><b>{{ t('run.logs') }}</b></template>
        <LogViewer :pid="pid" :run-id="rid" :active="active" @ended="load(true)" />
      </el-card>

      <el-collapse>
        <el-collapse-item :title="t('run.env')" name="env">
          <pre v-if="data.environment" class="code mono">{{ JSON.stringify(data.environment, null, 2) }}</pre>
          <div v-else class="muted">{{ t('run.noEnv') }}</div>
        </el-collapse-item>
      </el-collapse>

      <el-dialog v-model="lbl.open" :title="t('run.labelTitle')" width="480px">
        <el-radio-group v-model="lbl.label">
          <el-radio value="trusted">{{ t('run.trusted') }}</el-radio>
          <el-radio value="suspicious">{{ t('run.suspicious') }}</el-radio>
          <el-radio value="invalid">{{ t('run.invalid') }}</el-radio>
        </el-radio-group>
        <el-input v-model="lbl.reason" type="textarea" :rows="3" :placeholder="t('run.reasonPh')" class="mt" />
        <template #footer>
          <el-button @click="lbl.open = false">{{ t('common.cancel') }}</el-button>
          <el-button type="primary" :loading="lbl.saving" @click="saveLabel">{{ t('common.save') }}</el-button>
        </template>
      </el-dialog>
    </div>
  </PageState>
</template>

<style scoped>
.page-title h2 { font-size: 17px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 16px; }
.small { font-size: 12px; }
.link { color: var(--el-color-primary); }
.mt { margin-top: 12px; }
</style>
