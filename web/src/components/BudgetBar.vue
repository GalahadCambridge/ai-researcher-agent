<script setup>
import { computed } from 'vue'
import { BUDGET, label } from '../constants/labels'
import { num } from '../utils/format'

// 已用 / 上限；超过 soft 变黄，达到 hard 变红
const props = defineProps({ name: String, cat: { type: Object, required: true } })
const limit = computed(() => props.cat.hard ?? props.cat.soft)
const pct = computed(() => (limit.value ? Math.min(100, (props.cat.used / limit.value) * 100) : 0))
const status = computed(() => {
  const c = props.cat
  if (c.level === 'exhausted' || (c.hard != null && c.used >= c.hard)) return 'exception'
  if (c.level === 'warn' || (c.soft != null && c.used >= c.soft)) return 'warning'
  return ''
})
</script>

<template>
  <div class="budget">
    <div class="head">
      <span>{{ label(BUDGET, name) }}</span>
      <span class="muted mono">
        {{ num(cat.used) }} / {{ limit != null ? num(limit) : '不限' }}
        <template v-if="cat.soft != null && cat.hard != null">（提醒 {{ num(cat.soft) }}）</template>
      </span>
    </div>
    <el-progress :percentage="Number(pct.toFixed(1))" :status="status" :show-text="false" :stroke-width="8" />
  </div>
</template>

<style scoped>
.budget + .budget { margin-top: 12px; }
.head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; gap: 8px; }
</style>
