<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import PageState from '../components/PageState.vue'
import EvidenceChain from '../components/EvidenceChain.vue'
import { claimTrace } from '../api/paper'
import { useLoader, useProject } from '../utils/useLoader'

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
    <div class="page-title">
      <router-link :to="`/projects/${pid}/paper`"><el-button icon="ArrowLeft" text>论文与论断</el-button></router-link>
      <h2>证据链 {{ cid }}</h2>
    </div>
    <el-card shadow="never">
      <PageState :loading="loading && !data" :error="error" @retry="load()">
        <EvidenceChain v-if="data" :pid="pid" :trace="data" />
      </PageState>
    </el-card>
  </div>
</template>
