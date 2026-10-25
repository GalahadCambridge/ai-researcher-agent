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
    await ElMessageBox.confirm('确定取消这个运行吗？', '取消运行', { type: 'warning', confirmButtonText: '取消运行', cancelButtonText: '不取消' })
  } catch {
    return
  }
  cancelling.value = true
  try {
    await cancelRun(pid.value, rid.value, newRequestId())
    ElMessage.success('已请求取消')
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
    ElMessage.warning('请写一下原因')
    return
  }
  lbl.saving = true
  try {
    await labelRun(pid.value, rid.value, { label: lbl.label, reason: lbl.reason.trim(), request_id: lbl.requestId })
    ElMessage.success('已标注')
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
      <div class="page-title">
        <router-link :to="`/projects/${pid}/runs`"><el-button icon="ArrowLeft" text>运行列表</el-button></router-link>
        <h2><CopyText :text="rec.run_id" /></h2>
        <StateTag kind="run" :value="st.state" />
        <StateTag v-if="data.label" kind="runLabel" :value="data.label.label" />
        <span class="spacer" />
        <el-button icon="PriceTag" @click="openLabel">标注</el-button>
        <el-button v-if="cancellable" type="danger" plain icon="CircleClose" :loading="cancelling" @click="doCancel">取消运行</el-button>
      </div>

      <el-alert v-if="st.failure_reason" type="error" :closable="false" show-icon
        :title="`失败原因：${label(FAILURE, st.failure_reason)}`"
        :description="[st.exit_code != null ? `退出码 ${st.exit_code}` : '', st.error_class || ''].filter(Boolean).join('；')" />
      <el-alert v-if="data.label" :type="data.label.label === 'trusted' ? 'success' : 'warning'" :closable="false"
        :title="`人工标注：${label(RUN_LABEL, data.label.label)}${data.label.reason ? '，' + data.label.reason : ''}`"
        :description="data.label.label === 'invalid' ? '标为无效的运行不计入汇总结果' : data.label.label === 'suspicious' ? '标为可疑的运行计入汇总但会被标出' : ''" />

      <div class="grid">
        <el-card shadow="never">
          <template #header><b>基本信息</b></template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="实验">{{ rec.experiment_id }}（{{ label(RUN_KIND, rec.kind) }}）</el-descriptions-item>
            <el-descriptions-item label="任务键"><span class="mono">{{ rec.task_key }}</span></el-descriptions-item>
            <el-descriptions-item label="种子">{{ rec.seed ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="评价条件">{{ rec.eval_condition === 'final' ? '正式（final）' : '探索（explore）' }}</el-descriptions-item>
            <el-descriptions-item label="代码提交"><CopyText :text="rec.code_commit">{{ rec.code_commit.slice(0, 10) }}</CopyText></el-descriptions-item>
            <el-descriptions-item label="配置文件">
              <router-link :to="fileLink(`runs/${rec.run_id}/config.yaml`)" class="link">config.yaml</router-link>
              <span class="muted mono small"> sha256 {{ rec.config_sha256.slice(0, 12) }}…</span>
            </el-descriptions-item>
            <el-descriptions-item label="命令"><span class="mono small">{{ rec.command.join(' ') }}</span></el-descriptions-item>
            <el-descriptions-item label="依据">
              <span class="mono small">{{ rec.plan_ref.artifact_id }} v{{ rec.plan_ref.version }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="rec.retry_of" label="重试自">
              <router-link :to="`/projects/${pid}/runs/${rec.retry_of}`" class="link mono">{{ rec.retry_of }}</router-link>
              <span v-if="rec.repair_attempt" class="muted">（第 {{ rec.repair_attempt }} 次修复）</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="rec.inputs?.length" label="输入数据">
              <div v-for="i in rec.inputs" :key="i.name" class="mono small">{{ i.name }}：{{ i.path }}（{{ i.sha256.slice(0, 10) }}…）</div>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never">
          <template #header><b>状态</b></template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="状态"><StateTag kind="run" :value="st.state" size="small" /></el-descriptions-item>
            <el-descriptions-item label="创建"><TimeText :value="rec.created_at" /></el-descriptions-item>
            <el-descriptions-item label="开始"><TimeText :value="st.started_at" /></el-descriptions-item>
            <el-descriptions-item label="结束"><TimeText :value="st.ended_at" /></el-descriptions-item>
            <el-descriptions-item label="耗时">{{ duration(st.started_at, st.ended_at) || '—' }}</el-descriptions-item>
            <el-descriptions-item label="退出码">{{ st.exit_code ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="超时设置">{{ rec.timeout_s }} 秒</el-descriptions-item>
            <el-descriptions-item v-if="data.resources" label="资源">
              墙钟 {{ num(data.resources.wall_seconds, 2) }} 秒，CPU {{ num(data.resources.cpu_seconds, 2) }} 秒
              <template v-if="data.resources.max_rss_mb">，内存峰值 {{ num(data.resources.max_rss_mb, 1) }} MB</template>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </div>

      <el-card shadow="never">
        <template #header><b>指标</b><span class="muted small">（必需指标：{{ rec.required_metrics.join('、') || '无' }}）</span></template>
        <el-table v-if="data.metrics.length" :data="data.metrics" size="small" max-height="320">
          <el-table-column label="#" prop="seq" width="60" />
          <el-table-column label="指标" prop="name" />
          <el-table-column label="值"><template #default="{ row }"><span class="mono">{{ num(row.value, 6) }}</span></template></el-table-column>
          <el-table-column label="划分" prop="split" />
          <el-table-column label="步" prop="step" />
          <el-table-column label="时间" width="120"><template #default="{ row }"><TimeText :value="row.ts" /></template></el-table-column>
        </el-table>
        <div v-else class="muted">还没有上报指标</div>
      </el-card>

      <el-card shadow="never">
        <template #header><b>日志</b></template>
        <LogViewer :pid="pid" :run-id="rid" :active="active" @ended="load(true)" />
      </el-card>

      <el-collapse>
        <el-collapse-item title="环境摘要" name="env">
          <pre v-if="data.environment" class="code mono">{{ JSON.stringify(data.environment, null, 2) }}</pre>
          <div v-else class="muted">没有环境信息</div>
        </el-collapse-item>
      </el-collapse>

      <el-dialog v-model="lbl.open" title="人工标注运行" width="480px">
        <el-radio-group v-model="lbl.label">
          <el-radio value="trusted">可信</el-radio>
          <el-radio value="suspicious">可疑（计入汇总但标出）</el-radio>
          <el-radio value="invalid">无效（不计入汇总）</el-radio>
        </el-radio-group>
        <el-input v-model="lbl.reason" type="textarea" :rows="3" placeholder="原因（可疑、无效时必填）" class="mt" />
        <template #footer>
          <el-button @click="lbl.open = false">取消</el-button>
          <el-button type="primary" :loading="lbl.saving" @click="saveLabel">保存</el-button>
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
