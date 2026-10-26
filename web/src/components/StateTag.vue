<script setup>
import { computed } from 'vue'
import * as L from '../constants/labels'

// kind: state | run | approval | kind | support | runLabel | access | severity
const props = defineProps({ kind: { type: String, default: 'state' }, value: { type: String, default: '' }, size: { type: String, default: 'default' } })
const TABLES = { state: L.STATE, run: L.RUN, approval: L.APPROVAL, kind: L.KIND, support: L.SUPPORT, runLabel: L.RUN_LABEL, access: L.ACCESS, severity: L.SEVERITY }
const p = computed(() => L.pair(TABLES[props.kind] || {}, props.value))
const effect = computed(() => (props.kind === 'support' && props.value === 'contradicted' ? 'dark' : 'light'))
</script>

<template>
  <el-tag v-if="value" :type="p[1]" :effect="effect" :size="size" disable-transitions>{{ p[0] }}</el-tag>
</template>
