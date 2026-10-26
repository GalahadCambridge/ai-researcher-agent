<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import PageState from '../components/PageState.vue'
import TimeText from '../components/TimeText.vue'
import { listTemplates, uploadTemplate } from '../api/projects'
import { newRequestId } from '../api/client'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

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
    ElMessage.warning(t('newProject.zipOnly'))
    return
  }
  uploading.value = true
  try {
    const r = await uploadTemplate(pid.value, file, { templateId: asVersionOf.value || undefined, requestId: newRequestId() })
    ElMessage.success(t('templates.uploaded', { id: r.template_id, v: r.version }))
    load(true)
    ctx.refresh()
  } finally {
    uploading.value = false
  }
}

// check.json 的格式见详细设计 4 的 TemplateCheck：entry、entry_chosen_by、missing [{kind, name}]、minimal_compile_ok
const checkItems = (c) => {
  if (!c) return []
  const by = c.entry_chosen_by === 'user' ? t('templates.byUser') : c.entry_chosen_by === 'auto' ? t('templates.byAuto') : ''
  const missing = (c.missing || []).map((m) => (typeof m === 'string' ? m : `${m.name}${m.kind ? `（${m.kind}）` : ''}`))
  return [
    [t('templates.entry'), c.entry ? c.entry + by : t('templates.entryUnknown', { list: (c.entry_candidates || []).join(', ') || t('common.none') })],
    [t('templates.missing'), missing.length ? missing.join(', ') : t('common.none')],
    [t('templates.compile'), c.minimal_compile_ok ? t('templates.ok') : t('templates.failed')],
  ]
}
</script>

<template>
  <div class="card-gap">
    <div class="page-title"><h2>{{ t('templates.title') }}</h2></div>

    <el-alert v-if="question && question.stage === 'writing'" type="warning" :closable="false" show-icon>
      <template #title>{{ t('templates.question') }} <router-link :to="`/projects/${pid}`" class="link">{{ t('templates.answer') }}</router-link></template>
    </el-alert>

    <el-card shadow="never">
      <template #header><b>{{ t('templates.upload') }}</b></template>
      <div class="toolbar">
        <span>{{ t('templates.as') }}</span>
        <el-select v-model="asVersionOf" :placeholder="t('templates.asNew')" clearable style="width: 260px">
          <el-option v-for="i in ids" :key="i" :value="i" :label="t('templates.asVersion', { id: i })" />
        </el-select>
        <el-upload :show-file-list="false" accept=".zip" :http-request="upload" :disabled="uploading">
          <el-button type="primary" icon="Upload" :loading="uploading">{{ t('templates.choose') }}</el-button>
        </el-upload>
      </div>
      <div class="muted">{{ t('templates.hint') }}</div>
    </el-card>

    <el-card shadow="never">
      <template #header><b>{{ t('templates.versions') }}</b></template>
      <PageState :loading="loading && !data" :error="error" @retry="load()">
        <div v-if="data" class="cur">
          {{ t('templates.current') }}
          <el-tag v-if="data.current" type="success">{{ data.current.id }} v{{ data.current.version }}</el-tag>
          <el-tag v-else type="info">{{ t('templates.default') }}</el-tag>
        </div>
        <el-descriptions v-if="data?.default_check && !data.current" :column="1" border size="small" class="mb" :title="t('templates.defaultCheck')">
          <el-descriptions-item v-for="[k, v] in checkItems(data.default_check)" :key="k" :label="k">{{ v }}</el-descriptions-item>
        </el-descriptions>
        <el-table v-if="data?.templates.length" :data="data.templates" size="small">
          <el-table-column :label="t('templates.colTemplate')" width="140">
            <template #default="{ row }"><span class="mono">{{ row.template_id }} v{{ row.version }}</span><el-tag v-if="row.in_use" size="small" type="success" class="ml">{{ t('templates.inUse') }}</el-tag></template>
          </el-table-column>
          <el-table-column :label="t('templates.colFile')" prop="filename" min-width="140" />
          <el-table-column :label="t('templates.colUploaded')" width="110"><template #default="{ row }"><TimeText :value="row.uploaded_at" /></template></el-table-column>
          <el-table-column :label="t('templates.colEntries')" min-width="140"><template #default="{ row }"><span class="mono">{{ row.tex_files.join(', ') || '—' }}</span></template></el-table-column>
          <el-table-column :label="t('templates.colCheck')" min-width="260">
            <template #default="{ row }">
              <span v-if="!row.check" class="muted">{{ t('templates.notChecked') }}</span>
              <div v-for="[k, v] in checkItems(row.check)" v-else :key="k"><span class="muted">{{ k }}：</span>{{ v }}</div>
              <el-popover v-if="row.check" trigger="click" width="420">
                <template #reference><el-button link size="small" type="primary">{{ t('templates.raw') }}</el-button></template>
                <pre class="code mono">{{ JSON.stringify(row.check, null, 2) }}</pre>
              </el-popover>
            </template>
          </el-table-column>
          <el-table-column :label="t('templates.colFiles')" width="80">
            <template #default="{ row }">
              <el-popover trigger="click" width="360">
                <template #reference><el-button link size="small">{{ t('templates.filesCount', { n: row.files.length }) }}</el-button></template>
                <div v-for="f in row.files" :key="f" class="mono small">{{ f }}</div>
              </el-popover>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="muted">{{ t('templates.none') }}</div>
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
