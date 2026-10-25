<script setup>
import { computed, onBeforeUnmount, provide, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import StateTag from '../components/StateTag.vue'
import { getProject } from '../api/projects'
import { subscribeProject } from '../api/stream'
import { errorMessage } from '../api/client'

// 项目内页面的外框：左侧菜单 + 顶部项目标题。这里订阅一次项目 SSE，把“有东西变了”的通知分发给各页面。
// SSE 断开时显示灰色提示，并退化为每 10 秒轮询（详细设计 5 第 5.3 节）。
const props = defineProps({ pid: { type: String, required: true } })
const route = useRoute()

const status = ref(null)
const loadError = ref(null)
const connected = ref(true)
const notice = shallowRef(null) // {type, data, at}

async function refresh() {
  try {
    status.value = await getProject(props.pid)
    loadError.value = null
  } catch (e) {
    if (!status.value) loadError.value = e
  }
}

let unsubscribe = () => {}
let pollTimer = null
let refreshTimer = null
function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    refresh()
    notice.value = { type: 'poll', at: Date.now() }
  }, 10000)
}
function stopPolling() {
  clearInterval(pollTimer)
  pollTimer = null
}

function connect() {
  unsubscribe()
  stopPolling()
  status.value = null
  refresh()
  unsubscribe = subscribeProject(props.pid, {
    onMessage(type, data) {
      notice.value = { type, data, at: Date.now() }
      if (type !== 'event') { // 状态、审批、问题、运行、预算有变化：0.5 秒内重新读一次状态（合并连续通知）
        clearTimeout(refreshTimer)
        refreshTimer = setTimeout(refresh, 500)
      }
    },
    onOpen() {
      if (!connected.value) { // 重连成功：补读一次，防止漏掉断线期间的变化
        refresh()
        notice.value = { type: 'poll', at: Date.now() }
      }
      connected.value = true
      stopPolling()
    },
    onDisconnect() {
      connected.value = false
      startPolling()
    },
    onUnavailable() {
      startPolling()
    },
  })
}
watch(() => props.pid, connect, { immediate: true })
onBeforeUnmount(() => {
  unsubscribe()
  stopPolling()
  clearTimeout(refreshTimer)
})

provide('project', { pid: computed(() => props.pid), status, refresh, notice, connected })

// 换页面时回到顶部（内容区是自己滚动的，路由不会帮它复位）
const pageEl = ref(null)
watch(() => route.path, () => { if (pageEl.value) pageEl.value.scrollTop = 0 })

const menu = computed(() => route.meta.menu || 'overview')
const base = computed(() => `/projects/${props.pid}`)
const pendingCount = computed(() => status.value?.pending_approvals?.length || 0)
const items = computed(() => [
  { key: 'overview', to: '', icon: 'Odometer', label: '总览' },
  { key: 'approvals', to: '/approvals', icon: 'Stamp', label: '审批', badge: pendingCount.value },
  { key: 'runs', to: '/runs', icon: 'Cpu', label: '运行' },
  { key: 'literature', to: '/literature', icon: 'Collection', label: '文献' },
  { key: 'figures', to: '/figures', icon: 'PieChart', label: '图表' },
  { key: 'paper', to: '/paper', icon: 'Document', label: '论文' },
  { key: 'events', to: '/events', icon: 'List', label: '事件日志' },
  { key: 'templates', to: '/templates', icon: 'Files', label: '模板' },
])
</script>

<template>
  <div class="layout">
    <aside class="side">
      <el-menu :default-active="menu" router class="menu">
        <el-menu-item v-for="it in items" :key="it.key" :index="it.key" :route="base + it.to">
          <el-icon><component :is="it.icon" /></el-icon>
          <span>{{ it.label }}</span>
          <el-badge v-if="it.badge" :value="it.badge" type="danger" class="badge" />
        </el-menu-item>
      </el-menu>
    </aside>
    <section class="content">
      <div class="project-bar">
        <router-link :to="base" class="ptitle">{{ status?.title || pid }}</router-link>
        <StateTag v-if="status" :value="status.state" />
        <span class="muted">{{ status?.stage_label }}</span>
        <span class="spacer" />
        <span class="muted mono pid">{{ pid }}</span>
      </div>
      <el-alert v-if="!connected" type="info" :closable="false" show-icon class="conn"
        title="与后台的连接已断开，正在重连……（期间每 10 秒自动刷新一次）" />
      <div ref="pageEl" class="page">
        <el-result v-if="loadError" icon="error" title="打不开这个项目" :sub-title="errorMessage(loadError)">
          <template #extra>
            <el-button type="primary" @click="refresh">重试</el-button>
            <router-link to="/projects"><el-button>返回项目列表</el-button></router-link>
          </template>
        </el-result>
        <router-view v-else :key="pid" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.layout { display: flex; height: 100%; min-height: 0; flex: 1; }
.side { width: 180px; flex: none; background: var(--el-bg-color); border-right: 1px solid var(--el-border-color-lighter); overflow: auto; }
.menu { border-right: none; }
.badge { margin-left: auto; display: flex; }
.badge :deep(.el-badge__content) { position: static; transform: none; }
.content { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.project-bar {
  display: flex; align-items: center; gap: 10px; padding: 10px 24px; background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter); flex-wrap: wrap;
}
.ptitle { font-weight: 600; font-size: 15px; color: inherit; text-decoration: none; }
.pid { font-size: 12px; }
.conn { border-radius: 0; }
.page { padding: 20px 24px; overflow: auto; flex: 1; }
</style>
