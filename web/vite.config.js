import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时把 /api 代理到后台（air serve）；改端口时设置 AIR_SERVER，如 AIR_SERVER=http://127.0.0.1:8766
const target = process.env.AIR_SERVER || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': { target, changeOrigin: true } } },
  build: { chunkSizeWarningLimit: 1500 },
})
