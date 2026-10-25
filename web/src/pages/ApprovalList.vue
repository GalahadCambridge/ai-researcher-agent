<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageState from '../components/PageState.vue'
import StateTag from '../components/StateTag.vue'
import TimeText from '../components/TimeText.vue'
import { listApprovals } from '../api/approvals'
import { useLoader, useProject, useProjectRefresh } from '../utils/useLoader'

// P4 审批列表：待处理 / 历史
const router = useRouter()
const { pid } = useProject()
const tab = ref('pending')
const { data, loading, error, load } = useLoader(() => listApprovals(pid.value))
onMounted(load)
useProjectRefresh(load, ['approval'])

const rows = computed(() => {
  const all = [...(data.value || [])].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
  return tab.value === 'pending' ? all.filter((a) => a.status === 'pending') : all.filter((a) => a.status !== 'pending')
})
const pendingN = computed(() => (data.value || []).filter((a) => a.status === 'pending').length)
const open = (row) => router.push(`/projects/${pid.value}/approvals/${row.approval_id}`)
</script>

<template>
  <div>
    <div class="page-title"><h2>审批</h2></div>
    <el-card shadow="never">
      <el-tabs v-model="tab">
        <el-tab-pane :label="`待处理（${pendingN}）`" name="pending" />
        <el-tab-pane label="历史" name="history" />
      </el-tabs>
      <PageState :loading="loading && !data" :error="error" :empty="!rows.length"
        :empty-text="tab === 'pending' ? '没有需要你审批的内容' : '还没有审批记录'" @retry="load()">
        <el-table :data="rows" row-class-name="clickable-row" @row-click="open">
          <el-table-column label="编号" width="100"><template #default="{ row }"><span class="mono">{{ row.approval_id }}</span></template></el-table-column>
          <el-table-column label="类别" width="110"><template #default="{ row }"><StateTag kind="kind" :value="row.kind" /></template></el-table-column>
          <el-table-column label="对象与版本" min-width="200">
            <template #default="{ row }"><span class="mono">{{ row.target.artifact_id }} v{{ row.target.version }}</span></template>
          </el-table-column>
          <el-table-column label="摘要" prop="summary" min-width="260" show-overflow-tooltip />
          <el-table-column label="提交时间" width="120"><template #default="{ row }"><TimeText :value="row.created_at" /></template></el-table-column>
          <el-table-column label="状态" width="140"><template #default="{ row }"><StateTag kind="approval" :value="row.status" /></template></el-table-column>
        </el-table>
      </PageState>
    </el-card>
  </div>
</template>
