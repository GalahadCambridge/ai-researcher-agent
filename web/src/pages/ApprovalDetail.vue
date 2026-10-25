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
    drawer.content = '（无法读取这个文件）'
  } finally {
    drawer.loading = false
  }
}

// ---------------------------------------------------------------- 决策
const comment = ref('')
const submitting = ref('')
const DECISION_TEXT = { approved: '已批准', changes_requested: '已退回修改，系统会按你的意见生成新版本', rejected: '已拒绝' }

async function submit(decision) {
  if (submitting.value) return // 提交中再点无效
  const needComment = decision !== 'approved' || blockers.value > 0
  if (needComment && !comment.value.trim()) {
    ElMessage.warning(decision === 'approved' ? `仍有 ${blockers.value} 个阻断问题，批准时必须填写意见` : '请填写意见，说明需要怎么改 / 为什么拒绝')
    return
  }
  if (decision === 'rejected') {
    try {
      await ElMessageBox.confirm('拒绝后系统会问你“重写还是结束项目”。确定拒绝吗？', '拒绝', { type: 'warning', confirmButtonText: '确定拒绝', cancelButtonText: '取消' })
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
    ElMessage.success(DECISION_TEXT[decision])
    ctx.refresh()
    router.push(`/projects/${pid.value}`)
  } catch (err) {
    if (isConflict(err)) {
      ElMessage.warning('该版本已过期或已被处理，已刷新为最新状态')
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
    ElMessage.success(`已保存为人工编辑的新版本 v${r.version ?? ''}`)
    edit.open = false
    await load()
    ctx.refresh()
  } catch (err) {
    if (isConflict(err)) ElMessage.warning('文件已被修改，请刷新后再编辑')
  } finally {
    edit.saving = false
  }
}
</script>

<template>
  <PageState :loading="loading && !data" :error="error" @retry="load()">
    <div v-if="a">
      <div class="page-title">
        <router-link :to="`/projects/${pid}/approvals`"><el-button icon="ArrowLeft" text>审批列表</el-button></router-link>
        <h2>审批 {{ a.approval_id }}</h2>
        <StateTag kind="kind" :value="a.kind" />
        <span class="mono">{{ a.target.artifact_id }} v{{ a.target.version }}</span>
        <StateTag kind="approval" :value="a.status" />
      </div>

      <el-alert v-if="!isPending" :type="a.status === 'approved' ? 'success' : 'info'" :closable="false" show-icon class="mb">
        <template #title>
          这个审批已处理：<StateTag kind="approval" :value="a.status" size="small" />
          <span v-if="a.decided_at">（<TimeText :value="a.decided_at" />，{{ a.decided_by || '—' }}）</span>
          <span v-if="a.decision_comment">意见：{{ a.decision_comment }}</span>
          <router-link :to="`/projects/${pid}`" class="ml">回到总览</router-link>
        </template>
      </el-alert>

      <div class="cols">
        <!-- 左：要审的内容 -->
        <el-card shadow="never" class="left">
          <template #header>
            <div class="ch">
              <b>要审的内容</b>
              <CopyText :text="data.target_path" class="muted small" />
              <span class="spacer" />
              <el-button v-if="canEdit" size="small" icon="Edit" @click="openEdit">编辑</el-button>
              <el-switch v-if="canDiff" v-model="showDiff" :active-text="`与 v${prev.version} 对比`" />
            </div>
          </template>
          <template v-if="isManuscript">
            <PdfViewer v-if="pdfRef" :src="pdfUrl(pid, pdfRef.version)" height="70vh" />
            <el-alert v-else type="info" :closable="false" title="这一版没有 PDF" />
            <div v-if="review" class="review">
              <h4>核验报告摘要</h4>
              <div class="counts">
                <el-tag type="danger" effect="dark">阻断 {{ review.counts?.blocker || 0 }}</el-tag>
                <el-tag type="warning">重要 {{ review.counts?.major || 0 }}</el-tag>
                <el-tag type="info">次要 {{ review.counts?.minor || 0 }}</el-tag>
                <el-tag :type="review.compile_ok ? 'success' : 'danger'" effect="plain">{{ review.compile_ok ? '编译成功' : '编译失败' }}</el-tag>
                <router-link :to="`/projects/${pid}/paper`" class="ml">在“论文”页查看每条论断 →</router-link>
              </div>
              <el-collapse v-if="reviewMd" class="mt">
                <el-collapse-item title="核验报告全文" name="r"><MarkdownView :source="reviewMd" /></el-collapse-item>
              </el-collapse>
            </div>
          </template>
          <template v-else-if="data.target_content != null">
            <DiffView v-if="showDiff && canDiff" :old-text="prev.content" :new-text="data.target_content" />
            <MarkdownView v-else :source="data.target_content" />
          </template>
          <el-alert v-else type="info" :closable="false" title="这个对象不是文本文件，请查看右侧的相关材料" />
        </el-card>

        <!-- 右：决策区 -->
        <div class="right card-gap">
          <el-card shadow="never">
            <template #header><b>变更摘要</b></template>
            <p class="p">{{ a.summary }}</p>
            <div class="muted small">影响范围</div>
            <p class="p">{{ a.impact || '—' }}</p>
            <div class="muted small">提交时间：<TimeText :value="a.created_at" /></div>
            <div v-if="a.previous" class="muted small">上一个获批版本：v{{ a.previous.version }}</div>
          </el-card>

          <el-card v-if="data.extra.length" shadow="never">
            <template #header><b>相关材料</b></template>
            <div v-for="e in data.extra" :key="e.artifact_id" class="extra" @click="openExtra(e)">
              <el-icon><Document /></el-icon>
              <span class="mono">{{ e.artifact_id }}</span>
              <span class="muted">v{{ e.version }}</span>
            </div>
          </el-card>

          <el-card v-if="isPending" shadow="never">
            <template #header><b>你的决定</b></template>
            <el-alert v-if="blockers" type="error" :closable="false" show-icon class="mb"
              :title="`仍有 ${blockers} 个阻断问题`" description="批准时必须填写意见（例如为什么可以接受）。" />
            <el-input v-model="comment" type="textarea" :rows="4"
              placeholder="意见：退回修改和拒绝时必填，批准时可选" />
            <div class="decide">
              <el-button type="success" icon="Check" :loading="submitting === 'approved'" :disabled="!!submitting && submitting !== 'approved'" @click="submit('approved')">批准</el-button>
              <el-button type="warning" icon="RefreshLeft" :loading="submitting === 'changes_requested'" :disabled="!!submitting && submitting !== 'changes_requested'" @click="submit('changes_requested')">退回修改</el-button>
              <el-button type="danger" icon="Close" :loading="submitting === 'rejected'" :disabled="!!submitting && submitting !== 'rejected'" @click="submit('rejected')">拒绝</el-button>
            </div>
          </el-card>
        </div>
      </div>

      <el-drawer v-model="drawer.open" :title="drawer.title" size="55%">
        <PdfViewer v-if="drawer.pdf" :src="drawer.pdf" />
        <div v-else v-loading="drawer.loading"><ContentView :name="drawer.name" :content="drawer.content" /></div>
      </el-drawer>

      <el-dialog v-model="edit.open" :title="`编辑 ${a.target.artifact_id}`" width="80%" top="5vh">
        <el-alert type="info" :closable="false" show-icon class="mb" title="保存后登记为“人工编辑”的新版本；系统会用新版本重新提交审批。" />
        <el-input v-model="edit.text" type="textarea" :rows="24" class="mono" />
        <template #footer>
          <el-button @click="edit.open = false">取消</el-button>
          <el-button type="primary" :loading="edit.saving" @click="saveEdit">保存</el-button>
        </template>
      </el-dialog>
    </div>
  </PageState>
</template>

<style scoped>
.cols { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 16px; align-items: start; }
@media (max-width: 1000px) { .cols { grid-template-columns: 1fr; } }
.ch { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.small { font-size: 12px; }
.p { margin: 4px 0 12px; line-height: 1.6; }
.extra { display: flex; align-items: center; gap: 6px; padding: 6px 4px; cursor: pointer; border-radius: 4px; color: var(--el-color-primary); }
.extra:hover { background: var(--el-fill-color-light); }
.decide { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.decide .el-button { margin-left: 0; }
.review { margin-top: 16px; }
.review h4 { margin: 0 0 8px; }
.counts { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.mb { margin-bottom: 12px; }
.mt { margin-top: 12px; }
.ml { margin-left: 8px; color: var(--el-color-primary); }
.right { position: sticky; top: 0; }
</style>
