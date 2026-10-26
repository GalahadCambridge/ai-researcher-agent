import { inject, onBeforeUnmount, ref, watch } from 'vue'

// 每个页面的“加载中 / 出错 / 数据”三态（详细设计 5 第 4.12 节）。
// load(true) 是静默刷新：不显示加载中、出错时保留旧数据（SSE 通知后用）。
export function useLoader(fn) {
  const data = ref(null)
  const loading = ref(false)
  const error = ref(null)
  let seq = 0
  async function load(silent = false) {
    const my = ++seq
    if (!silent) loading.value = true
    try {
      const v = await fn()
      if (my === seq) {
        data.value = v
        error.value = null
      }
    } catch (e) {
      if (my === seq && (!silent || data.value === null)) error.value = e
    } finally {
      if (my === seq) loading.value = false
    }
  }
  return { data, loading, error, load }
}

// 项目内页面共享的上下文（ProjectLayout 提供）：项目编号、状态、SSE 通知
export const useProject = () => inject('project')

// 收到指定类型的 SSE 通知（或断线时的轮询）后，0.5 秒内静默刷新一次；多条通知只刷新一次
export function useProjectRefresh(reload, types = null) {
  const ctx = useProject()
  let timer = null
  watch(
    () => ctx.notice.value,
    (n) => {
      if (!n || (types && n.type !== 'poll' && !types.includes(n.type))) return
      clearTimeout(timer)
      timer = setTimeout(() => reload(true), 500)
    },
  )
  onBeforeUnmount(() => clearTimeout(timer))
}
