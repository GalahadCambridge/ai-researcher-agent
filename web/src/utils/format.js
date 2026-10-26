import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import { ElMessage } from 'element-plus'
import { t } from '../i18n'

export { getPref, setPref } from './prefs'

dayjs.extend(relativeTime) // 语言由 i18n/index.js 设置

export const fullTime = (v) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '')
export const shortTime = (v) => (v ? dayjs(v).format('MM-DD HH:mm') : '')
export const fromNow = (v) => (v ? dayjs(v).fromNow() : '')

export function duration(start, end) {
  if (!start) return ''
  const s = Math.max(0, (end ? dayjs(end) : dayjs()).diff(dayjs(start), 'second', true))
  if (s < 60) return t('time.seconds', { n: s.toFixed(s < 10 ? 1 : 0) })
  if (s < 3600) return t('time.minSec', { m: Math.floor(s / 60), s: Math.round(s % 60) })
  return t('time.hourMin', { h: Math.floor(s / 3600), m: Math.round((s % 3600) / 60) })
}

export function num(v, digits = 4) {
  if (v === null || v === undefined) return ''
  if (typeof v !== 'number') return String(v)
  if (Number.isInteger(v)) return String(v)
  return String(Number(v.toFixed(digits)))
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(t('common.copied'))
  } catch {
    ElMessage.warning(t('common.copyFailed'))
  }
}

// 把 selected.md 这类“YAML 头 + Markdown 正文”拆开
export function splitFrontmatter(text) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text || '')
  if (!m) return { yaml: '', body: text || '' }
  return { yaml: m[1], body: text.slice(m[0].length) }
}

// 论断文本：去掉 \claim{ID}{...} 外壳，把 \airval{key} 换成实际数字
export function renderClaimText(claim) {
  let t = claim?.text || ''
  const m = /^\\claim\{[^}]*\}\{([\s\S]*)\}$/.exec(t.trim())
  if (m) t = m[1]
  const values = Object.fromEntries((claim?.values || []).map((v) => [v.key, v.rendered]))
  t = t.replace(/\\airval\{([^}]*)\}/g, (_, k) => values[k] ?? `[${k}]`)
  return t.replace(/\\%/g, '%').replace(/\\cite[pt]?\{([^}]*)\}/g, '[$1]').replace(/~/g, ' ')
}
