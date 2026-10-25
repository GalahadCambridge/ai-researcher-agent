<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import TimeText from '../components/TimeText.vue'
import { getProject, listProjects } from '../api/projects'
import { useLoader } from '../utils/useLoader'

// P1 项目列表。预算百分比要读每个项目的状态，取各类预算里用得最多的一项
const router = useRouter()
const budgets = ref({})

const { data, loading, error, load } = useLoader(async () => {
  const rows = await listProjects()
  const res = await Promise.allSettled(rows.map((r) => getProject(r.project_id)))
  const out = {}
  res.forEach((r, i) => {
    if (r.status !== 'fulfilled') return
    let best = null
    for (const c of Object.values(r.value.budget.categories)) {
      const lim = c.hard ?? c.soft
      if (!lim) continue
      const pct = (c.used / lim) * 100
      if (best === null || pct > best) best = pct
    }
    out[rows[i].project_id] = best
  })
  budgets.value = out
  return rows
})

let timer = null
onMounted(() => { load(); timer = setInterval(() => load(true), 10000) })
onBeforeUnmount(() => clearInterval(timer))

const open = (row) => router.push(`/projects/${row.project_id}`)
</script>

<template>
  <div class="page">
    <div class="page-title">
      <h2>项目列表</h2>
      <span class="spacer" />
      <el-button icon="Refresh" @click="load()">刷新</el-button>
      <router-link to="/projects/new"><el-button type="primary" icon="Plus">新建项目</el-button></router-link>
    </div>
    <el-card shadow="never">
      <PageState :loading="loading && !data" :error="error" :empty="data?.length === 0"
        empty-text="还没有项目。点右上角“新建项目”，或在终端用 air new 创建" @retry="load()">
        <el-table :data="data || []" row-class-name="clickable-row" @row-click="open">
          <el-table-column label="标题" min-width="260">
            <template #default="{ row }">
              <div class="title">{{ row.title }}</div>
              <div class="muted mono small">{{ row.project_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="150">
            <template #default="{ row }"><StateTag :value="row.state" /></template>
          </el-table-column>
          <el-table-column label="阶段说明" prop="stage_label" min-width="180" />
          <el-table-column label="待办" width="170">
            <template #default="{ row }">
              <el-tag v-if="row.pending_approvals" type="danger" effect="dark" size="small">{{ row.pending_approvals }} 个待审批</el-tag>
              <el-tag v-if="row.has_question" type="warning" effect="dark" size="small" class="q">有问题待回答</el-tag>
              <span v-if="!row.pending_approvals && !row.has_question" class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="预算使用" width="140">
            <template #default="{ row }">
              <el-progress v-if="budgets[row.project_id] != null" :percentage="Math.min(100, Math.round(budgets[row.project_id]))"
                :status="budgets[row.project_id] >= 100 ? 'exception' : budgets[row.project_id] >= 80 ? 'warning' : ''" />
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="最后更新" width="120">
            <template #default="{ row }"><TimeText :value="row.updated_at || row.created_at" relative /></template>
          </el-table-column>
        </el-table>
      </PageState>
    </el-card>
  </div>
</template>

<style scoped>
.title { font-weight: 500; }
.small { font-size: 12px; }
.q { margin-left: 4px; }
</style>
