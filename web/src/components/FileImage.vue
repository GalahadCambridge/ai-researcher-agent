<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { fileObjectUrl } from '../api/artifacts'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 显示工作区里的图片（PNG 或 SVG）
const props = defineProps({ pid: String, path: String, alt: { type: String, default: '' }, height: { type: String, default: 'auto' } })
const url = ref('')
const failed = ref(false)

async function load() {
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = ''
  failed.value = false
  if (!props.path) return
  try {
    url.value = await fileObjectUrl(props.pid, props.path)
  } catch {
    failed.value = true
  }
}
watch(() => [props.pid, props.path], load, { immediate: true })
onBeforeUnmount(() => url.value && URL.revokeObjectURL(url.value))
</script>

<template>
  <img v-if="url" :src="url" :alt="alt" class="img" :style="{ height }" />
  <div v-else-if="failed" class="muted ph">{{ t('image.failed') }}</div>
  <el-skeleton v-else :rows="3" animated />
</template>

<style scoped>
.img { max-width: 100%; display: block; margin: 0 auto; object-fit: contain; background: #fff; }
.ph { text-align: center; padding: 24px; }
</style>
