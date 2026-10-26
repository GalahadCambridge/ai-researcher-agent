<script setup>
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
// 用浏览器自带的 PDF 查看器（详细设计 5 第 8 节）
defineProps({ src: { type: String, required: true }, height: { type: String, default: '75vh' } })
</script>

<template>
  <div>
    <div class="toolbar">
      <span class="spacer" />
      <a :href="src" target="_blank" rel="noopener"><el-button size="small" text type="primary" icon="TopRight">{{ t('pdf.open') }}</el-button></a>
    </div>
    <iframe :src="src" class="pdf" :style="{ height }" title="PDF" />
  </div>
</template>

<style scoped>
.pdf { width: 100%; border: 1px solid var(--el-border-color-lighter); border-radius: var(--air-radius-sm); background: #fff; }
</style>
