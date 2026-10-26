<script setup>
import { computed, onBeforeUnmount, provide, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import StateTag from '../components/StateTag.vue'
import { currentProject } from '../utils/currentProject'
import { useI18n } from 'vue-i18n'
import { getProject } from '../api/projects'
import { subscribeProject } from '../api/stream'
import { errorMessage } from '../api/client'

// 项目内页面的外框：顶部一行项目名（菜单在左侧边栏）。这里订阅一次项目 SSE，把“有东西变了”的通知分发给各页面。
// SSE 断开时显示灰色提示，并退化为每 10 秒轮询（详细设计 5 第 5.3 节）。
const props = defineProps({ pid: { type: String, required: true } })
const route = useRoute()
const { t, locale } = useI18n()

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

// keep：换语言时保留现有数据（不闪一下空白），只重新取一次
function connect(keep = false) {
  unsubscribe()
  stopPolling()
  if (keep !== true) status.value = null
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
      // 每次连上（包括第一次）都补读一次：第一次读状态到 SSE 连上之间、或断线期间的变化不会漏掉
      refresh()
      notice.value = { type: 'poll', at: Date.now() }
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
// 换语言：后台的状态说明、阻塞原因、问题正文要按新语言重新取；SSE 也带着新的 ?lang= 重连
watch(locale, () => {
  if (props.pid) connect(true)
})
onBeforeUnmount(() => {
  unsubscribe()
  stopPolling()
  clearTimeout(refreshTimer)
})

provide('project', { pid: computed(() => props.pid), status, refresh, notice, connected })

// 侧边栏要显示项目名和待审数量
watch([() => props.pid, status, connected], () => Object.assign(currentProject, { pid: props.pid, status: status.value, connected: connected.value }), { immediate: true })
onBeforeUnmount(() => Object.assign(currentProject, { pid: null, status: null }))

const isOverview = computed(() => (route.meta.menu || 'overview') === 'overview')
</script>

<template>
  <div>
    <!-- 总览页自己有大标题；其他页面在顶部显示一行项目名，点击回总览 -->
    <div v-if="status && !isOverview" class="crumb">
      <router-link :to="`/projects/${pid}`" class="ptitle">{{ status.title }}</router-link>
      <StateTag :value="status.state" size="small" />
      <span class="meta">{{ status.stage_label }}</span>
    </div>
    <el-alert v-if="!connected" type="info" :closable="false" show-icon class="conn"
      :title="t('layout.disconnected')" />
    <el-result v-if="loadError" icon="error" :title="t('layout.cannotOpen')" :sub-title="errorMessage(loadError)">
      <template #extra>
        <el-button type="primary" @click="refresh">{{ t('common.retry') }}</el-button>
        <router-link to="/projects"><el-button>{{ t('layout.backToList') }}</el-button></router-link>
      </template>
    </el-result>
    <router-view v-else :key="pid" />
  </div>
</template>

<style scoped>
.crumb { display: flex; align-items: center; gap: 10px; margin: -8px 0 16px; flex-wrap: wrap; font-size: var(--air-fs-meta); }
.ptitle { color: var(--air-text-2); font-weight: 500; }
.ptitle:hover { color: var(--air-text); }
.conn { margin-bottom: 16px; }
</style>
