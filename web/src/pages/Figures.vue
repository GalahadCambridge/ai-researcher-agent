<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import FileImage from '../components/FileImage.vue'
import ContentView from '../components/ContentView.vue'
import { listFigures } from '../api/paper'
import { readFile } from '../api/artifacts'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P9 图表：卡片网格；点击看大图、规格、数据来源（汇总表）和全部输入运行
const route = useRoute()
const router = useRouter()
const { pid } = useProject()
const { data, loading, error, load } = useLoader(() => listFigures(pid.value))
onMounted(load)
useProjectRefresh(load, ['event'])

const current = ref(null)
const aggMd = ref('')
const open = (f) => router.replace({ query: { fig: f.fig_id } })
watch(() => [route.query.fig, data.value], async ([id]) => {
  current.value = id ? (data.value || []).find((f) => f.fig_id === id) || null : null
  aggMd.value = ''
  if (current.value) {
    try {
      aggMd.value = (await readFile(pid.value, current.value.aggregate_ref.artifact_id.replace(/\.json$/, '.md'))).content
    } catch {
      aggMd.value = ''
    }
  }
}, { immediate: true })
const drawerOpen = computed({ get: () => !!current.value, set: (v) => { if (!v) router.replace({ query: {} }) } })
const image = (f) => f.outputs.find((o) => /\.(png|svg|jpe?g)$/i.test(o)) || f.outputs[0]
</script>

<template>
  <div>
    <div class="page-title"><h2>{{ t('figures.title') }}</h2></div>
    <PageState :loading="loading && !data" :error="error" :empty="!data?.length" :empty-text="t('figures.empty')" @retry="load()">
      <div class="grid">
        <el-card v-for="f in data" :key="f.fig_id" shadow="hover" class="fig" @click="open(f)">
          <FileImage :pid="pid" :path="image(f)" height="200px" />
          <div class="cap"><b class="mono">{{ f.fig_id }}</b> {{ f.spec.title }}</div>
          <div class="muted small">{{ f.spec.caption }}</div>
        </el-card>
      </div>
    </PageState>

    <el-drawer v-model="drawerOpen" :title="current ? `${current.fig_id}：${current.spec.title}` : ''" size="60%">
      <div v-if="current">
        <FileImage :pid="pid" :path="image(current)" />
        <p>{{ current.spec.caption }}</p>
        <el-descriptions :column="2" border size="small" class="mb">
          <el-descriptions-item :label="t('figures.kind')">{{ current.spec.kind }}</el-descriptions-item>
          <el-descriptions-item :label="t('figures.metric')">{{ current.spec.metric }}</el-descriptions-item>
          <el-descriptions-item :label="t('figures.x')">{{ current.spec.x }}</el-descriptions-item>
          <el-descriptions-item :label="t('figures.error')">{{ current.spec.error }}</el-descriptions-item>
          <el-descriptions-item :label="t('figures.n')">
            <span v-for="(n, g) in current.n_per_group" :key="g" class="mono">{{ g }}={{ n }} </span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('figures.skill')"><span class="mono">{{ current.skill_ref }}</span></el-descriptions-item>
        </el-descriptions>
        <h4>{{ t('figures.source', { id: current.spec.aggregate_id, v: current.aggregate_ref.version }) }}</h4>
        <ContentView v-if="aggMd" name="x.md" :content="aggMd" />
        <router-link v-else :to="`/projects/${pid}/files?path=${encodeURIComponent(current.aggregate_ref.artifact_id)}`" class="link">{{ t('figures.viewAggregate') }}</router-link>
        <h4>{{ t('figures.inputRuns', { n: current.run_ids.length }) }}</h4>
        <div class="runs">
          <router-link v-for="r in current.run_ids" :key="r" :to="`/projects/${pid}/runs/${r}`" class="mono link">{{ r }}</router-link>
        </div>
        <h4>{{ t('figures.outputs') }}</h4>
        <div v-for="o in current.outputs" :key="o"><router-link :to="`/projects/${pid}/files?path=${encodeURIComponent(o)}`" class="mono link">{{ o }}</router-link></div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.fig { cursor: pointer; }
.cap { margin-top: 8px; }
.small { font-size: 12px; }
.mb { margin-bottom: 12px; }
.runs { display: flex; flex-direction: column; gap: 4px; }
.link { color: var(--el-color-primary); }
</style>
