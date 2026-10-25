<script setup>
import { computed } from 'vue'
import StateTag from './StateTag.vue'
import FileImage from './FileImage.vue'
import CopyText from './CopyText.vue'
import { SECTION, label } from '../constants/labels'
import { renderClaimText } from '../utils/format'

// 一条论断的追溯链：论断 → 图表 → 汇总（重算核对）→ 运行 → 代码提交 / 配置 / 环境；引用型论断 → 文献
const props = defineProps({ pid: String, trace: { type: Object, required: true } })
const c = computed(() => props.trace.claim)
const check = computed(() => props.trace.check)
const fileLink = (path) => `/projects/${props.pid}/files?path=${encodeURIComponent(path)}`
const CITATION = { verified: ['已核实', 'success'], metadata_only: ['仅元数据', 'warning'], not_in_archive: ['不在文献档案中', 'danger'], source_unreachable: ['来源无法访问', 'info'] }
</script>

<template>
  <div class="chain">
    <div class="claim">
      <div class="row">
        <b class="mono">{{ c.claim_id }}</b>
        <el-tag size="small" effect="plain">{{ label(SECTION, c.section) }}</el-tag>
        <el-tag size="small" effect="plain" type="info">{{ c.type }} · {{ c.importance }}</el-tag>
        <StateTag v-if="check" kind="support" :value="check.support" />
      </div>
      <p class="text">{{ renderClaimText(c) }}</p>
      <div v-if="c.hypothesis" class="muted">对应假设：{{ c.hypothesis }}</div>
    </div>

    <el-timeline>
      <el-timeline-item v-if="trace.figures.length" type="primary" hollow>
        <div class="step-title">图表</div>
        <div class="figs">
          <router-link v-for="f in trace.figures" :key="f.fig_id" :to="`/projects/${pid}/figures?fig=${f.fig_id}`" class="fig">
            <FileImage :pid="pid" :path="f.outputs?.[0]" height="110px" />
            <div class="mono">{{ f.fig_id }}</div>
          </router-link>
        </div>
      </el-timeline-item>

      <el-timeline-item v-if="trace.aggregates.length" type="primary" hollow>
        <div class="step-title">汇总结果（论文里的数字 vs 从原始指标重算的数字）</div>
        <div v-for="a in trace.aggregates" :key="a.key" class="agg">
          <router-link :to="fileLink(`artifacts/aggregates/${a.aggregate_id}.md`)" class="mono">{{ a.aggregate_id }} v{{ a.version }}</router-link>
          <span class="mono muted">{{ a.key }}</span>
          <span>论文 <b class="mono">{{ a.rendered }}</b></span>
          <span>重算 <b class="mono">{{ a.recomputed }}</b></span>
          <el-tag v-if="a.rendered === a.recomputed" type="success" size="small">✓ 一致</el-tag>
          <el-tag v-else type="danger" size="small">✗ 不一致</el-tag>
        </div>
      </el-timeline-item>

      <el-timeline-item v-if="trace.runs.length" type="primary" hollow>
        <div class="step-title">实验运行（{{ trace.runs.length }} 个）</div>
        <div v-for="r in trace.runs" :key="r.run_id" class="run">
          <router-link :to="`/projects/${pid}/runs/${r.run_id}`" class="mono">{{ r.run_id }}</router-link>
          <StateTag kind="run" :value="r.state" size="small" />
          <span class="muted">代码提交</span><CopyText :text="r.code_commit">{{ r.code_commit.slice(0, 7) }}</CopyText>
          <router-link :to="fileLink(r.config_path)">配置</router-link>
          <router-link :to="fileLink(r.environment)">环境</router-link>
        </div>
      </el-timeline-item>

      <el-timeline-item v-if="trace.papers.length" type="primary" hollow>
        <div class="step-title">引用文献</div>
        <div v-for="p in trace.papers" :key="p.paper_id" class="run">
          <router-link :to="`/projects/${pid}/literature?paper=${encodeURIComponent(p.paper_id)}`">{{ p.title || p.paper_id }}</router-link>
          <el-tag :type="(CITATION[p.status] || [, 'info'])[1]" size="small">{{ (CITATION[p.status] || [p.status])[0] }}</el-tag>
          <span v-if="p.note" class="muted">{{ p.note }}</span>
        </div>
      </el-timeline-item>

      <el-timeline-item :type="check?.issues?.length ? 'danger' : 'success'">
        <div class="step-title">核验问题</div>
        <div v-if="!check" class="muted">还没有核验报告</div>
        <div v-else-if="!check.issues.length" class="muted">无</div>
        <div v-for="(i, k) in check?.issues || []" :key="k" class="issue">
          <StateTag kind="severity" :value="i.severity" size="small" />
          <span class="mono">{{ i.code }}</span>
          <span>{{ i.detail }}</span>
          <div v-if="i.suggestion" class="muted">建议：{{ i.suggestion }}</div>
        </div>
      </el-timeline-item>
    </el-timeline>
    <div v-if="!trace.figures.length && !trace.aggregates.length && !trace.runs.length && !trace.papers.length" class="muted">
      这条论断没有登记任何证据。
    </div>
  </div>
</template>

<style scoped>
.claim { background: var(--el-fill-color-light); border-radius: 8px; padding: 12px 14px; margin-bottom: 16px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.text { margin: 8px 0 4px; line-height: 1.6; }
.step-title { font-weight: 600; margin-bottom: 6px; }
.figs { display: flex; gap: 12px; flex-wrap: wrap; }
.fig { width: 160px; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; padding: 6px; text-align: center; color: inherit; text-decoration: none; }
.agg, .run, .issue { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 4px 0; font-size: 13px; }
.chain a { color: var(--el-color-primary); }
</style>
