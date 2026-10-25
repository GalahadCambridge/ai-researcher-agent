import axios from 'axios'
import { ElMessage } from 'element-plus'
import { USE_MOCK, mockAdapter } from '../mock'

// 详细设计 5 第 5.1 节：axios 实例 + 统一错误提示。后台错误格式：{error: {code, message, detail}}
export const http = axios.create({ baseURL: '/api', timeout: 20000, adapter: USE_MOCK ? mockAdapter : undefined })

export function errorMessage(err) {
  const e = err?.response?.data?.error
  if (e?.message) return e.message
  if (err?.response) return `请求失败（HTTP ${err.response.status}）`
  return '无法连接后台，请确认 air serve 已启动'
}

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    // 409（版本过期、已被处理）由调用方处理；quiet 的请求（如页面自己显示错误）不弹提示
    if (err.response?.status !== 409 && !err.config?.quiet) ElMessage.error(errorMessage(err))
    return Promise.reject(err)
  },
)

export const isConflict = (err) => err?.response?.status === 409

// 每次用户操作生成一个；网络重试时沿用同一个，后台只处理一次（第 6 节）
export function newRequestId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID()
  return 'req-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)
}

export const apiUrl = (path) => '/api' + path
