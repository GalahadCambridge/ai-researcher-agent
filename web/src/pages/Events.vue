<script setup>
import { onMounted, reactive, ref } from 'vue'
import PageState from '../components/PageState.vue'
import { listEvents } from '../api/projects'
import { fullTime } from '../utils/format'
import { useProject, useProjectRefresh } from '../utils/useLoader'

// P13 事件日志：按类型筛选、按关键词搜索，滚动到底部加载更多（after_seq 分页，从旧到新）
const { pid } = useProject()
const PAGE = 100
const TYPES = [
  ['state.*', '状态变化'], ['approval.*', '审批'], ['question.*', '提问'], ['run.*', '运行'],
  ['artifact.*', '产物版本'], ['stage.*', '阶段决策'], ['project.*', '项目'], ['budget.*', '预算'],
  ['literature.*', '文献'], ['template.*', '模板'], ['permission.*', '权限'], ['engine.*', '引擎'],
]
const filter = reactive({ types: [], q: '', run_id: '' })
const rows = ref([])
const loading = ref(false)
const more = ref(true)
const error = ref(null)

function params(after) {
  return { after_seq: after, limit: PAGE, type: filter.types.join(',') || undefined, q: filter.q.trim() || undefined, run_id: filter.run_id.trim() || undefined }
}
async function reset() {
  rows.value = []
  more.value = true
  error.value = null
  await loadMore()
}
async function loadMore() {
  if (loading.value || !more.value) return
  loading.value = true
  try {
    const after = rows.value.length ? rows.value[rows.value.length - 1].seq : 0
    const got = await listEvents(pid.value, params(after))
    rows.value.push(...got)
    more.value = got.length === PAGE
  } catch (e) {
    error.value = e
  } finally {
    loading.value = false
  }
}
onMounted(reset)
// 有新事件时，如果已经加载到最后，把新的接上
useProjectRefresh(() => { if (!more.value) { more.value = true; loadMore() } }, ['event'])

const color = (t) => (t.includes('fail') || t.includes('error') ? 'danger' : t.startsWith('approval') ? 'warning' : t.startsWith('run') ? 'primary' : t.startsWith('state') ? 'success' : '')
const expanded = ref({})
</script>

<template>
  <div>
    <div class="page-title"><h2>事件日志</h2><span class="muted">项目里发生过的每件事（只增不删）</span></div>
    <div class="toolbar">
      <el-select v-model="filter.types" multiple collapse-tags collapse-tags-tooltip placeholder="全部类型" clearable style="width: 240px" @change="reset">
        <el-option v-for="[v, l] in TYPES" :key="v" :value="v" :label="l" />
      </el-select>
      <el-input v-model="filter.q" placeholder="搜索摘要，如 idea、E2" clearable style="width: 220px" @keyup.enter="reset" @clear="reset" />
      <el-input v-model="filter.run_id" placeholder="运行编号" clearable style="width: 220px" class="mono" @keyup.enter="reset" @clear="reset" />
      <el-button type="primary" icon="Search" @click="reset">搜索</el-button>
    </div>
    <el-card shadow="never">
      <PageState :loading="loading && !rows.length" :error="error && !rows.length ? error : null" :empty="!loading && !rows.length" empty-text="没有符合条件的事件" @retry="reset">
        <div v-infinite-scroll="loadMore" :infinite-scroll-disabled="loading || !more" :infinite-scroll-distance="80" class="list">
          <el-timeline>
            <el-timeline-item v-for="e in rows" :key="e.seq" :type="color(e.type)" :timestamp="fullTime(e.ts)" placement="top">
              <div class="row">
                <span class="mono muted seq">#{{ e.seq }}</span>
                <span>{{ e.summary }}</span>
              </div>
              <div class="meta muted">
                <span class="mono">{{ e.type }}</span> · {{ e.actor }}
                <router-link v-if="e.run_id" :to="`/projects/${pid}/runs/${e.run_id}`" class="mono link">{{ e.run_id }}</router-link>
                <router-link v-for="r in e.refs" :key="r.artifact_id + r.version" :to="`/projects/${pid}/files?path=${encodeURIComponent(r.artifact_id)}`" class="mono link">{{ r.artifact_id }} v{{ r.version }}</router-link>
                <el-button v-if="Object.keys(e.data || {}).length" link size="small" @click="expanded[e.seq] = !expanded[e.seq]">{{ expanded[e.seq] ? '收起' : '详情' }}</el-button>
              </div>
              <pre v-if="expanded[e.seq]" class="code mono">{{ JSON.stringify(e.data, null, 2) }}</pre>
            </el-timeline-item>
          </el-timeline>
          <div class="end muted">
            <span v-if="loading">加载中……</span>
            <el-button v-else-if="more" size="small" @click="loadMore">加载更多</el-button>
            <span v-else>已经到底了（共 {{ rows.length }} 条）</span>
          </div>
        </div>
      </PageState>
    </el-card>
  </div>
</template>

<style scoped>
.row { display: flex; gap: 8px; }
.seq { flex: none; }
.meta { font-size: 12px; margin-top: 2px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.link { color: var(--el-color-primary); }
.end { text-align: center; padding: 8px; }
</style>
