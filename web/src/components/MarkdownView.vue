<script setup>
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { splitFrontmatter } from '../utils/format'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// 显示 Markdown；文件开头的 YAML 区块折叠显示为代码。reading：阅读模式（衬线字体、较宽行距、限制行宽）
const props = defineProps({ source: { type: String, default: '' }, reading: { type: Boolean, default: false } })
const md = new MarkdownIt({ html: false, linkify: true, breaks: false })
const parts = computed(() => splitFrontmatter(props.source))
const html = computed(() => md.render(parts.value.body))
</script>

<template>
  <div class="markdown">
    <el-collapse v-if="parts.yaml" class="yaml">
      <el-collapse-item :title="t('markdown.yaml')" name="yaml">
        <pre class="code mono">{{ parts.yaml }}</pre>
      </el-collapse-item>
    </el-collapse>
    <div :class="['md-body', { reading }]" v-html="html" />
  </div>
</template>

<style scoped>
.yaml { margin-bottom: 12px; }
.md-body { line-height: 1.7; word-break: break-word; }
.md-body > :deep(:first-child) { margin-top: 0; }
.md-body :deep(table) { border-collapse: collapse; margin: 8px 0; display: block; overflow-x: auto; }
.md-body :deep(th), .md-body :deep(td) { border: 1px solid var(--el-border-color); padding: 4px 8px; }
.md-body :deep(th) { background: var(--el-fill-color-light); }
.md-body :deep(pre) { background: var(--el-fill-color-light); padding: 10px; border-radius: 6px; overflow: auto; }
.md-body :deep(code) { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; }
.md-body :deep(blockquote) { border-left: 3px solid var(--el-border-color); margin: 8px 0; padding: 0 12px; color: var(--el-text-color-secondary); }
.md-body :deep(h1) { font-size: 1.5em; }
.md-body :deep(h2) { font-size: 1.3em; border-bottom: 1px solid var(--el-border-color-lighter); padding-bottom: 4px; }
</style>
