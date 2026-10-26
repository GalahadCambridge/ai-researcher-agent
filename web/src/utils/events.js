import { i18n, lang, t } from '../i18n'
import { APPROVAL, KIND, RUN, RUN_LABEL, STATE, label } from '../constants/labels'

// 事件的显示文字。后台存的 summary 是中文（老数据、CLI 都靠它），所以中文界面直接用 summary；
// 英文界面按 type + data 套模板（i18n/en.js 的 events.tpl），没有模板的类型仍显示 summary（详细设计 1 第 9.6 节）
export function eventText(e) {
  if (lang.value === 'zh') return e.summary
  const key = `events.tpl.${e.type}`
  if (!i18n.global.te(key, lang.value)) return e.summary
  const d = e.data || {}
  const ref = e.refs?.[0]
  return t(key, {
    ...d,
    run_id: e.run_id ?? d.run_id,
    artifact: ref?.artifact_id ?? '',
    version: ref?.version ?? '',
    from: label(STATE, d.from),
    to: label(STATE, d.to),
    kind: label(KIND, d.kind),
    decision: label(APPROVAL, d.decision),
    state: label(RUN, d.state),
    label: label(RUN_LABEL, d.label),
  })
}
