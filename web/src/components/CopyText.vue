<script setup>
import { copyText } from '../utils/format'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 文件路径、运行编号一键复制
defineProps({ text: { type: String, required: true }, mono: { type: Boolean, default: true } })
</script>

<template>
  <span class="copy-text" :class="{ mono }">
    <slot>{{ text }}</slot>
    <el-icon class="copy-icon" :title="t('common.copy')" @click.stop="copyText(text)"><CopyDocument /></el-icon>
  </span>
</template>

<style scoped>
.copy-text { display: inline-flex; align-items: center; gap: 4px; word-break: break-all; }
.copy-icon { cursor: pointer; color: var(--el-text-color-secondary); flex: none; }
.copy-icon:hover { color: var(--el-color-primary); }
</style>
