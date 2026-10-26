import axios from 'axios'
import { ElMessage } from 'element-plus'
import { USE_MOCK, mockAdapter } from '../mock'
import { lang, t } from '../i18n'

// 详细设计 5 第 5.1 节：axios 实例 + 统一错误提示。后台错误格式：{error: {code, message, detail}}
export const http = axios.create({ baseURL: '/api', timeout: 20000, adapter: USE_MOCK ? mockAdapter : undefined })

// 后台按我们发的语言写 message（见下面的 Accept-Language）；没有 message 时用网页自己的说明
export function errorMessage(err) {
  const e = err?.response?.data?.error
  if (e?.message) return e.message
  if (err?.response) return t('error.http', { status: err.response.status })
  return t('error.offline')
}

// 后台的状态说明、阻塞原因、错误提示按这个语言返回（详细设计 1 第 9.6 节）；不依赖浏览器自带的语言
http.interceptors.request.use((config) => {
  config.headers['Accept-Language'] = lang.value
  return config
})

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
