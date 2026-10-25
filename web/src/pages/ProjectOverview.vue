<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import StateTag from '../components/StateTag.vue'
import BudgetBar from '../components/BudgetBar.vue'
import QuestionCard from '../components/QuestionCard.vue'
import EmptyState from '../components/EmptyState.vue'
import TimeText from '../components/TimeText.vue'
import { exportUrl, listCheckpoints, listEvents, patchBudget, projectAction, rollback } from '../api/projects'
import { newRequestId } from '../api/client'
import { BUDGET, KIND, RUN_KIND, STATE, label } from '../constants/labels'
import { duration } from '../utils/format'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'

// P3 项目总览：现在在干什么、卡在哪里、我需要做什么（详细设计 5 第 4.3 节）。
// 按钮是否出现只看后台给的 next_actions，网页不自己判断状态。
const router = useRouter()
const ctx = useProject()
const pid = ctx.pid
const st = computed(() => ctx.status.value)

// ---------------------------------------------------------------- 最近动态（最近 20 条，新的在上）
const events = useLoader(async () => {
  const all = await listEvents(pid.value, { limit: 100000 })
  return all.slice(-20).reverse()
})
onMounted(() => events.load())
useProjectRefresh(events.load, ['event'])

// ---------------------------------------------------------------- 按钮
const actions = computed(() => st.value?.next_actions || [])
const can = (a) => actions.value.includes(a)
const firstApproval = computed(() => st.value?.pending_approvals?.[0])
// 后台的 next_actions 不含 cancel；取消规则同引擎：已完成、已出错停止的项目不能取消
const canCancel = computed(() => st.value && !['Completed', 'Failed'].includes(st.value.state))
const busy = ref('')

const ACTION_TEXT = { pause: '已暂停', resume: '已继续', cancel: '项目已取消', reopen: '已重新打开，回到撰写论文' }
async function act(action) {
  if (busy.value) return
  if (action === 'cancel') {
    try {
      await ElMessageBox.confirm('取消后项目会停止，待审批和待回答的问题都会作废（已有文件不会删除）。确定取消吗？', '取消项目',
        { type: 'warning', confirmButtonText: '确定取消项目', cancelButtonText: '不取消' })
    } catch {
      return
    }
  }
  busy.value = action
  try {
    ctx.status.value = await projectAction(pid.value, action, newRequestId())
    ElMessage.success(ACTION_TEXT[action])
  } catch {
    ctx.refresh()
  } finally {
    busy.value = ''
  }
}

// ---------------------------------------------------------------- 提示条
const alertType = computed(() => {
  const s = st.value?.state
  if (['Paused', 'BudgetExhausted', 'Failed'].includes(s) && !st.value.pending_approvals.length && !st.value.pending_question) return 'error'
  return 'warning'
})
const progress = computed(() => {
  const p = st.value?.progress
  if (!p || !p.total) return null
  return { pct: Math.round((p.done / p.total) * 100), text: `${p.done} / ${p.total}${p.unit ? ' ' + p.unit : ''}` }
})

// ---------------------------------------------------------------- 预算
const budgetCats = computed(() => {
  const cats = st.value?.budget?.categories || {}
  // 有上限或已经用了的才显示
  return Object.entries(cats).filter(([, c]) => c.hard != null || c.soft != null || c.used > 0)
})
const budgetDlg = reactive({ open: false, saving: false, rows: [] })
function openBudget() {
  const cats = st.value?.budget?.categories || {}
  const names = new Set([...Object.keys(BUDGET), ...Object.keys(cats)])
  budgetDlg.rows = [...names].map((name) => ({ name, used: cats[name]?.used ?? 0, soft: cats[name]?.soft ?? null, hard: cats[name]?.hard ?? null }))
  budgetDlg.open = true
}
async function saveBudget() {
  const cats = st.value?.budget?.categories || {}
  const body = { request_id: newRequestId() }
  for (const r of budgetDlg.rows) {
    const old = cats[r.name] || {}
    if ((old.soft ?? null) !== r.soft || (old.hard ?? null) !== r.hard) body[r.name] = { soft: r.soft, hard: r.hard }
  }
  if (Object.keys(body).length === 1) {
    budgetDlg.open = false
    return
  }
  budgetDlg.saving = true
  try {
    await patchBudget(pid.value, body)
    ElMessage.success('预算上限已更新' + (st.value?.state === 'BudgetExhausted' ? '，点“继续”即可恢复运行' : ''))
    budgetDlg.open = false
    ctx.refresh()
  } finally {
    budgetDlg.saving = false
  }
}

// ---------------------------------------------------------------- 检查点与回滚（可选功能）
const ck = reactive({ open: false, loading: false, rows: [], busy: '' })
async function openCheckpoints() {
  ck.open = true
  ck.loading = true
  try {
    ck.rows = await listCheckpoints(pid.value)
  } catch {
    ck.rows = []
  } finally {
    ck.loading = false
  }
}
async function doRollback(row) {
  try {
    await ElMessageBox.confirm(`回到 ${row.checkpoint_id}（状态：${label(STATE, row.state)}）？不会删除任何文件或运行；回滚后点“继续”从那里重新开始。`,
      '回滚', { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' })
  } catch {
    return
  }
  ck.busy = row.checkpoint_id
  try {
    ctx.status.value = await rollback(pid.value, row.checkpoint_id, newRequestId())
    ElMessage.success('已回滚')
    ck.open = false
  } finally {
    ck.busy = ''
  }
}

const EVENT_COLOR = (t) => (t.includes('fail') || t.includes('error') ? 'danger' : t.startsWith('approval') ? 'warning' : t.startsWith('run') ? 'primary' : '')
</script>

<template>
  <div v-if="!st"><el-skeleton :rows="8" animated /></div>
  <div v-else class="card-gap">
    <!-- 1. 标题栏 -->
    <el-card shadow="never">
      <div class="head">
        <div class="htext">
          <div class="row">
            <h2>{{ st.title }}</h2>
            <StateTag :value="st.state" />
          </div>
          <div class="muted stage">{{ st.stage_label }}<span v-if="progress">（{{ progress.text }}）</span></div>
          <el-progress v-if="progress" :percentage="progress.pct" class="prog" />
        </div>
        <div class="btns">
          <el-button v-if="can('pause')" icon="VideoPause" :loading="busy === 'pause'" @click="act('pause')">暂停</el-button>
          <el-button v-if="can('resume')" type="primary" icon="VideoPlay" :loading="busy === 'resume'" @click="act('resume')">继续</el-button>
          <el-button v-if="can('reopen')" icon="EditPen" :loading="busy === 'reopen'" @click="act('reopen')">重新打开（修改论文）</el-button>
          <el-button v-if="canCancel" type="danger" plain icon="CircleClose" :loading="busy === 'cancel'" @click="act('cancel')">取消项目</el-button>
          <el-dropdown trigger="click">
            <el-button icon="Download">导出<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <a :href="exportUrl(pid, 'paper')" class="dl"><el-dropdown-item>论文交付包（zip）</el-dropdown-item></a>
                <a :href="exportUrl(pid, 'archive')" class="dl"><el-dropdown-item>完整研究档案（zip）</el-dropdown-item></a>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button text icon="Clock" @click="openCheckpoints">检查点</el-button>
        </div>
      </div>
    </el-card>

    <!-- 2. 提示条 -->
    <el-alert v-if="st.blocking_reason" :type="alertType" :closable="false" show-icon class="block">
      <template #title>
        <div class="block-row">
          <span class="block-text">{{ st.blocking_reason }}</span>
          <router-link v-if="firstApproval" :to="`/projects/${pid}/approvals/${firstApproval.approval_id}`">
            <el-button type="warning" size="small">去审批</el-button>
          </router-link>
          <el-button v-else-if="can('resume')" type="primary" size="small" :loading="busy === 'resume'" @click="act('resume')">继续</el-button>
          <el-button v-if="st.state === 'BudgetExhausted'" size="small" @click="openBudget">调整预算上限</el-button>
        </div>
      </template>
    </el-alert>
    <el-alert v-else-if="st.state === 'Completed'" type="success" :closable="false" show-icon
      title="项目已完成。可以在“论文”页查看最终 PDF，或点右上角“导出”。" />

    <!-- 3. 问题卡片 -->
    <QuestionCard v-if="st.pending_question" :pid="pid" :question="st.pending_question" @answered="ctx.refresh()" />

    <!-- 待审批（多个时逐个列出） -->
    <el-card v-if="st.pending_approvals.length" shadow="never">
      <template #header><b>等你审批（{{ st.pending_approvals.length }}）</b></template>
      <div v-for="a in st.pending_approvals" :key="a.approval_id" class="appr" @click="router.push(`/projects/${pid}/approvals/${a.approval_id}`)">
        <el-tag :type="(KIND[a.kind] || [, 'info'])[1]">{{ label(KIND, a.kind) }}</el-tag>
        <span class="mono">{{ a.target.artifact_id }} v{{ a.target.version }}</span>
        <span class="sum">{{ a.summary }}</span>
        <TimeText :value="a.created_at" relative />
        <el-button type="primary" size="small" link>去审批 →</el-button>
      </div>
    </el-card>

    <div class="grid">
      <!-- 4. 预算 -->
      <el-card shadow="never">
        <template #header>
          <div class="ch"><b>预算</b><el-button size="small" text type="primary" @click="openBudget">调整上限</el-button></div>
        </template>
        <BudgetBar v-for="[name, cat] in budgetCats" :key="name" :name="name" :cat="cat" />
        <EmptyState v-if="!budgetCats.length" text="没有设置预算" />
      </el-card>

      <!-- 5. 进行中的运行 -->
      <el-card shadow="never">
        <template #header>
          <div class="ch">
            <b>进行中的运行<span v-if="st.active_runs.length">（{{ st.active_runs.length }}）</span></b>
            <router-link :to="`/projects/${pid}/runs`"><el-button size="small" text type="primary">全部运行</el-button></router-link>
          </div>
        </template>
        <div v-for="r in st.active_runs.slice(0, 5)" :key="r.run_id" class="run" @click="router.push(`/projects/${pid}/runs/${r.run_id}`)">
          <StateTag kind="run" :value="r.state" size="small" />
          <span class="mono">{{ r.task_key }}</span>
          <span class="muted">{{ label(RUN_KIND, r.kind) }}</span>
          <span class="spacer" />
          <span class="muted">{{ duration(r.started_at, r.ended_at) }}</span>
        </div>
        <EmptyState v-if="!st.active_runs.length" text="现在没有正在跑的实验" />
      </el-card>
    </div>

    <!-- 6. 最近动态 -->
    <el-card shadow="never">
      <template #header>
        <div class="ch">
          <b>最近动态</b>
          <router-link :to="`/projects/${pid}/events`"><el-button size="small" text type="primary">查看全部</el-button></router-link>
        </div>
      </template>
      <el-skeleton v-if="events.loading.value && !events.data.value" :rows="4" animated />
      <el-timeline v-else-if="events.data.value?.length">
        <el-timeline-item v-for="e in events.data.value" :key="e.seq" :type="EVENT_COLOR(e.type)" :timestamp="new Date(e.ts).toLocaleString()" placement="top">
          <span>{{ e.summary }}</span>
          <span class="muted mono etype">{{ e.type }}</span>
        </el-timeline-item>
      </el-timeline>
      <el-result v-else-if="events.error.value" icon="error" title="动态加载失败">
        <template #extra><el-button @click="events.load()">重试</el-button></template>
      </el-result>
      <EmptyState v-else text="还没有动态" />
    </el-card>

    <!-- 调整预算对话框 -->
    <el-dialog v-model="budgetDlg.open" title="调整预算上限" width="620px">
      <p class="muted">“提醒”到了会提示你，“上限”到了项目会自动暂停。留空表示不限。</p>
      <el-table :data="budgetDlg.rows" size="small">
        <el-table-column label="类别" min-width="150"><template #default="{ row }">{{ label(BUDGET, row.name) }}</template></el-table-column>
        <el-table-column label="已用" width="90"><template #default="{ row }"><span class="mono">{{ +row.used.toFixed(4) }}</span></template></el-table-column>
        <el-table-column label="提醒" width="150">
          <template #default="{ row }"><el-input-number v-model="row.soft" :min="0" size="small" controls-position="right" :value-on-clear="null" /></template>
        </el-table-column>
        <el-table-column label="上限" width="150">
          <template #default="{ row }"><el-input-number v-model="row.hard" :min="0" size="small" controls-position="right" :value-on-clear="null" /></template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="budgetDlg.open = false">取消</el-button>
        <el-button type="primary" :loading="budgetDlg.saving" @click="saveBudget">保存</el-button>
      </template>
    </el-dialog>

    <!-- 检查点抽屉 -->
    <el-drawer v-model="ck.open" title="检查点与回滚" size="560px">
      <el-alert type="info" :closable="false" show-icon class="mb"
        title="回滚只改变“流程走到哪一步”，不删除任何产物或运行。项目正在工作时请先暂停。" />
      <el-table v-loading="ck.loading" :data="ck.rows" size="small">
        <el-table-column label="检查点" width="120"><template #default="{ row }"><span class="mono">{{ row.checkpoint_id }}</span></template></el-table-column>
        <el-table-column label="状态"><template #default="{ row }"><StateTag :value="row.state" size="small" /><el-tag v-if="row.milestone" size="small" type="success" effect="plain" class="ml">里程碑</el-tag></template></el-table-column>
        <el-table-column label="时间" width="110"><template #default="{ row }"><TimeText :value="row.saved_at" /></template></el-table-column>
        <el-table-column width="80">
          <template #default="{ row }"><el-button size="small" link type="primary" :loading="ck.busy === row.checkpoint_id" @click="doRollback(row)">回滚</el-button></template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped>
.head { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.htext { flex: 1; min-width: 280px; }
.row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.row h2 { margin: 0; font-size: 20px; }
.stage { margin-top: 6px; }
.prog { margin-top: 8px; max-width: 420px; }
.btns { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.btns .el-button { margin-left: 0; }
.dl { text-decoration: none; }
.block-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.block-text { font-size: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }
.ch { display: flex; align-items: center; justify-content: space-between; }
.appr, .run { display: flex; align-items: center; gap: 10px; padding: 8px 4px; cursor: pointer; border-radius: 6px; flex-wrap: wrap; }
.appr:hover, .run:hover { background: var(--el-fill-color-light); }
.sum { flex: 1; min-width: 200px; }
.etype { margin-left: 8px; font-size: 12px; }
.mb { margin-bottom: 12px; }
.ml { margin-left: 6px; }
</style>
