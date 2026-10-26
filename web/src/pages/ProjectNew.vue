<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { createProject, getDefaults, listTasks, uploadTemplate } from '../api/projects'
import { newRequestId } from '../api/client'
import { BUDGET, STAGE, label } from '../constants/labels'
import { useI18n } from 'vue-i18n'
const { t, locale } = useI18n()

// P2 新建项目：先创建，拿到 project_id 后若选了模板再上传，最后跳到总览。
// 默认值（任务、运行模式、预算）来自 GET /api/defaults；预算只提交改过的类别，其余用后台默认值
const router = useRouter()
const formRef = ref()
const tasks = ref([])
const loadError = ref(false)
const defaults = ref(null)
const form = reactive({
  title: '',
  idea_text: '',
  compute: '',
  time: '',
  data: '',
  task: '',
  mode: 'fake',
  allow_local_execution: false,
  evidence_check: true,
  smoke_fail: '',
})
const budget = ref([]) // [{name, soft, hard}]
const templateFile = ref(null)
const submitting = ref(false)
let requestId = newRequestId()

const rules = computed(() => ({
  title: [{ required: true, message: t('newProject.fTitleRequired'), trigger: 'blur' }],
  idea_text: [{ required: true, message: t('newProject.fIdeaRequired'), trigger: 'blur' }],
  task: [{ required: true, message: t('newProject.fTaskRequired'), trigger: 'change' }],
}))
const realStages = computed(() => defaults.value?.real_stages || [])

onMounted(async () => {
  try {
    const [taskList, d] = await Promise.all([listTasks(), getDefaults()])
    tasks.value = taskList
    defaults.value = d
    form.task = taskList.find((x) => x.path === d.task)?.path || taskList[0]?.path || ''
    form.mode = d.mode
    form.allow_local_execution = d.allow_local_execution
    budget.value = Object.entries(d.budget).map(([name, lim]) => ({ name, soft: lim.soft ?? null, hard: lim.hard ?? null }))
  } catch {
    loadError.value = true
  }
})

// 只提交和默认值不同的类别（后台按类别整类覆盖）
function changedBudget() {
  const out = {}
  for (const row of budget.value) {
    const d = defaults.value?.budget?.[row.name] || {}
    if ((d.soft ?? null) !== row.soft || (d.hard ?? null) !== row.hard) out[row.name] = { soft: row.soft, hard: row.hard }
  }
  return Object.keys(out).length ? out : null
}

function onFile(file) {
  if (!file.name.toLowerCase().endsWith('.zip')) {
    ElMessage.warning(t('newProject.zipOnly'))
    return false
  }
  templateFile.value = file.raw
  return false
}

async function submit() {
  if (submitting.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  const constraints = Object.fromEntries(['compute', 'time', 'data'].filter((k) => form[k].trim()).map((k) => [k, form[k].trim()]))
  const dev = {}
  if (form.smoke_fail.trim()) { // 格式同 air new --smoke-fail：E2-seed=1:exit1，多个用逗号分隔
    dev.smoke_fail = Object.fromEntries(form.smoke_fail.split(',').map((s) => s.trim()).filter(Boolean).map((s) => {
      const i = s.lastIndexOf(':')
      return [s.slice(0, i), s.slice(i + 1)]
    }))
  }
  try {
    const p = await createProject({
      title: form.title.trim(),
      idea_text: form.idea_text.trim(),
      constraints,
      task: form.task,
      budget: changedBudget(),
      evidence_check: form.evidence_check,
      mode: form.mode,
      allow_local_execution: form.mode === 'real' && form.allow_local_execution,
      dev: Object.keys(dev).length ? dev : null,
      request_id: requestId,
    })
    if (templateFile.value) {
      try {
        await uploadTemplate(p.project_id, templateFile.value, { requestId: newRequestId() })
      } catch {
        ElMessage.warning(t('newProject.templateFailed'))
      }
    }
    ElMessage.success(t('newProject.created'))
    router.push(`/projects/${p.project_id}`)
  } catch {
    requestId = newRequestId()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-title"><h2>{{ t('newProject.title') }}</h2></div>
    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="mb" :title="t('newProject.loadFailed')" />
    <el-card shadow="never" class="wrap">
      <el-form ref="formRef" :model="form" :rules="rules" :label-width="locale === 'en' ? '170px' : '120px'" label-position="right">
        <el-form-item :label="t('newProject.fTitle')" prop="title">
          <el-input v-model="form.title" maxlength="100" :placeholder="t('newProject.fTitlePh')" />
        </el-form-item>
        <el-form-item :label="t('newProject.fIdea')" prop="idea_text">
          <el-input v-model="form.idea_text" type="textarea" :rows="4"
            :placeholder="t('newProject.fIdeaPh')" />
        </el-form-item>
        <el-form-item :label="t('newProject.fConstraints')">
          <div class="cons">
            <el-input v-model="form.compute" :placeholder="t('newProject.fCompute')" />
            <el-input v-model="form.time" :placeholder="t('newProject.fTime')" />
            <el-input v-model="form.data" :placeholder="t('newProject.fData')" />
          </div>
        </el-form-item>
        <el-form-item :label="t('newProject.fTask')" prop="task">
          <el-select v-model="form.task" :placeholder="t('newProject.fTaskPh')" class="full">
            <el-option v-for="tk in tasks" :key="tk.path" :label="`${tk.name} (${tk.path})`" :value="tk.path">
              <span>{{ tk.name }}</span>
              <span class="muted opt-desc">{{ tk.description }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="t('newProject.fMode')">
          <el-radio-group v-model="form.mode">
            <el-radio value="fake">{{ t('newProject.modeFake') }}</el-radio>
            <el-radio value="real" :disabled="!realStages.length">{{ t('newProject.modeReal') }}</el-radio>
          </el-radio-group>
          <div v-if="form.mode === 'real'" class="real">
            <div class="muted">
              {{ t('newProject.realStages') }}
              <el-tag v-for="s in realStages" :key="s" size="small" class="stag">{{ label(STAGE, s) }}</el-tag>
              <template v-if="realStages.length < 4">{{ t('newProject.realRest') }}</template>
            </div>
            <el-checkbox v-model="form.allow_local_execution">{{ t('newProject.allowLocal') }}</el-checkbox>
            <div class="muted small">{{ t('newProject.allowLocalHint') }}</div>
          </div>
          <div v-else-if="defaults && !realStages.length" class="muted small">{{ t('newProject.noReal') }}</div>
        </el-form-item>
        <el-form-item :label="t('newProject.fBudget')">
          <el-table :data="budget" size="small" class="budget">
            <el-table-column :label="t('newProject.colCategory')" min-width="160"><template #default="{ row }">{{ label(BUDGET, row.name) }}</template></el-table-column>
            <el-table-column :label="t('newProject.colSoft')" width="160">
              <template #default="{ row }"><el-input-number v-model="row.soft" :min="0" size="small" controls-position="right" :value-on-clear="null" :placeholder="t('newProject.noSoft')" /></template>
            </el-table-column>
            <el-table-column :label="t('newProject.colHard')" width="160">
              <template #default="{ row }"><el-input-number v-model="row.hard" :min="0" size="small" controls-position="right" :value-on-clear="null" :placeholder="t('newProject.noHard')" /></template>
            </el-table-column>
          </el-table>
          <div class="muted small">{{ t('newProject.budgetHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('newProject.fTemplate')">
          <el-upload :auto-upload="false" :limit="1" accept=".zip" :on-change="onFile" :on-remove="() => (templateFile = null)">
            <el-button icon="Upload">{{ t('newProject.chooseZip') }}</el-button>
            <template #tip><div class="muted">{{ t('newProject.templateHint') }}</div></template>
          </el-upload>
        </el-form-item>
        <el-collapse class="adv">
          <el-collapse-item :title="t('newProject.advanced')" name="adv">
            <el-form-item :label="t('newProject.fEvidence')">
              <el-switch v-model="form.evidence_check" />
              <span class="muted gap">{{ t('newProject.evidenceHint') }}</span>
            </el-form-item>
            <el-form-item :label="t('newProject.fSmokeFail')">
              <el-input v-model="form.smoke_fail" :placeholder="t('newProject.smokeFailPh')" />
            </el-form-item>
          </el-collapse-item>
        </el-collapse>
        <el-form-item>
          <el-button type="primary" :loading="submitting" :disabled="loadError" @click="submit">{{ t('newProject.submit') }}</el-button>
          <router-link to="/projects"><el-button class="gap">{{ t('common.cancel') }}</el-button></router-link>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.wrap { max-width: 860px; }
.mb { margin-bottom: 16px; max-width: 860px; }
.cons { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; width: 100%; }
.full { width: 100%; }
.opt-desc { margin-left: 12px; font-size: 12px; }
.real { width: 100%; margin-top: 4px; }
.stag { margin-left: 4px; }
.small { font-size: 12px; }
.budget { width: 100%; max-width: 520px; }
.gap { margin-left: 12px; }
.adv { margin: 0 0 18px 0; }
</style>
