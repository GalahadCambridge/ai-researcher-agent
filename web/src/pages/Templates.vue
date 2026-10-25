<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import PageState from '../components/PageState.vue'
import TimeText from '../components/TimeText.vue'
import { listTemplates, uploadTemplate } from '../api/projects'
import { newRequestId } from '../api/client'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'

// P14 模板管理：上传 zip；显示当前使用的模板与每个版本的检查结果。
// 模板有多个入口或缺依赖时，论文阶段会提问——问题出现在总览的问题卡片里，这里只给“去回答”链接
const ctx = useProject()
const pid = ctx.pid
const { data, loading, error, load } = useLoader(() => listTemplates(pid.value))
onMounted(load)
useProjectRefresh(load, ['event', 'question'])

const asVersionOf = ref('')
const uploading = ref(false)
const ids = computed(() => [...new Set((data.value?.templates || []).map((t) => t.template_id))])
const question = computed(() => ctx.status.value?.pending_question)

async function upload({ file }) {
  if (!file.name.toLowerCase().endsWith('.zip')) {
    ElMessage.warning('请上传 .zip 格式的模板')
    return
  }
  uploading.value = true
  try {
    const r = await uploadTemplate(pid.value, file, { templateId: asVersionOf.value || undefined, requestId: newRequestId() })
    ElMessage.success(`已上传 ${r.template_id} v${r.version}，项目改用这个模板`)
    load(true)
    ctx.refresh()
  } finally {
    uploading.value = false
  }
}

// check.json 的格式见详细设计 4 的 TemplateCheck：entry、entry_chosen_by、missing [{kind, name}]、minimal_compile_ok
const checkItems = (c) => {
  if (!c) return []
  const by = c.entry_chosen_by === 'user' ? '（用户选定）' : c.entry_chosen_by === 'auto' ? '（自动识别）' : ''
  const missing = (c.missing || []).map((m) => (typeof m === 'string' ? m : `${m.name}${m.kind ? `（${m.kind}）` : ''}`))
  return [
    ['入口文件', c.entry ? c.entry + by : `未确定（候选：${(c.entry_candidates || []).join('、') || '无'}）`],
    ['缺失的依赖', missing.length ? missing.join('、') : '无'],
    ['最小编译', c.minimal_compile_ok ? '成功' : '失败'],
  ]
}
</script>

<template>
  <div class="card-gap">
    <div class="page-title"><h2>论文模板</h2></div>

    <el-alert v-if="question && question.stage === 'writing'" type="warning" :closable="false" show-icon>
      <template #title>论文阶段有关于模板的问题等你回答 <router-link :to="`/projects/${pid}`" class="link">去回答 →</router-link></template>
    </el-alert>

    <el-card shadow="never">
      <template #header><b>上传模板</b></template>
      <div class="toolbar">
        <span>作为</span>
        <el-select v-model="asVersionOf" placeholder="新模板" clearable style="width: 200px">
          <el-option v-for="i in ids" :key="i" :value="i" :label="`${i} 的新版本（修好后重新上传）`" />
        </el-select>
        <el-upload :show-file-list="false" accept=".zip" :http-request="upload" :disabled="uploading">
          <el-button type="primary" icon="Upload" :loading="uploading">选择 zip 上传</el-button>
        </el-upload>
      </div>
      <div class="muted">zip 里放模板的 .tex、.cls、.sty 等文件。上传后项目立即改用它，论文阶段会检查入口文件和缺失的依赖。</div>
    </el-card>

    <el-card shadow="never">
      <template #header><b>模板版本</b></template>
      <PageState :loading="loading && !data" :error="error" @retry="load()">
        <div v-if="data" class="cur">
          当前使用：
          <el-tag v-if="data.current" type="success">{{ data.current.id }} v{{ data.current.version }}</el-tag>
          <el-tag v-else type="info">默认模板</el-tag>
        </div>
        <el-descriptions v-if="data?.default_check && !data.current" :column="1" border size="small" class="mb" title="默认模板检查结果">
          <el-descriptions-item v-for="[k, v] in checkItems(data.default_check)" :key="k" :label="k">{{ v }}</el-descriptions-item>
        </el-descriptions>
        <el-table v-if="data?.templates.length" :data="data.templates" size="small">
          <el-table-column label="模板" width="140">
            <template #default="{ row }"><span class="mono">{{ row.template_id }} v{{ row.version }}</span><el-tag v-if="row.in_use" size="small" type="success" class="ml">使用中</el-tag></template>
          </el-table-column>
          <el-table-column label="文件名" prop="filename" min-width="140" />
          <el-table-column label="上传时间" width="110"><template #default="{ row }"><TimeText :value="row.uploaded_at" /></template></el-table-column>
          <el-table-column label="入口候选" min-width="140"><template #default="{ row }"><span class="mono">{{ row.tex_files.join(', ') || '—' }}</span></template></el-table-column>
          <el-table-column label="检查结果" min-width="260">
            <template #default="{ row }">
              <span v-if="!row.check" class="muted">还没检查（论文阶段检查后显示）</span>
              <div v-for="[k, v] in checkItems(row.check)" v-else :key="k"><span class="muted">{{ k }}：</span>{{ v }}</div>
              <el-popover v-if="row.check" trigger="click" width="420">
                <template #reference><el-button link size="small" type="primary">原始结果</el-button></template>
                <pre class="code mono">{{ JSON.stringify(row.check, null, 2) }}</pre>
              </el-popover>
            </template>
          </el-table-column>
          <el-table-column label="文件" width="80">
            <template #default="{ row }">
              <el-popover trigger="click" width="360">
                <template #reference><el-button link size="small">{{ row.files.length }} 个</el-button></template>
                <div v-for="f in row.files" :key="f" class="mono small">{{ f }}</div>
              </el-popover>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="muted">还没有上传过模板，论文会用默认模板。</div>
      </PageState>
    </el-card>
  </div>
</template>

<style scoped>
.cur { margin-bottom: 12px; }
.link { color: var(--el-color-primary); margin-left: 8px; }
.ml { margin-left: 6px; }
.mb { margin-bottom: 12px; }
.small { font-size: 12px; }
</style>
