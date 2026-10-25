<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import TimeText from '../components/TimeText.vue'
import CopyText from '../components/CopyText.vue'
import { getRun, listRuns } from '../api/runs'
import { FAILURE, RUN, RUN_KIND, label } from '../constants/labels'
import { duration, getPref, num, setPref } from '../utils/format'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'

// P6 运行列表：可按实验 ID 和状态筛选。主要指标值要读每个运行的详情，只读当前页的
const router = useRouter()
const { pid } = useProject()
const { data, loading, error, load } = useLoader(() => listRuns(pid.value))
onMounted(load)
useProjectRefresh(load, ['run'])

const filter = reactive({ exp: '', state: '' })
const page = ref(1)
const pageSize = ref(getPref('runs.pageSize', 20))
watch(pageSize, (v) => setPref('runs.pageSize', v))

const experiments = computed(() => [...new Set((data.value || []).map((r) => r.experiment_id))].sort())
const states = computed(() => [...new Set((data.value || []).map((r) => r.state))])
const filtered = computed(() =>
  [...(data.value || [])]
    .filter((r) => (!filter.exp || r.experiment_id === filter.exp) && (!filter.state || r.state === filter.state))
    .sort((a, b) => ((a.created_at || '') < (b.created_at || '') ? 1 : -1)),
)
const pageRows = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
watch(() => [filter.exp, filter.state], () => (page.value = 1))

// 主要指标：required_metrics 的第一个，没有时取第一条指标
const metrics = ref({})
watch(pageRows, async (rows) => {
  const need = rows.filter((r) => r.state === 'succeeded' && !(r.run_id in metrics.value))
  const res = await Promise.allSettled(need.map((r) => getRun(pid.value, r.run_id)))
  const out = { ...metrics.value }
  res.forEach((x, i) => {
    if (x.status !== 'fulfilled') return
    const v = x.value
    const name = v.record.required_metrics?.[0] || v.metrics[0]?.name
    const m = [...v.metrics].reverse().find((m) => m.name === name)
    out[need[i].run_id] = m ? { name, value: m.value } : null
  })
  metrics.value = out
})

const open = (row) => router.push(`/projects/${pid.value}/runs/${row.run_id}`)
</script>

<template>
  <div>
    <div class="page-title">
      <h2>运行</h2>
      <span class="muted">共 {{ data?.length || 0 }} 个</span>
    </div>
    <el-card shadow="never">
      <div class="toolbar">
        <el-select v-model="filter.exp" placeholder="全部实验" clearable style="width: 160px">
          <el-option v-for="e in experiments" :key="e" :label="e" :value="e" />
        </el-select>
        <el-select v-model="filter.state" placeholder="全部状态" clearable style="width: 160px">
          <el-option v-for="s in states" :key="s" :label="label(RUN, s)" :value="s" />
        </el-select>
        <span class="spacer" />
        <el-button icon="Refresh" @click="load()">刷新</el-button>
      </div>
      <PageState :loading="loading && !data" :error="error" :empty="!filtered.length"
        :empty-text="data?.length ? '没有符合筛选条件的运行' : '还没有运行记录（批准实验计划后开始运行）'" @retry="load()">
        <el-table :data="pageRows" row-class-name="clickable-row" @row-click="open">
          <el-table-column label="运行编号" min-width="250">
            <template #default="{ row }"><CopyText :text="row.run_id" /></template>
          </el-table-column>
          <el-table-column label="实验" prop="experiment_id" width="80" />
          <el-table-column label="类型" width="90"><template #default="{ row }">{{ label(RUN_KIND, row.kind) }}</template></el-table-column>
          <el-table-column label="任务键" min-width="160"><template #default="{ row }"><span class="mono">{{ row.task_key }}</span></template></el-table-column>
          <el-table-column label="状态" width="170">
            <template #default="{ row }">
              <StateTag kind="run" :value="row.state" size="small" />
              <StateTag v-if="row.label" kind="runLabel" :value="row.label" size="small" class="ml" />
              <div v-if="row.failure_reason" class="fail">{{ label(FAILURE, row.failure_reason) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="100"><template #default="{ row }">{{ duration(row.started_at, row.ended_at) }}</template></el-table-column>
          <el-table-column label="主要指标" width="140">
            <template #default="{ row }">
              <span v-if="metrics[row.run_id]" class="mono">{{ metrics[row.run_id].name }}={{ num(metrics[row.run_id].value) }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="重试自" min-width="120">
            <template #default="{ row }">
              <router-link v-if="row.retry_of" :to="`/projects/${pid}/runs/${row.retry_of}`" class="mono link" @click.stop>{{ row.retry_of.slice(-4) }}</router-link>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="110"><template #default="{ row }"><TimeText :value="row.created_at" /></template></el-table-column>
        </el-table>
        <el-pagination v-if="filtered.length > pageSize" v-model:current-page="page" v-model:page-size="pageSize"
          :total="filtered.length" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" class="pager" />
      </PageState>
    </el-card>
  </div>
</template>

<style scoped>
.ml { margin-left: 4px; }
.fail { font-size: 12px; color: var(--el-color-danger); }
.link { color: var(--el-color-primary); }
.pager { margin-top: 12px; justify-content: flex-end; }
</style>
