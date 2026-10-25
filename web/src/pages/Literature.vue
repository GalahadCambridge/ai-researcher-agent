<script setup>
import { onMounted, reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import { getPaperRecord, listLiterature } from '../api/paper'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'

// P8 文献。“仅基于摘要”用橙色醒目显示；点击行打开抽屉看摘要和阅读卡
const route = useRoute()
const router = useRouter()
const { pid } = useProject()
const { data, loading, error, load } = useLoader(() => listLiterature(pid.value))
onMounted(load)
useProjectRefresh(load, ['event'])

const drawer = reactive({ open: false, loading: false, paper: null, cards: [] })
async function open(paperId) {
  Object.assign(drawer, { open: true, loading: true, paper: (data.value || []).find((p) => p.paper_id === paperId) || null, cards: [] })
  try {
    const r = await getPaperRecord(pid.value, paperId)
    drawer.paper = r.paper
    drawer.cards = r.reading_cards
  } catch {
    /* 抽屉里显示列表中已有的信息 */
  } finally {
    drawer.loading = false
  }
}
// 从证据链跳过来时（?paper=...）直接打开
watch(() => [route.query.paper, data.value], ([p]) => { if (p && data.value && !drawer.open) open(p) }, { immediate: true })
watch(() => drawer.open, (o) => { if (!o && route.query.paper) router.replace({ query: {} }) })

const authors = (a) => (a.length > 3 ? a.slice(0, 3).join(', ') + ' 等' : a.join(', '))
const ROLE = { methods: '方法', feasibility: '可行性', single: '综合' }
const CONF = { high: '高', medium: '中', low: '低' }
</script>

<template>
  <div>
    <div class="page-title"><h2>文献</h2><span class="muted">共 {{ data?.length || 0 }} 篇</span></div>
    <el-card shadow="never">
      <PageState :loading="loading && !data" :error="error" :empty="!data?.length" empty-text="还没有文献（选题阶段会检索）" @retry="load()">
        <el-table :data="data || []" row-class-name="clickable-row" @row-click="(r) => open(r.paper_id)">
          <el-table-column label="标题" min-width="300">
            <template #default="{ row }">
              <div>{{ row.title }}</div>
              <div class="muted small mono">{{ row.paper_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="作者" min-width="180"><template #default="{ row }">{{ authors(row.authors) }}</template></el-table-column>
          <el-table-column label="年份" prop="year" width="70" />
          <el-table-column label="会议/期刊" prop="venue" width="110" />
          <el-table-column label="访问层级" width="120">
            <template #default="{ row }"><StateTag kind="access" :value="row.access_level" :size="'default'" /></template>
          </el-table-column>
          <el-table-column label="相关性" width="80"><template #default="{ row }">{{ row.relevance?.score ?? '—' }}</template></el-table-column>
          <el-table-column label="精读" width="70">
            <template #default="{ row }"><el-icon v-if="row.selected_for_reading" color="var(--el-color-success)"><Select /></el-icon></template>
          </el-table-column>
        </el-table>
      </PageState>
    </el-card>

    <el-drawer v-model="drawer.open" :title="drawer.paper?.title || '文献'" size="50%">
      <div v-if="drawer.paper" v-loading="drawer.loading">
        <div class="meta">
          <StateTag kind="access" :value="drawer.paper.access_level" />
          <span>{{ drawer.paper.authors.join(', ') }}</span>
          <span class="muted">{{ drawer.paper.venue }} {{ drawer.paper.year }}</span>
          <a :href="drawer.paper.url" target="_blank" rel="noopener" class="link">打开原文链接 ↗</a>
        </div>
        <el-alert v-if="drawer.paper.access_level !== 'full_text'" type="warning" :closable="false" show-icon class="mb"
          title="没有取得全文：下面的阅读卡只基于摘要/元数据，结论需要谨慎看待" />
        <h4>摘要</h4>
        <p class="abs">{{ drawer.paper.abstract || '（无摘要）' }}</p>
        <div v-if="drawer.paper.relevance" class="muted">相关性 {{ drawer.paper.relevance.score }}：{{ drawer.paper.relevance.reason }}</div>
        <h4>阅读卡（{{ drawer.cards.length }}）</h4>
        <div v-if="!drawer.cards.length" class="muted">没有阅读卡（没有被选中精读）</div>
        <el-card v-for="c in drawer.cards" :key="c.card_id" shadow="never" class="cardx">
          <template #header>
            <span class="mono">{{ c.card_id }}</span>
            <el-tag size="small" class="ml">{{ ROLE[c.role] || c.role }}</el-tag>
            <el-tag size="small" :type="c.based_on === 'full_text' ? 'success' : 'warning'" class="ml">{{ c.based_on === 'full_text' ? '基于全文' : '仅基于摘要' }}</el-tag>
          </template>
          <div class="muted mb">与研究问题的关系：{{ c.relevance_to_question }}</div>
          <div v-for="(it, i) in c.items" :key="i" class="item">
            <div class="row">
              <b>{{ it.field }}</b>
              <el-tag size="small" :type="it.kind === 'author_claim' ? 'primary' : 'info'">{{ it.kind === 'author_claim' ? '作者结论' : 'Agent 推断' }}</el-tag>
              <span class="muted small">置信度 {{ CONF[it.confidence] || it.confidence }} · 出处 {{ it.source_loc }}</span>
            </div>
            <div>{{ it.content }}</div>
            <blockquote v-if="it.quote" class="quote">
              “{{ it.quote }}”
              <span v-if="it.quote_verified === true" class="ok">✓ 已核对原文</span>
              <span v-else-if="it.quote_verified === false" class="bad">⚠ 原文中未找到</span>
            </blockquote>
          </div>
        </el-card>
        <el-collapse class="mt"><el-collapse-item title="BibTeX" name="b"><pre class="code mono">{{ drawer.paper.bibtex }}</pre></el-collapse-item></el-collapse>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.small { font-size: 12px; }
.meta { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.link { color: var(--el-color-primary); }
.abs { line-height: 1.7; }
.cardx { margin-bottom: 12px; }
.item { padding: 8px 0; border-top: 1px dashed var(--el-border-color-lighter); }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 4px; }
.quote { margin: 6px 0 0; padding: 4px 10px; border-left: 3px solid var(--el-border-color); color: var(--el-text-color-regular); font-size: 13px; }
.ok { color: var(--el-color-success); margin-left: 6px; }
.bad { color: var(--el-color-warning); margin-left: 6px; }
.ml { margin-left: 6px; }
.mb { margin-bottom: 8px; }
.mt { margin-top: 12px; }
</style>
