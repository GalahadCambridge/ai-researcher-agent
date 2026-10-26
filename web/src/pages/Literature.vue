<script setup>
import { onMounted, reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import { getPaperRecord, listLiterature } from '../api/paper'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t, te } = useI18n()

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

// 只把 http(s) 链接做成可点的，防止 javascript: 之类的地址
const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? u : '')
const authors = (a) => (a.length > 3 ? t('literature.etAl', { names: a.slice(0, 3).join(', ') }) : a.join(', '))
const sub = (group, v) => (te(`literature.${group}.${v}`) ? t(`literature.${group}.${v}`) : v)
</script>

<template>
  <div>
    <div class="page-title"><h2>{{ t('literature.title') }}</h2><span class="muted">{{ t('literature.count', { n: data?.length || 0 }) }}</span></div>
    <el-card shadow="never">
      <PageState :loading="loading && !data" :error="error" :empty="!data?.length" :empty-text="t('literature.empty')" @retry="load()">
        <el-table :data="data || []" row-class-name="clickable-row" @row-click="(r) => open(r.paper_id)">
          <el-table-column :label="t('literature.colTitle')" min-width="300">
            <template #default="{ row }">
              <div>{{ row.title }}</div>
              <div class="muted small mono">{{ row.paper_id }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('literature.colAuthors')" min-width="180"><template #default="{ row }">{{ authors(row.authors) }}</template></el-table-column>
          <el-table-column :label="t('literature.colYear')" prop="year" width="70" />
          <el-table-column :label="t('literature.colVenue')" prop="venue" width="110" />
          <el-table-column :label="t('literature.colAccess')" width="120">
            <template #default="{ row }"><StateTag kind="access" :value="row.access_level" :size="'default'" /></template>
          </el-table-column>
          <el-table-column :label="t('literature.colRelevance')" width="80"><template #default="{ row }">{{ row.relevance?.score ?? '—' }}</template></el-table-column>
          <el-table-column :label="t('literature.colRead')" width="70">
            <template #default="{ row }"><el-icon v-if="row.selected_for_reading" color="var(--el-color-success)"><Select /></el-icon></template>
          </el-table-column>
        </el-table>
      </PageState>
    </el-card>

    <el-drawer v-model="drawer.open" :title="drawer.paper?.title || t('literature.drawerTitle')" size="50%">
      <div v-if="drawer.paper" v-loading="drawer.loading">
        <div class="meta">
          <StateTag kind="access" :value="drawer.paper.access_level" />
          <span>{{ drawer.paper.authors.join(', ') }}</span>
          <span class="muted">{{ drawer.paper.venue }} {{ drawer.paper.year }}</span>
          <a v-if="safeUrl(drawer.paper.url)" :href="safeUrl(drawer.paper.url)" target="_blank" rel="noopener noreferrer" class="link">{{ t('literature.openLink') }}</a>
          <span v-else-if="drawer.paper.url" class="muted mono">{{ drawer.paper.url }}</span>
        </div>
        <el-alert v-if="drawer.paper.access_level !== 'full_text'" type="warning" :closable="false" show-icon class="mb"
          :title="t('literature.noFullText')" />
        <h4>{{ t('literature.abstract') }}</h4>
        <p class="abs">{{ drawer.paper.abstract || t('literature.noAbstract') }}</p>
        <div v-if="drawer.paper.relevance" class="muted">{{ t('literature.relevance', { score: drawer.paper.relevance.score, reason: drawer.paper.relevance.reason }) }}</div>
        <h4>{{ t('literature.cards', { n: drawer.cards.length }) }}</h4>
        <div v-if="!drawer.cards.length" class="muted">{{ t('literature.noCards') }}</div>
        <el-card v-for="c in drawer.cards" :key="c.card_id" shadow="never" class="cardx">
          <template #header>
            <span class="mono">{{ c.card_id }}</span>
            <el-tag size="small" class="ml">{{ sub('role', c.role) }}</el-tag>
            <el-tag size="small" :type="c.based_on === 'full_text' ? 'success' : 'warning'" class="ml">{{ c.based_on === 'full_text' ? t('literature.basedFull') : t('literature.basedAbstract') }}</el-tag>
          </template>
          <div class="muted mb">{{ t('literature.relToQuestion', { text: c.relevance_to_question }) }}</div>
          <div v-for="(it, i) in c.items" :key="i" class="item">
            <div class="row">
              <b>{{ it.field }}</b>
              <el-tag size="small" :type="it.kind === 'author_claim' ? 'primary' : 'info'">{{ it.kind === 'author_claim' ? t('literature.authorClaim') : t('literature.agentInference') }}</el-tag>
              <span class="muted small">{{ t('literature.confidence', { c: sub('conf', it.confidence), loc: it.source_loc }) }}</span>
            </div>
            <div>{{ it.content }}</div>
            <blockquote v-if="it.quote" class="quote">
              “{{ it.quote }}”
              <span v-if="it.quote_verified === true" class="ok">{{ t('literature.quoteOk') }}</span>
              <span v-else-if="it.quote_verified === false" class="bad">{{ t('literature.quoteBad') }}</span>
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
