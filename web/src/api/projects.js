import { http } from './client'

const q = { quiet: true } // 页面自己显示“出错 + 重试”的读取请求

export const listTasks = () => http.get('/tasks', q)
export const getDefaults = () => http.get('/defaults', q) // 新建项目的默认值：任务、运行模式、预算
export const listProjects = () => http.get('/projects', q)
export const createProject = (body) => http.post('/projects', body)
export const getProject = (pid, opts = {}) => http.get(`/projects/${pid}`, { ...q, ...opts })
export const projectAction = (pid, action, requestId) =>
  http.post(`/projects/${pid}/actions`, { action, request_id: requestId })
export const getQuestion = (pid) => http.get(`/projects/${pid}/question`, q)
export const answerQuestion = (pid, body) => http.post(`/projects/${pid}/question/answer`, body)
export const patchBudget = (pid, body) => http.patch(`/projects/${pid}/budget`, body)
export const listEvents = (pid, params) => http.get(`/projects/${pid}/events`, { ...q, params })
// 最新 N 条（仍按时间正序），后台从文件末尾往前读
export const latestEvents = (pid, n, params = {}) => http.get(`/projects/${pid}/events`, { ...q, params: { ...params, tail: n } })
export const listCheckpoints = (pid) => http.get(`/projects/${pid}/checkpoints`, q)
export const rollback = (pid, checkpointId, requestId) =>
  http.post(`/projects/${pid}/rollback`, { checkpoint_id: checkpointId, request_id: requestId })
export const listTemplates = (pid) => http.get(`/projects/${pid}/templates`, q)
export const uploadTemplate = (pid, file, { templateId, requestId } = {}) => {
  const form = new FormData()
  form.append('file', file)
  form.append('request_id', requestId || '')
  if (templateId) form.append('template_id', templateId)
  return http.post(`/projects/${pid}/templates`, form, { timeout: 120000 })
}
export const exportUrl = (pid, what) => `/api/projects/${pid}/export?what=${what}`
