<script setup>
import { fromNow, fullTime, shortTime } from '../utils/format'

// 本地时间；悬停显示完整时间
defineProps({ value: { type: [String, Date], default: null }, relative: { type: Boolean, default: false } })
</script>

<template>
  <el-tooltip v-if="value" :content="fullTime(value)" placement="top" :show-after="300">
    <span>{{ relative ? fromNow(value) : shortTime(value) }}</span>
  </el-tooltip>
  <span v-else class="muted">—</span>
</template>
