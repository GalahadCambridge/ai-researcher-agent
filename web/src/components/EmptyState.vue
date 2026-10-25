<script setup>
defineProps({ text: { type: String, default: '暂无数据' } })
</script>

<template>
  <el-empty :description="text" :image-size="80"><slot /></el-empty>
</template>
