<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { http } from './api/client'
import { USE_MOCK } from './mock'

// 整体布局：顶栏 + 内容区（项目内页面由 ProjectLayout 再加左侧菜单）。每 15 秒检查一次后台是否在线
const online = ref(true)
let timer = null
async function ping() {
  if (USE_MOCK) return
  try {
    await http.get('/health', { quiet: true, timeout: 5000 })
    online.value = true
  } catch {
    online.value = false
  }
}
onMounted(() => { ping(); timer = setInterval(ping, 15000) })
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <el-container class="app">
    <el-header class="top">
      <router-link to="/projects" class="brand">
        <el-icon size="22"><Reading /></el-icon>
        <span>AI Researcher</span>
      </router-link>
      <router-link to="/projects" class="nav">项目列表</router-link>
      <router-link to="/projects/new" class="nav">新建项目</router-link>
      <span class="spacer" />
      <el-tag v-if="USE_MOCK" type="warning" effect="dark">假数据模式</el-tag>
      <a href="/docs" target="_blank" rel="noopener" class="nav muted">接口文档</a>
    </el-header>
    <el-alert v-if="!online" type="error" :closable="false" show-icon center
      title="连不上后台：请在终端运行 air serve（保持窗口开着），页面会自动恢复" />
    <el-main class="main"><router-view /></el-main>
  </el-container>
</template>

<style scoped>
.app { height: 100%; flex-direction: column; }
.top {
  display: flex; align-items: center; gap: 20px; background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter); height: 52px;
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 17px; color: var(--el-color-primary); text-decoration: none; }
.nav { color: var(--el-text-color-regular); text-decoration: none; font-size: 14px; }
.nav.router-link-exact-active { color: var(--el-color-primary); font-weight: 600; }
.main { padding: 0; overflow: hidden; display: flex; flex-direction: column; }
.main > :deep(.page) { padding: 20px 24px; overflow: auto; flex: 1; }
</style>
