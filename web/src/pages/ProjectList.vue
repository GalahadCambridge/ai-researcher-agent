<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import TimeText from '../components/TimeText.vue'
import { getProject, listProjects } from '../api/projects'
import { useLoader } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t, locale } = useI18n()

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

// 阶段说明由后台按界面语言返回，换语言时重新取
watch(locale, () => load(true))

const open = (row) => router.push(`/projects/${row.project_id}`)
</script>

<template>
  <div class="page">
    <div class="page-title">
      <h2>{{ t('list.title') }}</h2>
      <span class="spacer" />
      <el-button icon="Refresh" @click="load()">{{ t('common.refresh') }}</el-button>
      <router-link to="/projects/new"><el-button type="primary" icon="Plus">{{ t('nav.newProject') }}</el-button></router-link>
    </div>
    <el-card shadow="never">
      <PageState :loading="loading && !data" :error="error" :empty="data?.length === 0"
        :empty-text="t('list.empty')" @retry="load()">
        <el-table :data="data || []" row-class-name="clickable-row" @row-click="open">
          <el-table-column :label="t('list.colTitle')" min-width="260">
            <template #default="{ row }">
              <div class="title">{{ row.title }}</div>
              <div class="muted mono small">{{ row.project_id }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('list.colState')" width="190">
            <template #default="{ row }"><StateTag :value="row.state" /></template>
          </el-table-column>
          <el-table-column :label="t('list.colStage')" prop="stage_label" min-width="180" />
          <el-table-column :label="t('list.colTodo')" width="170">
            <template #default="{ row }">
              <el-tag v-if="row.pending_approvals" type="danger" size="small">{{ t('list.pendingApprovals', { n: row.pending_approvals }) }}</el-tag>
              <el-tag v-if="row.has_question" type="warning" size="small" class="q">{{ t('list.hasQuestion') }}</el-tag>
              <span v-if="!row.pending_approvals && !row.has_question" class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('list.colBudget')" width="140">
            <template #default="{ row }">
              <el-progress v-if="budgets[row.project_id] != null" :percentage="Math.min(100, Math.round(budgets[row.project_id]))"
                :status="budgets[row.project_id] >= 100 ? 'exception' : budgets[row.project_id] >= 80 ? 'warning' : ''" />
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('list.colUpdated')" width="120">
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
