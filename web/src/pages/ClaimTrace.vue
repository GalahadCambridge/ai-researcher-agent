<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import PageState from '../components/PageState.vue'
import EvidenceChain from '../components/EvidenceChain.vue'
import { claimTrace } from '../api/paper'
import { useLoader, useProject } from '../utils/useLoader'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

// P11 证据链（单独页面；论文页里是抽屉）
const route = useRoute()
const { pid } = useProject()
const cid = computed(() => route.params.claimId)
const { data, loading, error, load } = useLoader(() => claimTrace(pid.value, cid.value))
onMounted(load)
watch(cid, () => load())
</script>

<template>
  <div>
    <router-link :to="`/projects/${pid}/paper`" class="back"><el-icon><ArrowLeft /></el-icon>{{ t('evidence.backToPaper') }}</router-link>
    <div class="page-title">
      <h2>{{ t('paper.traceTitle', { id: cid }) }}</h2>
    </div>
    <el-card shadow="never">
      <PageState :loading="loading && !data" :error="error" @retry="load()">
        <EvidenceChain v-if="data" :pid="pid" :trace="data" />
      </PageState>
    </el-card>
  </div>
</template>
