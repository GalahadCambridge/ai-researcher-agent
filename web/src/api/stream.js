import { USE_MOCK } from '../mock'
import { lang } from '../i18n'

// 详细设计 5 第 5.3 节：SSE 只当“通知”，收到后页面重新请求 REST 接口拿完整数据
export const PROJECT_EVENTS = ['state', 'event', 'approval', 'question', 'run', 'budget']

export function subscribeProject(pid, handlers) {
  if (USE_MOCK || typeof EventSource === 'undefined') {
    handlers.onUnavailable?.()
    return () => {}
  }
  // EventSource 不能加请求头，语言用 ?lang= 传
  const es = new EventSource(`/api/projects/${pid}/stream?from_now=true&lang=${lang.value}`)
  for (const type of PROJECT_EVENTS) {
    es.addEventListener(type, (msg) => handlers.onMessage?.(type, JSON.parse(msg.data)))
  }
  es.onopen = () => handlers.onOpen?.()
  es.onerror = () => handlers.onDisconnect?.() // 浏览器会自动重连
  return () => es.close()
}

// 运行日志：每行一条 line，最后一条 end；收到 end 必须 close()，否则浏览器会自动重连
export function subscribeRunLog(pid, rid, stream, offset, { onLine, onEnd, onError }) {
  if (USE_MOCK || typeof EventSource === 'undefined') {
    onError?.()
    return () => {}
  }
  const es = new EventSource(`/api/projects/${pid}/runs/${rid}/logs/stream?stream=${stream}&offset=${offset}&lang=${lang.value}`)
  es.addEventListener('line', (m) => onLine(JSON.parse(m.data).text))
  es.addEventListener('end', (m) => {
    es.close()
    onEnd?.(JSON.parse(m.data))
  })
  es.onerror = () => onError?.()
  return () => es.close()
}
