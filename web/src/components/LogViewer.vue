<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { getLogs } from '../api/runs'
import { subscribeRunLog } from '../api/stream'
import { getPref, setPref } from '../utils/format'
import { USE_MOCK } from '../mock'

// 运行日志：stdout / stderr 两个标签页。运行中时实时追加（SSE），结束后一次读完。
// 只保留最近 5,000 行（详细设计 5 第 4.6 节）。
const props = defineProps({ pid: String, runId: String, active: Boolean })
const emit = defineEmits(['ended'])
const MAX_LINES = 5000

const tab = ref('stdout')
const autoScroll = ref(getPref('log.autoScroll', true))
watch(autoScroll, (v) => setPref('log.autoScroll', v))

const logs = { stdout: makeState(), stderr: makeState() }
function makeState() {
  return { lines: ref([]), dropped: ref(0), live: ref(false), loading: ref(false), error: ref(''), stop: null, started: false }
}
const boxes = { stdout: ref(null), stderr: ref(null) }

function push(s, text) {
  s.lines.value.push(text)
  const extra = s.lines.value.length - MAX_LINES
  if (extra > 0) {
    s.lines.value.splice(0, extra)
    s.dropped.value += extra
  }
}

function scrollDown(stream) {
  if (!autoScroll.value) return
  nextTick(() => {
    const el = boxes[stream].value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function readAll(stream) {
  const s = logs[stream]
  s.loading.value = true
  try {
    let offset = 0
    let rest = ''
    for (let i = 0; i < 50; i++) { // 最多读 50 MB
      const chunk = await getLogs(props.pid, props.runId, stream, offset)
      const parts = (rest + chunk.text).split('\n')
      rest = parts.pop()
      for (const l of parts) push(s, l)
      if (chunk.eof || !chunk.text || chunk.next_offset === offset) break
      offset = chunk.next_offset
    }
    if (rest) push(s, rest)
    s.error.value = ''
  } catch {
    s.error.value = '读取日志失败'
  } finally {
    s.loading.value = false
    scrollDown(stream)
  }
}

function follow(stream) {
  const s = logs[stream]
  s.live.value = true
  s.stop = subscribeRunLog(props.pid, props.runId, stream, 0, {
    onLine: (text) => {
      push(s, text)
      scrollDown(stream)
    },
    onEnd: (info) => {
      s.live.value = false
      s.stop = null
      emit('ended', info)
    },
    onError: () => {
      if (USE_MOCK) { // 假数据模式没有 SSE：退化为一次性读取
        s.live.value = false
        readAll(stream)
      }
    },
  })
}

function start(stream) {
  const s = logs[stream]
  if (s.started) return
  s.started = true
  s.lines.value = []
  s.dropped.value = 0
  if (props.active && !USE_MOCK) follow(stream)
  else readAll(stream)
}

function stopAll() {
  for (const s of Object.values(logs)) {
    s.stop?.()
    s.stop = null
    s.live.value = false
    s.started = false
  }
}

watch(() => [props.pid, props.runId], () => { stopAll(); start(tab.value) }, { immediate: true })
watch(tab, (t) => start(t))
// 运行从“排队”变成“运行中”等：已经读完的不需要重读；从结束变回活动不会发生
watch(() => props.active, (a, was) => {
  if (a && !was) { stopAll(); start(tab.value) }
})
onBeforeUnmount(stopAll)
</script>

<template>
  <div>
    <div class="toolbar">
      <el-radio-group v-model="tab" size="small">
        <el-radio-button value="stdout">stdout</el-radio-button>
        <el-radio-button value="stderr">stderr</el-radio-button>
      </el-radio-group>
      <el-tag v-if="logs[tab].live.value" type="success" size="small" effect="plain">实时更新中</el-tag>
      <span class="spacer" />
      <el-switch v-model="autoScroll" active-text="自动滚动到底部" size="small" />
      <router-link :to="`/projects/${pid}/files?path=${encodeURIComponent(`runs/${runId}/${tab}.log`)}`">
        <el-button size="small" text type="primary">查看完整日志</el-button>
      </router-link>
    </div>
    <el-alert v-if="logs[tab].dropped.value" type="info" :closable="false" class="drop"
      :title="`只显示最近 ${MAX_LINES} 行（前面 ${logs[tab].dropped.value} 行已省略），完整日志见“查看完整日志”`" />
    <el-alert v-if="logs[tab].error.value" type="error" :closable="false" :title="logs[tab].error.value" />
    <div :ref="(el) => (boxes[tab].value = el)" v-loading="logs[tab].loading.value" class="log mono">
      <div v-if="!logs[tab].lines.value.length && !logs[tab].loading.value" class="muted">（没有输出）</div>
      <div v-for="(l, i) in logs[tab].lines.value" :key="i" class="ln">{{ l || ' ' }}</div>
    </div>
  </div>
</template>

<style scoped>
.log {
  background: #1e1e1e; color: #d4d4d4; border-radius: 6px; padding: 10px 12px;
  height: 420px; overflow: auto; font-size: 12.5px; line-height: 1.55;
}
.ln { white-space: pre-wrap; word-break: break-all; }
.drop { margin-bottom: 8px; }
</style>
