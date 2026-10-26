import { t, te } from '../i18n'

// 英文枚举 → 显示文字和颜色（详细设计 5 第 7 节）。文字在 i18n/zh-CN.js、en.js 的 labels.<表名>.<值>，
// 这里只登记颜色（Element Plus 的 tag type：primary 强调色、warning 黄、success 绿、info 灰、danger 红）。
// 后台新增的值没有对照时显示原始英文，不报错。

function table(ns, colors) {
  Object.defineProperty(colors, '__ns', { value: ns }) // 不可枚举：Object.keys 只得到值
  return colors
}

export const STATE = table('state', {
  IdeaDrafting: 'primary', IdeaPending: 'warning', PlanDrafting: 'primary', PlanPending: 'warning',
  Executing: 'primary', Analyzing: 'primary', LogPending: 'warning', Writing: 'primary',
  ManuscriptPending: 'warning', Completed: 'success', Paused: 'info', BudgetExhausted: 'danger', Failed: 'danger',
})
export const RUN = table('run', {
  created: 'info', preparing: 'info', queued: 'info', running: 'primary', succeeded: 'success',
  failed: 'danger', cancelled: 'info', unknown: 'warning',
})
export const FAILURE = table('failure', {
  nonzero_exit: null, timeout: null, cancelled: null, lost: null, oom: null, missing_metrics: null, wrapper_error: null,
})
export const RUN_LABEL = table('runLabel', { trusted: 'success', suspicious: 'warning', invalid: 'danger' })
export const APPROVAL = table('approval', {
  pending: 'warning', approved: 'success', changes_requested: 'warning', rejected: 'danger', superseded: 'info',
})
export const KIND = table('kind', { idea: 'primary', plan: 'success', log: 'warning', manuscript: 'danger' })
export const SUPPORT = table('support', {
  supported: 'success', partially_supported: 'warning', unsupported: 'danger', contradicted: 'danger', unverifiable: 'info',
})
export const SEVERITY = table('severity', { blocker: 'danger', major: 'warning', minor: 'info' })
export const RUN_KIND = table('runKind', {
  baseline: null, main: null, ablation: null, control: null, replication: null, exploration: null, trial: null, smoke: null,
})
export const BUDGET = table('budget', {
  llm_usd: null, llm_tokens: null, llm_calls: null, wall_hours: null, cpu_hours: null, gpu_hours: null, storage_gb: null,
})
export const ACCESS = table('access', { full_text: 'success', abstract_only: 'warning', metadata_only: 'danger' })
export const SECTION = table('section', {
  abstract: null, introduction: null, related_work: null, method: null, setup: null, results: null, discussion: null, conclusion: null,
})
export const QUESTION_STATUS = table('questionStatus', { pending: null, answered: null, withdrawn: null })
export const CITATION = table('citation', { verified: 'success', metadata_only: 'warning', not_in_archive: 'danger', source_unreachable: 'info' })
export const STAGE = table('stage', { idea: null, plan: null, experiment: null, writing: null })

// 取 [文字, 颜色]；表里没有时返回 [原值, 'info']
export function pair(tbl, value) {
  if (value === null || value === undefined || value === '') return ['', 'info']
  const key = `labels.${tbl.__ns}.${value}`
  return [te(key) ? t(key) : String(value), tbl[value] || 'info']
}
export const label = (tbl, value) => pair(tbl, value)[0]
