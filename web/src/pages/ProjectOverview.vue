<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import StateTag from '../components/StateTag.vue'
import BudgetBar from '../components/BudgetBar.vue'
import QuestionCard from '../components/QuestionCard.vue'
import TimeText from '../components/TimeText.vue'
import { exportUrl, latestEvents, listCheckpoints, patchBudget, projectAction, rollback } from '../api/projects'
import { newRequestId } from '../api/client'
import { BUDGET, KIND, RUN_KIND, STATE, label } from '../constants/labels'
import { duration } from '../utils/format'
import { eventText } from '../utils/events'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P3 项目总览：现在在干什么、卡在哪里、我需要做什么（详细设计 5 第 4.3 节）。
// 按钮是否出现只看后台给的 next_actions，网页不自己判断状态。
const router = useRouter()
const ctx = useProject()
const pid = ctx.pid
const st = computed(() => ctx.status.value)

// ---------------------------------------------------------------- 最近动态（最近 20 条，新的在上）
const events = useLoader(async () => (await latestEvents(pid.value, 20)).reverse())
onMounted(() => events.load())
useProjectRefresh(events.load, ['event'])

// ---------------------------------------------------------------- 按钮
const actions = computed(() => st.value?.next_actions || [])
const can = (a) => actions.value.includes(a)
const firstApproval = computed(() => st.value?.pending_approvals?.[0])
// 后台的 next_actions 不含 cancel；取消规则同引擎：已完成、已出错停止的项目不能取消
const canCancel = computed(() => st.value && !['Completed', 'Failed'].includes(st.value.state))
const busy = ref('')

async function act(action) {
  if (busy.value) return
  if (action === 'cancel') {
    try {
      await ElMessageBox.confirm(t('overview.cancelConfirm'), t('overview.cancelTitle'),
        { type: 'warning', confirmButtonText: t('overview.cancelOk'), cancelButtonText: t('overview.cancelNo') })
    } catch {
      return
    }
  }
  busy.value = action
  try {
    ctx.status.value = await projectAction(pid.value, action, newRequestId())
    ElMessage.success(t(`overview.actionDone.${action}`))
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
    ElMessage.success(t(st.value?.state === 'BudgetExhausted' ? 'overview.budgetSavedResume' : 'overview.budgetSaved'))
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
    await ElMessageBox.confirm(t('overview.rollbackConfirm', { id: row.checkpoint_id, state: label(STATE, row.state) }),
      t('overview.rollback'), { type: 'warning', confirmButtonText: t('overview.rollback'), cancelButtonText: t('common.cancel') })
  } catch {
    return
  }
  ck.busy = row.checkpoint_id
  try {
    ctx.status.value = await rollback(pid.value, row.checkpoint_id, newRequestId())
    ElMessage.success(t('overview.rolledBack'))
    ck.open = false
  } finally {
    ck.busy = ''
  }
}

const EVENT_COLOR = (t) => (t.includes('fail') || t.includes('error') ? 'danger' : t.startsWith('approval') ? 'warning' : t.startsWith('run') ? 'primary' : '')
</script>

<template>
  <div v-if="!st"><el-skeleton :rows="8" animated /></div>
  <div v-else class="overview">
    <!-- 1. 标题：状态、阶段说明、进度；右侧按钮（一个区域只有一个主按钮） -->
    <header class="hero">
      <div class="htext">
        <div class="eyebrow">
          <StateTag :value="st.state" size="small" />
          <span class="meta">{{ st.stage_label }}<template v-if="progress"> · {{ progress.text }}</template></span>
        </div>
        <h1 class="title">{{ st.title }}</h1>
        <el-progress v-if="progress" :percentage="progress.pct" :show-text="false" :stroke-width="4" class="prog" />
      </div>
      <div class="btns">
        <el-button v-if="can('resume')" type="primary" icon="VideoPlay" :loading="busy === 'resume'" @click="act('resume')">{{ t('overview.resume') }}</el-button>
        <el-button v-if="can('pause')" icon="VideoPause" :loading="busy === 'pause'" @click="act('pause')">{{ t('overview.pause') }}</el-button>
        <el-button v-if="can('reopen')" icon="EditPen" :loading="busy === 'reopen'" @click="act('reopen')">{{ t('overview.reopen') }}</el-button>
        <el-dropdown trigger="click">
          <el-button icon="Download">{{ t('overview.export') }}</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <a :href="exportUrl(pid, 'paper')" class="dl"><el-dropdown-item>{{ t('overview.exportPaper') }}</el-dropdown-item></a>
              <a :href="exportUrl(pid, 'archive')" class="dl"><el-dropdown-item>{{ t('overview.exportArchive') }}</el-dropdown-item></a>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-dropdown trigger="click" @command="(c) => (c === 'cancel' ? act('cancel') : openCheckpoints())">
          <el-button icon="MoreFilled" class="more" :title="t('overview.more')" />
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="checkpoints" icon="Clock">{{ t('overview.checkpoints') }}</el-dropdown-item>
              <el-dropdown-item v-if="canCancel" command="cancel" icon="CircleClose" divided class="danger-item">{{ t('overview.cancelProject') }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <!-- 2. 下一步：卡在哪里、需要你做什么 -->
    <div v-if="st.blocking_reason" :class="['callout', alertType === 'error' ? 'stop' : 'todo']">
      <el-icon size="18" class="callout-icon"><component :is="alertType === 'error' ? 'Warning' : 'Stamp'" /></el-icon>
      <div class="callout-text">
        <div class="callout-label">{{ alertType === 'error' ? t('overview.stopped') : t('overview.nextStep') }}</div>
        <div>{{ st.blocking_reason }}</div>
      </div>
      <div class="callout-actions">
        <el-button v-if="st.state === 'BudgetExhausted'" @click="openBudget">{{ t('overview.adjustBudget') }}</el-button>
        <router-link v-if="firstApproval" :to="`/projects/${pid}/approvals/${firstApproval.approval_id}`">
          <el-button type="primary">{{ t('overview.goApprove') }}</el-button>
        </router-link>
        <el-button v-else-if="can('resume')" type="primary" :loading="busy === 'resume'" @click="act('resume')">{{ t('overview.resume') }}</el-button>
      </div>
    </div>
    <div v-else-if="st.state === 'Completed'" class="callout done">
      <el-icon size="18" class="callout-icon"><Check /></el-icon>
      <div class="callout-text">
        <div class="callout-label">{{ t('overview.done') }}</div>
        <div>{{ t('overview.doneHint') }}</div>
      </div>
    </div>

    <!-- 3. 问题卡片 -->
    <QuestionCard v-if="st.pending_question" :pid="pid" :question="st.pending_question" @answered="ctx.refresh()" />

    <!-- 待审批（多个时逐个列出） -->
    <section v-if="st.pending_approvals.length" class="sec">
      <div class="sec-head"><h3 class="section-title">{{ t('overview.pendingApprovals') }}</h3><span class="meta">{{ t('overview.items', { n: st.pending_approvals.length }) }}</span></div>
      <div class="panel">
        <div v-for="a in st.pending_approvals" :key="a.approval_id" class="row-item" @click="router.push(`/projects/${pid}/approvals/${a.approval_id}`)">
          <StateTag kind="kind" :value="a.kind" size="small" />
          <div class="row-main">
            <div>{{ a.summary }}</div>
            <div class="meta"><span class="mono">{{ a.target.artifact_id }} v{{ a.target.version }}</span> · <TimeText :value="a.created_at" relative /></div>
          </div>
          <el-icon class="chev"><ArrowRight /></el-icon>
        </div>
      </div>
    </section>

    <div class="grid">
      <!-- 4. 预算 -->
      <section class="sec">
        <div class="sec-head"><h3 class="section-title">{{ t('overview.budget') }}</h3><el-button size="small" text @click="openBudget">{{ t('overview.adjustLimits') }}</el-button></div>
        <div class="panel pad">
          <BudgetBar v-for="[name, cat] in budgetCats" :key="name" :name="name" :cat="cat" />
          <div v-if="!budgetCats.length" class="meta">{{ t('overview.noBudget') }}</div>
        </div>
      </section>

      <!-- 5. 进行中的运行 -->
      <section class="sec">
        <div class="sec-head">
          <h3 class="section-title">{{ t('overview.activeRuns') }}</h3>
          <router-link :to="`/projects/${pid}/runs`"><el-button size="small" text>{{ t('overview.allRuns') }}</el-button></router-link>
        </div>
        <div class="panel">
          <div v-for="r in st.active_runs.slice(0, 5)" :key="r.run_id" class="row-item" @click="router.push(`/projects/${pid}/runs/${r.run_id}`)">
            <StateTag kind="run" :value="r.state" size="small" />
            <div class="row-main"><span class="mono">{{ r.task_key }}</span> <span class="meta">{{ label(RUN_KIND, r.kind) }}</span></div>
            <span class="meta">{{ duration(r.started_at, r.ended_at) }}</span>
          </div>
          <div v-if="!st.active_runs.length" class="empty meta">{{ t('overview.noActiveRuns') }}</div>
        </div>
      </section>
    </div>

    <!-- 6. 最近动态 -->
    <section class="sec">
      <div class="sec-head">
        <h3 class="section-title">{{ t('overview.recent') }}</h3>
        <router-link :to="`/projects/${pid}/events`"><el-button size="small" text>{{ t('common.viewAll') }}</el-button></router-link>
      </div>
      <div class="panel">
        <el-skeleton v-if="events.loading.value && !events.data.value" :rows="4" animated class="pad" />
        <template v-else-if="events.data.value?.length">
          <div v-for="e in events.data.value" :key="e.seq" class="event">
            <span :class="['dot', EVENT_COLOR(e.type)]" />
            <span class="ev-text">{{ eventText(e) }}</span>
            <span class="meta ev-time"><TimeText :value="e.ts" relative /></span>
          </div>
        </template>
        <div v-else-if="events.error.value" class="empty">
          <span class="meta">{{ t('overview.recentFailed') }}</span> <el-button size="small" text @click="events.load()">{{ t('common.retry') }}</el-button>
        </div>
        <div v-else class="empty meta">{{ t('overview.noEvents') }}</div>
      </div>
    </section>

    <!-- 调整预算对话框 -->
    <el-dialog v-model="budgetDlg.open" :title="t('overview.budgetTitle')" width="620px">
      <p class="muted">{{ t('overview.budgetHint') }}</p>
      <el-table :data="budgetDlg.rows" size="small">
        <el-table-column :label="t('newProject.colCategory')" min-width="150"><template #default="{ row }">{{ label(BUDGET, row.name) }}</template></el-table-column>
        <el-table-column :label="t('overview.colUsed')" width="90"><template #default="{ row }"><span class="mono">{{ +row.used.toFixed(4) }}</span></template></el-table-column>
        <el-table-column :label="t('newProject.colSoft')" width="150">
          <template #default="{ row }"><el-input-number v-model="row.soft" :min="0" size="small" controls-position="right" :value-on-clear="null" /></template>
        </el-table-column>
        <el-table-column :label="t('newProject.colHard')" width="150">
          <template #default="{ row }"><el-input-number v-model="row.hard" :min="0" size="small" controls-position="right" :value-on-clear="null" /></template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="budgetDlg.open = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="budgetDlg.saving" @click="saveBudget">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 检查点抽屉 -->
    <el-drawer v-model="ck.open" :title="t('overview.checkpoints')" size="560px">
      <el-alert type="info" :closable="false" show-icon class="mb"
        :title="t('overview.ckHint')" />
      <el-table v-loading="ck.loading" :data="ck.rows" size="small">
        <el-table-column :label="t('overview.colCheckpoint')" width="120"><template #default="{ row }"><span class="mono">{{ row.checkpoint_id }}</span></template></el-table-column>
        <el-table-column :label="t('approvals.colStatus')"><template #default="{ row }"><StateTag :value="row.state" size="small" /><el-tag v-if="row.milestone" size="small" type="success" effect="plain" class="ml">{{ t('overview.milestone') }}</el-tag></template></el-table-column>
        <el-table-column :label="t('overview.colTime')" width="110"><template #default="{ row }"><TimeText :value="row.saved_at" /></template></el-table-column>
        <el-table-column width="80">
          <template #default="{ row }"><el-button size="small" link type="primary" :loading="ck.busy === row.checkpoint_id" @click="doRollback(row)">{{ t('overview.rollback') }}</el-button></template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped>
.overview > * + * { margin-top: 28px; }
.hero { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
.htext { flex: 1; min-width: 300px; }
.eyebrow { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.title { margin: 0; font-size: 26px; font-weight: 600; letter-spacing: -0.015em; line-height: 1.3; }
.prog { margin-top: 14px; max-width: 420px; }
.btns { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.btns .el-button { margin-left: 0; }
.more { padding: 8px 10px; }
.dl { text-decoration: none; }
:global(.el-dropdown-menu__item.danger-item) { color: var(--air-danger); }

.callout {
  display: flex; align-items: center; gap: 14px; padding: 16px 18px; border-radius: var(--air-radius);
  background: var(--air-surface); border: 1px solid var(--air-border); border-left: 3px solid var(--air-accent); flex-wrap: wrap;
}
.callout.stop { border-left-color: var(--air-danger); background: var(--el-color-danger-light-9); }
.callout.done { border-left-color: var(--air-success); }
.callout-icon { color: var(--air-accent); flex: none; }
.callout.stop .callout-icon { color: var(--air-danger); }
.callout.done .callout-icon { color: var(--air-success); }
.callout-text { flex: 1; min-width: 240px; line-height: 1.55; }
.callout-label { font-size: var(--air-fs-meta); color: var(--air-text-3); margin-bottom: 2px; }
.callout-actions { display: flex; gap: 8px; }
.callout-actions .el-button { margin-left: 0; }

.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 28px; }
.grid > .sec { margin: 0; }
.sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; min-height: 28px; }
.panel { background: var(--air-surface); border: 1px solid var(--air-border); border-radius: var(--air-radius); overflow: hidden; }
.pad { padding: 18px 20px; }
.row-item { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; }
.row-item + .row-item { border-top: 1px solid var(--air-border); }
.row-item:hover { background: var(--air-fill); }
.row-main { flex: 1; min-width: 0; line-height: 1.5; }
.chev { color: var(--air-text-3); }
.empty { padding: 18px 16px; }
.event { display: flex; align-items: baseline; gap: 12px; padding: 9px 16px; }
.event + .event { border-top: 1px solid var(--air-border); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--air-border-strong); flex: none; transform: translateY(-1px); }
.dot.danger { background: var(--air-danger); }
.dot.warning { background: var(--air-warning); }
.dot.primary { background: var(--air-accent); }
.ev-text { flex: 1; min-width: 0; }
.ev-time { flex: none; }
.mb { margin-bottom: 12px; }
.ml { margin-left: 6px; }
</style>
