<script setup>
import { computed } from 'vue'
import { diffLines } from 'diff'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 与上一版的差异。内容超过 3,000 行时退化为左右并列显示两版全文（详细设计 5 第 4.5 节）
const props = defineProps({ oldText: { type: String, default: '' }, newText: { type: String, default: '' } })
const MAX_LINES = 3000
const tooBig = computed(() => props.oldText.split('\n').length + props.newText.split('\n').length > MAX_LINES)
const rows = computed(() => {
  if (tooBig.value) return []
  const out = []
  for (const part of diffLines(props.oldText, props.newText)) {
    const lines = part.value.replace(/\n$/, '').split('\n')
    const type = part.added ? 'add' : part.removed ? 'del' : 'same'
    for (const l of lines) out.push({ type, text: l })
  }
  return out
})
const stats = computed(() => ({
  add: rows.value.filter((r) => r.type === 'add').length,
  del: rows.value.filter((r) => r.type === 'del').length,
}))
</script>

<template>
  <div v-if="tooBig" class="side">
    <el-alert type="info" :closable="false" show-icon :title="t('diff.tooBig')" />
    <div class="cols">
      <div><div class="muted">{{ t('diff.old') }}</div><pre class="code mono">{{ oldText }}</pre></div>
      <div><div class="muted">{{ t('diff.new') }}</div><pre class="code mono">{{ newText }}</pre></div>
    </div>
  </div>
  <div v-else>
    <div class="muted stats"><span class="a">{{ t('diff.added', { n: stats.add }) }}</span> <span class="d">{{ t('diff.removed', { n: stats.del }) }}</span></div>
    <div class="diff mono">
      <div v-for="(r, i) in rows" :key="i" :class="['line', r.type]">
        <span class="sign">{{ r.type === 'add' ? '+' : r.type === 'del' ? '−' : ' ' }}</span>{{ r.text || ' ' }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.stats { margin-bottom: 6px; }
.stats .a { color: var(--el-color-success); }
.stats .d { color: var(--el-color-danger); }
.diff { border: 1px solid var(--el-border-color-lighter); border-radius: 6px; overflow: auto; max-height: 70vh; }
.line { white-space: pre-wrap; word-break: break-all; padding: 0 8px; line-height: 1.6; }
.line.add { background: var(--el-color-success-light-9); }
.line.del { background: var(--el-color-danger-light-9); text-decoration: line-through; text-decoration-color: var(--el-color-danger-light-5); }
.sign { display: inline-block; width: 14px; color: var(--el-text-color-secondary); user-select: none; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px; }
.cols pre { max-height: 70vh; }
</style>
