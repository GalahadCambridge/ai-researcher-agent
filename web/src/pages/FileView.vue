<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import CopyText from '../components/CopyText.vue'
import ContentView from '../components/ContentView.vue'
import { http } from '../api/client'
import { fileUrl } from '../api/artifacts'
import { useProject } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P12 文件查看：只读显示工作区内的一个文件（文本等宽显示，图片直接显示，其余提供下载）
const route = useRoute()
const router = useRouter()
const { pid } = useProject()
const input = ref('')
const loading = ref(false)
const error = ref(null)
const text = ref(null)
const image = ref('')
const binary = ref(false)
const raw = ref(false)

async function load() {
  const path = route.query.path || ''
  input.value = path
  text.value = null
  binary.value = false
  if (image.value) URL.revokeObjectURL(image.value)
  image.value = ''
  error.value = null
  if (!path) return
  loading.value = true
  try {
    const blob = await http.get(`/projects/${pid.value}/files`, { quiet: true, params: { path }, responseType: 'blob' })
    if (blob.type.includes('json')) {
      const body = JSON.parse(await blob.text())
      text.value = body.content
      if (path.endsWith('.svg')) image.value = URL.createObjectURL(new Blob([body.content], { type: 'image/svg+xml' }))
    } else if (blob.type.startsWith('image/')) {
      image.value = URL.createObjectURL(blob)
    } else {
      binary.value = true
    }
  } catch (e) {
    // blob 响应里的错误信息要先转回 JSON
    try {
      const body = JSON.parse(await e.response.data.text())
      e.response.data = body
    } catch {
      /* 保持原样 */
    }
    error.value = e
  } finally {
    loading.value = false
  }
}
watch(() => route.query.path, load, { immediate: true })
onBeforeUnmount(() => image.value && URL.revokeObjectURL(image.value))
const go = () => router.push({ query: { path: input.value.trim() } })
</script>

<template>
  <div>
    <a class="back" href="#" @click.prevent="router.back()"><el-icon><ArrowLeft /></el-icon>{{ t('common.back') }}</a>
    <div class="page-title">
      <h2>{{ t('files.title') }}</h2>
    </div>
    <div class="toolbar">
      <el-input v-model="input" :placeholder="t('files.pathPh')" class="mono path" @keyup.enter="go">
        <template #append><el-button @click="go">{{ t('files.open') }}</el-button></template>
      </el-input>
    </div>
    <el-card shadow="never">
      <el-empty v-if="!route.query.path" :description="t('files.hint')" />
      <PageState v-else :loading="loading" :error="error" @retry="load">
        <div class="toolbar">
          <CopyText :text="route.query.path" />
          <span class="spacer" />
          <el-switch v-if="text != null && /\.(md|json|jsonl)$/.test(route.query.path)" v-model="raw" :active-text="t('files.raw')" />
          <a :href="fileUrl(pid, route.query.path)" target="_blank" rel="noopener"><el-button size="small" text type="primary">{{ t('common.openInNewTab') }}</el-button></a>
        </div>
        <img v-if="image" :src="image" class="img" alt="" />
        <template v-else-if="text != null">
          <pre v-if="raw" class="code mono">{{ text }}</pre>
          <ContentView v-else :name="route.query.path" :content="text" />
        </template>
        <el-result v-else-if="binary" icon="info" :title="t('files.binary')">
          <template #extra><a :href="fileUrl(pid, route.query.path)" download><el-button type="primary">{{ t('common.download') }}</el-button></a></template>
        </el-result>
      </PageState>
    </el-card>
  </div>
</template>

<style scoped>
.path { max-width: 720px; }
.img { max-width: 100%; background: #fff; }
</style>
