// 假数据模式（详细设计 5 第 5.2 节）：`npm run dev:mock` 时所有 GET 请求返回 mock/data/ 下的 JSON，
// 写操作一律提示“假数据模式下不能提交”。数据是样例项目的真实返回值，用 `npm run mock:capture` 重新抓取。
export const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'

const files = import.meta.glob('./data/*.json', { eager: true, import: 'default' })
const data = (name) => files[`./data/${name}.json`]

// [路径正则, 文件名]；从上到下匹配第一条
const ROUTES = [
  [/^\/tasks$/, 'tasks'],
  [/^\/projects$/, 'projects'],
  [/^\/projects\/[^/]+$/, 'project_status'],
  [/^\/projects\/[^/]+\/approvals$/, 'approvals'],
  [/^\/projects\/[^/]+\/approvals\/([^/]+)$/, (m) => `approval_${m[1]}`],
  [/^\/projects\/[^/]+\/runs$/, 'runs'],
  [/^\/projects\/[^/]+\/runs\/[^/]+\/logs$/, 'run_logs'],
  [/^\/projects\/[^/]+\/runs\/[^/]+$/, 'run_detail'],
  [/^\/projects\/[^/]+\/literature$/, 'literature'],
  [/^\/projects\/[^/]+\/literature\/.+$/, 'literature_record'],
  [/^\/projects\/[^/]+\/figures$/, 'figures'],
  [/^\/projects\/[^/]+\/paper$/, 'paper'],
  [/^\/projects\/[^/]+\/claims$/, 'claims'],
  [/^\/projects\/[^/]+\/claims\/[^/]+\/trace$/, 'claim_trace'],
  [/^\/projects\/[^/]+\/templates$/, 'templates'],
  [/^\/projects\/[^/]+\/events$/, 'events'],
  [/^\/projects\/[^/]+\/checkpoints$/, 'checkpoints'],
  [/^\/projects\/[^/]+\/question$/, () => null],
]

function reply(config, status, body) {
  const res = { data: body, status, statusText: String(status), headers: {}, config }
  if (status >= 400) {
    const err = new Error(body?.error?.message || 'mock error')
    err.response = res
    err.config = config
    return Promise.reject(err)
  }
  return Promise.resolve(res)
}

export function mockAdapter(config) {
  const path = (config.url || '').split('?')[0]
  if ((config.method || 'get').toLowerCase() !== 'get') {
    return reply(config, 400, { error: { code: 'MOCK', message: '假数据模式下不能提交操作（请启动 air serve 并用 npm run dev）' } })
  }
  for (const [re, name] of ROUTES) {
    const m = path.match(re)
    if (!m) continue
    const key = typeof name === 'function' ? name(m) : name
    if (key === null) return reply(config, 200, null)
    const body = data(key)
    if (body === undefined) break
    let out = body
    if (config.responseType === 'blob') out = new Blob([JSON.stringify(body)], { type: 'application/json' })
    return reply(config, 200, out)
  }
  return reply(config, 404, { error: { code: 'NOT_FOUND', message: `假数据中没有 ${path}` } })
}
