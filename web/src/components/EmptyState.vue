<script setup>
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
defineProps({ text: { type: String, default: '' } })
</script>

<template>
  <el-empty :description="text || t('common.noData')" :image-size="80"><slot /></el-empty>
</template>
