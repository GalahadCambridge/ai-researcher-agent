import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// 开发时把 /api 代理到后台（air serve）；改端口时设置 AIR_SERVER，如 AIR_SERVER=http://127.0.0.1:8766
const target = process.env.AIR_SERVER || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板里用到哪个组件（含 v-loading 等指令）才打包哪个；样式在 main.js 统一引入
    Components({ resolvers: [ElementPlusResolver({ importStyle: false })], dts: false }),
  ],
  server: { proxy: { '/api': { target, changeOrigin: true } } },
  build: {
    rollupOptions: {
      output: {
        // Vue 全家桶（含 vue-i18n）单独打包（页面代码改了，浏览器仍能用缓存）；Element Plus 组件跟着用到它的页面拆分
        manualChunks(id) {
          if (/node_modules\/(@vue|vue|vue-router|vue-i18n|@intlify)\//.test(id)) return 'vue'
        },
      },
    },
  },
})
