<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { createProject, listTasks, uploadTemplate } from '../api/projects'
import { newRequestId } from '../api/client'

// P2 新建项目：先创建，拿到 project_id 后若选了模板再上传，最后跳到总览
const router = useRouter()
const formRef = ref()
const tasks = ref([])
const tasksError = ref(false)
const form = reactive({
  title: '',
  idea_text: '',
  compute: '',
  time: '',
  data: '',
  task: '',
  llm_usd: 5,
  wall_hours: 6,
  evidence_check: true,
  smoke_fail: '',
})
const templateFile = ref(null)
const submitting = ref(false)
let requestId = newRequestId()

const rules = {
  title: [{ required: true, message: '请填写项目标题', trigger: 'blur' }],
  idea_text: [{ required: true, message: '请用一两句话写下你的研究想法', trigger: 'blur' }],
  task: [{ required: true, message: '请选择任务配置', trigger: 'change' }],
}

onMounted(async () => {
  try {
    tasks.value = await listTasks()
    form.task = tasks.value.find((t) => t.path === 'tasks/smoke')?.path || tasks.value[0]?.path || ''
  } catch {
    tasksError.value = true
  }
})

function onFile(file) {
  if (!file.name.toLowerCase().endsWith('.zip')) {
    ElMessage.warning('请上传 .zip 格式的模板')
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
      // 传了 budget 会整体替换默认值，所以 GPU、存储也照默认值带上（core/models/project.py 的 _default_budget）
      budget: {
        llm_usd: { soft: +(form.llm_usd * 0.8).toFixed(2), hard: form.llm_usd },
        wall_hours: { hard: form.wall_hours },
        gpu_hours: { hard: 2 },
        storage_gb: { hard: 5 },
      },
      evidence_check: form.evidence_check,
      dev: Object.keys(dev).length ? dev : null,
      request_id: requestId,
    })
    if (templateFile.value) {
      try {
        await uploadTemplate(p.project_id, templateFile.value, { requestId: newRequestId() })
      } catch {
        ElMessage.warning('项目已创建，但模板上传失败；可以稍后在“模板”页重新上传')
      }
    }
    ElMessage.success('项目已创建，系统开始选题')
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
    <div class="page-title"><h2>新建项目</h2></div>
    <el-card shadow="never" class="wrap">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="120px" label-position="right">
        <el-form-item label="项目标题" prop="title">
          <el-input v-model="form.title" maxlength="100" placeholder="如：小样本文本分类的微调方法比较" />
        </el-form-item>
        <el-form-item label="研究想法" prop="idea_text">
          <el-input v-model="form.idea_text" type="textarea" :rows="4"
            placeholder="研究小型文本分类模型在数据很少时，哪种微调方法更好……" />
        </el-form-item>
        <el-form-item label="约束（可选）">
          <div class="cons">
            <el-input v-model="form.compute" placeholder="算力：如 只有 CPU / 1 张 GPU" />
            <el-input v-model="form.time" placeholder="时间：如 3 天内完成" />
            <el-input v-model="form.data" placeholder="数据：如 只用公开数据集" />
          </div>
        </el-form-item>
        <el-form-item label="任务配置" prop="task">
          <el-select v-model="form.task" placeholder="选择任务" class="full">
            <el-option v-for="t in tasks" :key="t.path" :label="`${t.name}（${t.path}）`" :value="t.path">
              <span>{{ t.name }}</span>
              <span class="muted opt-desc">{{ t.description }}</span>
            </el-option>
          </el-select>
          <div v-if="tasksError" class="muted">读取任务列表失败，请确认后台已启动</div>
        </el-form-item>
        <el-form-item label="预算">
          <div class="budget">
            <span>大模型费用上限</span>
            <el-input-number v-model="form.llm_usd" :min="0" :step="1" :precision="2" />
            <span>美元</span>
            <span class="gap">运行时长上限</span>
            <el-input-number v-model="form.wall_hours" :min="0" :step="1" />
            <span>小时</span>
          </div>
          <div class="muted">用到 80% 时会提醒，用完会自动暂停并告诉你原因。</div>
        </el-form-item>
        <el-form-item label="论文模板">
          <el-upload :auto-upload="false" :limit="1" accept=".zip" :on-change="onFile" :on-remove="() => (templateFile = null)">
            <el-button icon="Upload">选择模板 zip（可选）</el-button>
            <template #tip><div class="muted">不上传就用默认模板。之后也可以在项目的“模板”页上传。</div></template>
          </el-upload>
        </el-form-item>
        <el-collapse class="adv">
          <el-collapse-item title="高级选项" name="adv">
            <el-form-item label="论断-证据检查">
              <el-switch v-model="form.evidence_check" />
              <span class="muted gap">对比实验用，平时保持开启</span>
            </el-form-item>
            <el-form-item label="让运行失败">
              <el-input v-model="form.smoke_fail" placeholder="开发调试：如 E2-seed=1:exit1（同 air new --smoke-fail）" />
            </el-form-item>
          </el-collapse-item>
        </el-collapse>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submit">创建并开始</el-button>
          <router-link to="/projects"><el-button class="gap">取消</el-button></router-link>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.wrap { max-width: 860px; }
.cons { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; width: 100%; }
.full { width: 100%; }
.opt-desc { margin-left: 12px; font-size: 12px; }
.budget { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.gap { margin-left: 12px; }
.adv { margin: 0 0 18px 120px; }
</style>
