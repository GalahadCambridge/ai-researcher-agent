<script setup>
import EmptyState from './EmptyState.vue'
import { errorMessage } from '../api/client'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 加载中 / 出错（提示 + 重试）/ 空状态 / 内容
defineProps({
  loading: Boolean,
  error: { type: Object, default: null },
  empty: Boolean,
  emptyText: { type: String, default: '' },
  rows: { type: Number, default: 6 },
})
defineEmits(['retry'])
</script>

<template>
  <el-skeleton v-if="loading" :rows="rows" animated />
  <el-result v-else-if="error" icon="error" :title="t('common.loadFailed')" :sub-title="errorMessage(error)">
    <template #extra><el-button type="primary" @click="$emit('retry')">{{ t('common.retry') }}</el-button></template>
  </el-result>
  <EmptyState v-else-if="empty" :text="emptyText" />
  <slot v-else />
</template>
