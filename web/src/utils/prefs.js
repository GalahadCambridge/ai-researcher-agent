// 只用来存“界面偏好”（详细设计 5 第 5.4 节），读写失败不影响使用
export function getPref(key, fallback) {
  try {
    const v = localStorage.getItem('air.' + key)
    return v === null ? fallback : JSON.parse(v)
  } catch {
    return fallback
  }
}
export function setPref(key, value) {
  try {
    localStorage.setItem('air.' + key, JSON.stringify(value))
  } catch {
    /* 忽略 */
  }
}
