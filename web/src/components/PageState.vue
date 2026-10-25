<script setup>
import EmptyState from './EmptyState.vue'
import { errorMessage } from '../api/client'

// 加载中 / 出错（提示 + 重试）/ 空状态 / 内容
defineProps({
  loading: Boolean,
  error: { type: Object, default: null },
  empty: Boolean,
  emptyText: { type: String, default: '暂无数据' },
  rows: { type: Number, default: 6 },
})
defineEmits(['retry'])
</script>

<template>
  <el-skeleton v-if="loading" :rows="rows" animated />
  <el-result v-else-if="error" icon="error" title="加载失败" :sub-title="errorMessage(error)">
    <template #extra><el-button type="primary" @click="$emit('retry')">重试</el-button></template>
  </el-result>
  <EmptyState v-else-if="empty" :text="emptyText" />
  <slot v-else />
</template>
