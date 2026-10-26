<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { fromNow, fullTime, shortTime } from '../utils/format'

// 本地时间；悬停显示完整时间。“3 分钟前”跟着界面语言变（dayjs 本身不是响应式的，所以显式依赖 locale）
const props = defineProps({ value: { type: [String, Date], default: null }, relative: { type: Boolean, default: false } })
const { locale } = useI18n()
const text = computed(() => (locale.value, props.relative ? fromNow(props.value) : shortTime(props.value)))
</script>

<template>
  <el-tooltip v-if="value" :content="fullTime(value)" placement="top" :show-after="300">
    <span>{{ text }}</span>
  </el-tooltip>
  <span v-else class="muted">—</span>
</template>
