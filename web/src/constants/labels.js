// 英文枚举 → 中文和颜色（详细设计 5 第 7 节；与 airesearcher/cli/labels.py 保持一致）。
// 颜色用 Element Plus 的 tag type：primary 蓝、warning 黄、success 绿、info 灰、danger 红。
// 后台新增的值没有对照时显示原始英文，不报错。

export const STATE = {
  IdeaDrafting: ['选题中', 'primary'],
  IdeaPending: ['等待审批 idea', 'warning'],
  PlanDrafting: ['制定实验计划', 'primary'],
  PlanPending: ['等待审批计划', 'warning'],
  Executing: ['实验执行中', 'primary'],
  Analyzing: ['分析结果', 'primary'],
  LogPending: ['等待审批过程日志', 'warning'],
  Writing: ['撰写论文', 'primary'],
  ManuscriptPending: ['等待审批最终手稿', 'warning'],
  Completed: ['已完成', 'success'],
  Paused: ['已暂停', 'info'],
  BudgetExhausted: ['预算已用尽', 'danger'],
  Failed: ['出错停止', 'danger'],
}

export const RUN = {
  created: ['准备中', 'info'],
  preparing: ['准备中', 'info'],
  queued: ['排队中', 'info'],
  running: ['运行中', 'primary'],
  succeeded: ['成功', 'success'],
  failed: ['失败', 'danger'],
  cancelled: ['已取消', 'info'],
  unknown: ['状态未知（需人工确认）', 'warning'],
}

export const FAILURE = {
  nonzero_exit: '程序报错退出', timeout: '超时', cancelled: '已取消', lost: '进程丢失（后台重启时发现）',
  oom: '内存不足', missing_metrics: '未输出必需指标', wrapper_error: '运行包装器出错',
}

export const RUN_LABEL = {
  trusted: ['可信', 'success'],
  suspicious: ['可疑', 'warning'],
  invalid: ['无效', 'danger'],
}

export const APPROVAL = {
  pending: ['待处理', 'warning'],
  approved: ['已批准', 'success'],
  changes_requested: ['已退回', 'warning'],
  rejected: ['已拒绝', 'danger'],
  superseded: ['已被新版本取代', 'info'],
}

export const KIND = {
  idea: ['idea', 'primary'],
  plan: ['实验计划', 'success'],
  log: ['过程日志', 'warning'],
  manuscript: ['最终手稿', 'danger'],
}

export const SUPPORT = {
  supported: ['有依据', 'success'],
  partially_supported: ['部分有依据', 'warning'],
  unsupported: ['无依据', 'danger'],
  contradicted: ['与证据相反', 'danger'],
  unverifiable: ['无法核验', 'info'],
}

export const SEVERITY = { blocker: ['阻断', 'danger'], major: ['重要', 'warning'], minor: ['次要', 'info'] }

export const RUN_KIND = {
  baseline: '基线', main: '主方法', ablation: '消融', control: '对照', replication: '复现',
  exploration: '探索', trial: '试运行', smoke: '冒烟',
}

export const BUDGET = {
  llm_usd: '大模型费用（美元）', llm_tokens: '大模型 token', llm_calls: '大模型调用次数',
  wall_hours: '运行时长（小时）', cpu_hours: 'CPU 时间（小时）', gpu_hours: 'GPU 时间（小时）',
  storage_gb: '存储（GB）',
}

export const ACCESS = {
  full_text: ['全文', 'success'],
  abstract_only: ['仅基于摘要', 'warning'],
  metadata_only: ['仅元数据', 'danger'],
}

export const SECTION = {
  abstract: '摘要', introduction: '引言', related_work: '相关工作', method: '方法', setup: '实验设置',
  results: '结果', discussion: '讨论', conclusion: '结论',
}

export const QUESTION_STATUS = { pending: '待回答', answered: '已回答', withdrawn: '已撤回' }

// 取 [中文, 颜色]；表里没有时返回 [原值, 'info']
export function pair(table, value) {
  if (value === null || value === undefined || value === '') return ['', 'info']
  const v = table[value]
  if (Array.isArray(v)) return v
  return [v ?? String(value), 'info']
}
export const label = (table, value) => pair(table, value)[0]
