<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownView from './MarkdownView.vue'
import { answerQuestion } from '../api/projects'
import { isConflict, newRequestId } from '../api/client'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 待回答问题（详细设计 5 第 4.3 节）。提交方式与审批相同：request_id、提交中禁用、409 提示并刷新
const props = defineProps({ pid: String, question: { type: Object, required: true } })
const emit = defineEmits(['answered'])

const text = ref('')
const submitting = ref('')
let requestId = newRequestId()
watch(() => props.question.question_id, () => { text.value = ''; requestId = newRequestId() })

const textRequired = computed(() => !props.question.options?.length)

function refLink(ref) {
  const m = /^runs\/([^/]+)/.exec(ref.artifact_id)
  if (m) return { to: `/projects/${props.pid}/runs/${m[1]}`, label: t('question.run', { id: m[1] }) }
  if (ref.artifact_id.startsWith('templates')) return { to: `/projects/${props.pid}/templates`, label: t('question.templates') }
  return { to: `/projects/${props.pid}/files?path=${encodeURIComponent(ref.artifact_id)}`, label: `${ref.artifact_id} v${ref.version}` }
}

async function submit(choice) {
  if (submitting.value) return
  if (textRequired.value && !text.value.trim()) {
    ElMessage.warning(t('question.needText'))
    return
  }
  if (choice === 'end') {
    try {
      await ElMessageBox.confirm(t('question.endConfirm'), t('question.endTitle'), { type: 'warning', confirmButtonText: t('question.endOk'), cancelButtonText: t('question.endNo') })
    } catch {
      return
    }
  }
  submitting.value = choice || 'text'
  try {
    await answerQuestion(props.pid, { question_id: props.question.question_id, choice: choice || null, text: text.value, request_id: requestId })
    ElMessage.success(t('question.answered'))
    emit('answered')
  } catch (err) {
    if (isConflict(err)) {
      ElMessage.warning(t('question.stale'))
      requestId = newRequestId()
      emit('answered')
    }
  } finally {
    submitting.value = ''
  }
}
</script>

<template>
  <el-card class="question" shadow="always">
    <template #header>
      <div class="hd">
        <el-icon color="var(--el-color-warning)" size="20"><QuestionFilled /></el-icon>
        <b>{{ t('question.title') }}</b>
        <span class="muted mono">{{ question.question_id }} · {{ question.stage }}</span>
      </div>
    </template>
    <MarkdownView :source="question.text" />
    <div v-if="question.refs?.length" class="refs">
      <span class="muted">{{ t('question.refs') }}</span>
      <router-link v-for="r in question.refs" :key="r.artifact_id + r.version" :to="refLink(r).to" class="ref">{{ refLink(r).label }}</router-link>
    </div>
    <el-input v-if="question.allow_text" v-model="text" type="textarea" :rows="3" class="txt"
      :placeholder="textRequired ? t('question.textRequired') : t('question.textOptional')" />
    <div class="opts">
      <!-- default 是“无人值守时自动选的安全选项”，不是推荐，所以不突出显示，只加一句说明 -->
      <el-button v-for="o in question.options" :key="o.id" :type="o.id === 'end' ? 'danger' : 'default'"
        :plain="o.id === 'end'" :loading="submitting === o.id" :disabled="!!submitting && submitting !== o.id" @click="submit(o.id)">
        {{ o.label }}<span v-if="o.default" class="dflt">{{ t('question.defaultOption') }}</span>
      </el-button>
      <el-button v-if="textRequired" type="primary" :loading="submitting === 'text'" :disabled="!!submitting" @click="submit(null)">{{ t('question.submit') }}</el-button>
    </div>
  </el-card>
</template>

<style scoped>
.question { border: 1px solid var(--el-color-warning-light-5); }
.hd { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.refs { margin: 8px 0; display: flex; gap: 12px; flex-wrap: wrap; }
.ref { color: var(--el-color-primary); }
.txt { margin: 8px 0; }
.opts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.opts .el-button { margin-left: 0; }
.dflt { font-size: 12px; opacity: 0.8; }
</style>
