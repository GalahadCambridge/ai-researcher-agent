<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import TimeText from '../components/TimeText.vue'
import MarkdownView from '../components/MarkdownView.vue'
import DiffView from '../components/DiffView.vue'
import PdfViewer from '../components/PdfViewer.vue'
import ContentView from '../components/ContentView.vue'
import CopyText from '../components/CopyText.vue'
import { decide, getApproval } from '../api/approvals'
import { artifactContent, editArtifact, readFile } from '../api/artifacts'
import { pdfUrl } from '../api/paper'
import { isConflict, newRequestId } from '../api/client'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P5 审批详情（详细设计 5 第 4.5 节、第 6 节）。
// 防重复：打开时生成一个 request_id，提交中再点无效；409（已过期 / 已被处理）时提示并刷新。
const route = useRoute()
const router = useRouter()
const ctx = useProject()
const pid = ctx.pid
const aid = computed(() => route.params.aid)

const { data, loading, error, load } = useLoader(() => getApproval(pid.value, aid.value))
const a = computed(() => data.value?.approval)
const isPending = computed(() => a.value?.status === 'pending')
const isManuscript = computed(() => a.value?.kind === 'manuscript')

let requestId = newRequestId()
watch(aid, () => { requestId = newRequestId(); comment.value = ''; load() })
onMounted(load)
useProjectRefresh(load, ['approval'])

// ---------------------------------------------------------------- 内容
const showDiff = ref(false)
// 对比对象：后台给的 previous 是“上一个获批版本”；退回修改后还没有获批版本时，用紧挨着的上一版
const prev = ref({ version: null, content: null })
watch(data, async (d) => {
  prev.value = { version: null, content: null }
  if (!d || d.approval.kind === 'manuscript' || d.target_content == null) return
  if (d.previous_content != null) {
    prev.value = { version: d.approval.previous.version, content: d.previous_content }
    return
  }
  const t = d.approval.target
  if (t.version < 2) return
  try {
    const r = await artifactContent(pid.value, t.artifact_id, t.version - 1)
    if (typeof r?.content === 'string') prev.value = { version: t.version - 1, content: r.content }
  } catch {
    /* 取不到就不显示对比开关 */
  }
})
const canDiff = computed(() => prev.value.content != null)
const pdfRef = computed(() => data.value?.extra?.find((e) => e.artifact_id === 'paper/build/main.pdf'))
const review = computed(() => {
  const r = data.value?.extra?.find((e) => /paper\/review\/review_v\d+\.json$/.test(e.artifact_id))
  if (!r?.content) return null
  try {
    return { ...JSON.parse(r.content), file: r.artifact_id }
  } catch {
    return null
  }
})
const blockers = computed(() => review.value?.counts?.blocker || 0)
const reviewMd = ref('')
watch(review, async (r) => {
  reviewMd.value = ''
  if (!r) return
  try {
    reviewMd.value = (await readFile(pid.value, r.file.replace(/\.json$/, '.md'))).content
  } catch {
    /* 没有 Markdown 版时只显示数量 */
  }
})

// ---------------------------------------------------------------- 相关材料抽屉
const drawer = reactive({ open: false, title: '', name: '', content: '', loading: false, pdf: '' })
async function openExtra(e) {
  Object.assign(drawer, { open: true, title: `${e.artifact_id} v${e.version}`, name: e.artifact_id, content: '', pdf: '' })
  if (e.artifact_id.endsWith('.pdf')) {
    drawer.pdf = pdfUrl(pid.value, e.version)
    return
  }
  if (e.content) {
    drawer.content = e.content
    return
  }
  drawer.loading = true
  try {
    const r = await artifactContent(pid.value, e.artifact_id, e.version)
    drawer.content = typeof r === 'string' ? r : r.content ?? JSON.stringify(r, null, 2)
  } catch {
    drawer.content = t('approval.readFailed')
  } finally {
    drawer.loading = false
  }
}

// ---------------------------------------------------------------- 决策
const comment = ref('')
const submitting = ref('')

async function submit(decision) {
  if (submitting.value) return // 提交中再点无效
  const needComment = decision !== 'approved' || blockers.value > 0
  if (needComment && !comment.value.trim()) {
    ElMessage.warning(decision === 'approved' ? t('approval.needCommentBlocker', { n: blockers.value }) : t('approval.needComment'))
    return
  }
  if (decision === 'rejected') {
    try {
      await ElMessageBox.confirm(t('approval.rejectConfirm'), t('approval.reject'), { type: 'warning', confirmButtonText: t('approval.rejectOk'), cancelButtonText: t('common.cancel') })
    } catch {
      return
    }
  }
  submitting.value = decision
  try {
    await decide(pid.value, aid.value, {
      decision,
      comment: comment.value.trim(),
      expected_sha256: a.value.target.sha256, // 告诉后台“我审的是这个版本”
      request_id: requestId, // 网络重试时沿用同一个，后台只处理一次
    })
    ElMessage.success(t(`approval.done.${decision}`))
    ctx.refresh()
    router.push(`/projects/${pid.value}`)
  } catch (err) {
    if (isConflict(err)) {
      ElMessage.warning(t('approval.stale'))
      requestId = newRequestId()
      await load()
      ctx.refresh()
    }
  } finally {
    submitting.value = ''
  }
}

// ---------------------------------------------------------------- 简单文本编辑（4.13）
const EDITABLE = ['idea/selected.md', 'plan/experiment_plan.md']
const canEdit = computed(() => isPending.value && EDITABLE.includes(a.value?.target.artifact_id) && data.value?.target_content != null)
const edit = reactive({ open: false, text: '', saving: false, requestId: '' })
function openEdit() {
  Object.assign(edit, { open: true, text: data.value.target_content, requestId: newRequestId() })
}
async function saveEdit() {
  if (edit.saving) return
  edit.saving = true
  try {
    const r = await editArtifact(pid.value, {
      artifact_id: a.value.target.artifact_id,
      content: edit.text,
      base_sha256: a.value.target.sha256,
      request_id: edit.requestId,
    })
    ElMessage.success(t('approval.editSaved', { v: r.version ?? '' }))
    edit.open = false
    await load()
    ctx.refresh()
  } catch (err) {
    if (isConflict(err)) ElMessage.warning(t('approval.editStale'))
  } finally {
    edit.saving = false
  }
}
</script>

<template>
  <PageState :loading="loading && !data" :error="error" @retry="load()">
    <div v-if="a">
      <router-link :to="`/projects/${pid}/approvals`" class="back"><el-icon><ArrowLeft /></el-icon>{{ t('approval.backToList') }}</router-link>
      <div class="page-title">
        <h2>{{ a.summary }}</h2>
      </div>
      <div class="sub">
        <StateTag kind="kind" :value="a.kind" size="small" />
        <StateTag kind="approval" :value="a.status" size="small" />
        <span class="meta"><span class="mono">{{ a.approval_id }}</span> · <span class="mono">{{ a.target.artifact_id }} v{{ a.target.version }}</span> · {{ t('approval.submittedAt') }} <TimeText :value="a.created_at" /></span>
      </div>

      <el-alert v-if="!isPending" :type="a.status === 'approved' ? 'success' : 'info'" :closable="false" show-icon class="mb">
        <template #title>
          {{ t('approval.handled') }}<StateTag kind="approval" :value="a.status" size="small" />
          <span v-if="a.decided_at">（<TimeText :value="a.decided_at" />，{{ a.decided_by || '—' }}）</span>
          <span v-if="a.decision_comment">{{ t('approval.comment') }}{{ a.decision_comment }}</span>
          <router-link :to="`/projects/${pid}`" class="ml">{{ t('approval.backToOverview') }}</router-link>
        </template>
      </el-alert>

      <div class="cols">
        <!-- 左：要审的内容（阅读模式） -->
        <section class="content">
          <div class="content-bar">
            <CopyText :text="data.target_path" class="meta path" />
            <span class="spacer" />
            <el-switch v-if="canDiff" v-model="showDiff" :active-text="t('approval.diffWith', { v: prev.version })" />
            <el-button v-if="canEdit" size="small" icon="Edit" text @click="openEdit">{{ t('approval.edit') }}</el-button>
          </div>
          <div class="paper">
            <template v-if="isManuscript">
              <PdfViewer v-if="pdfRef" :src="pdfUrl(pid, pdfRef.version)" height="72vh" />
              <el-alert v-else type="info" :closable="false" :title="t('approval.noPdf')" />
              <div v-if="review" class="review">
                <h3 class="section-title">{{ t('approval.reviewSummary') }}</h3>
                <div class="counts">
                  <el-tag type="danger">{{ t('approval.blocker', { n: review.counts?.blocker || 0 }) }}</el-tag>
                  <el-tag type="warning">{{ t('approval.major', { n: review.counts?.major || 0 }) }}</el-tag>
                  <el-tag type="info">{{ t('approval.minor', { n: review.counts?.minor || 0 }) }}</el-tag>
                  <el-tag :type="review.compile_ok ? 'success' : 'danger'">{{ review.compile_ok ? t('approval.compileOk') : t('approval.compileFailed') }}</el-tag>
                  <router-link :to="`/projects/${pid}/paper`" class="ml">{{ t('approval.seeClaims') }}</router-link>
                </div>
                <el-collapse v-if="reviewMd" class="mt">
                  <el-collapse-item :title="t('approval.reviewFull')" name="r"><MarkdownView :source="reviewMd" reading /></el-collapse-item>
                </el-collapse>
              </div>
            </template>
            <template v-else-if="data.target_content != null">
              <DiffView v-if="showDiff && canDiff" :old-text="prev.content" :new-text="data.target_content" />
              <MarkdownView v-else :source="data.target_content" reading />
            </template>
            <el-alert v-else type="info" :closable="false" :title="t('approval.notText')" />
          </div>
        </section>

        <!-- 右：决策区（一个面板，用分隔线分成几块） -->
        <aside class="side">
          <div class="block">
            <div class="label">{{ t('approval.impact') }}</div>
            <p class="p">{{ a.impact || '—' }}</p>
            <div v-if="a.previous" class="meta">{{ t('approval.previousApproved', { v: a.previous.version }) }}</div>
          </div>

          <div v-if="data.extra.length" class="block">
            <div class="label">{{ t('approval.extra') }}</div>
            <div v-for="e in data.extra" :key="e.artifact_id" class="extra" @click="openExtra(e)">
              <el-icon><Document /></el-icon>
              <span class="mono name">{{ e.artifact_id }}</span>
              <span class="meta">v{{ e.version }}</span>
            </div>
          </div>

          <div v-if="isPending" class="block">
            <div class="label">{{ t('approval.decision') }}</div>
            <div v-if="blockers" class="blocker">
              <el-icon><Warning /></el-icon>
              <div><b>{{ t('approval.blockersLeft', { n: blockers }) }}</b><div class="meta">{{ t('approval.blockersHint') }}</div></div>
            </div>
            <el-input v-model="comment" type="textarea" :rows="4" resize="none"
              :placeholder="t('approval.commentPh')" />
            <el-button type="primary" class="approve" icon="Check" :loading="submitting === 'approved'" :disabled="!!submitting && submitting !== 'approved'" @click="submit('approved')">{{ t('approval.approve') }}</el-button>
            <div class="secondary">
              <el-button icon="RefreshLeft" :loading="submitting === 'changes_requested'" :disabled="!!submitting && submitting !== 'changes_requested'" @click="submit('changes_requested')">{{ t('approval.requestChanges') }}</el-button>
              <el-button type="danger" text icon="Close" :loading="submitting === 'rejected'" :disabled="!!submitting && submitting !== 'rejected'" @click="submit('rejected')">{{ t('approval.reject') }}</el-button>
            </div>
          </div>
        </aside>
      </div>

      <el-drawer v-model="drawer.open" :title="drawer.title" size="55%">
        <PdfViewer v-if="drawer.pdf" :src="drawer.pdf" />
        <div v-else v-loading="drawer.loading"><ContentView :name="drawer.name" :content="drawer.content" /></div>
      </el-drawer>

      <el-dialog v-model="edit.open" :title="t('approval.editTitle', { path: a.target.artifact_id })" width="80%" top="5vh">
        <el-alert type="info" :closable="false" show-icon class="mb" :title="t('approval.editHint')" />
        <el-input v-model="edit.text" type="textarea" :rows="24" class="mono" />
        <template #footer>
          <el-button @click="edit.open = false">{{ t('common.cancel') }}</el-button>
          <el-button type="primary" :loading="edit.saving" @click="saveEdit">{{ t('common.save') }}</el-button>
        </template>
      </el-dialog>
    </div>
  </PageState>
</template>

<style scoped>
.page-title { margin-bottom: 10px; }
.sub { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }
.cols { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 32px; align-items: start; }
@media (max-width: 1000px) { .cols { grid-template-columns: 1fr; } }
.content-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; min-height: 32px; flex-wrap: wrap; }
.path { min-width: 0; }
.paper { background: var(--air-surface); border: 1px solid var(--air-border); border-radius: var(--air-radius); padding: 32px 40px; }
@media (max-width: 700px) { .paper { padding: 20px; } }
.side {
  position: sticky; top: 16px; background: var(--air-surface); border: 1px solid var(--air-border);
  border-radius: var(--air-radius); padding: 4px 20px;
}
.block { padding: 16px 0; }
.block + .block { border-top: 1px solid var(--air-border); }
.label { font-size: var(--air-fs-meta); color: var(--air-text-3); margin-bottom: 8px; font-weight: 500; }
.p { margin: 0 0 6px; line-height: 1.6; }
.extra { display: flex; align-items: center; gap: 8px; padding: 6px 8px; margin: 0 -8px; cursor: pointer; border-radius: 6px; color: var(--air-text-2); }
.extra:hover { background: var(--air-fill); color: var(--air-text); }
.extra .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.blocker {
  display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; margin-bottom: 12px; border-radius: var(--air-radius-sm);
  background: var(--el-color-danger-light-9); color: var(--air-danger);
}
.blocker b { color: var(--air-text); font-weight: 600; }
.approve { width: 100%; margin-top: 12px; }
.secondary { display: flex; justify-content: space-between; margin-top: 8px; }
.secondary .el-button { margin-left: 0; }
.review { margin-top: 24px; }
.counts { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 10px; }
.mb { margin-bottom: 16px; }
.mt { margin-top: 12px; }
.ml { margin-left: 8px; }
</style>
