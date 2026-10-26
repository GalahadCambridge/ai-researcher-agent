import { createApp } from 'vue'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './style.css'

import App from './App.vue'
import router from './router'
import icons from './icons'
import { initTheme } from './utils/theme'
import { i18n } from './i18n'

// Element Plus 组件由 vite.config.js 按需引入；它的语言包由 App.vue 的 el-config-provider 跟着界面语言切换
initTheme()
const app = createApp(App)
app.use(icons)
app.use(i18n)
app.use(router)
app.mount('#app')
