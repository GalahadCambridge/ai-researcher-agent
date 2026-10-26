import { reactive } from 'vue'

// 当前打开的项目（ProjectLayout 写入，侧边栏读取：项目名、待审数量）
export const currentProject = reactive({ pid: null, status: null, connected: true })
