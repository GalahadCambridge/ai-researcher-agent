<script setup>
import { computed } from 'vue'
import MarkdownView from './MarkdownView.vue'

// 按文件类型显示文本内容：Markdown 渲染、JSON / JSONL 格式化、其余等宽显示
const props = defineProps({ name: { type: String, default: '' }, content: { type: String, default: '' } })
const kind = computed(() => {
  const n = props.name.toLowerCase()
  if (n.endsWith('.md')) return 'md'
  if (n.endsWith('.json')) return 'json'
  if (n.endsWith('.jsonl')) return 'jsonl'
  return 'text'
})
const pretty = computed(() => {
  try {
    if (kind.value === 'json') return JSON.stringify(JSON.parse(props.content), null, 2)
    if (kind.value === 'jsonl') {
      return props.content.split('\n').filter((l) => l.trim()).map((l) => JSON.stringify(JSON.parse(l), null, 2)).join('\n\n')
    }
  } catch {
    /* 不是合法 JSON 时原样显示 */
  }
  return props.content
})
</script>

<template>
  <MarkdownView v-if="kind === 'md'" :source="content" />
  <pre v-else class="code mono">{{ pretty }}</pre>
</template>
