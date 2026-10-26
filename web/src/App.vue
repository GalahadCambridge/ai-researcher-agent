<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'
import AppSidebar from './components/AppSidebar.vue'
import { http } from './api/client'
import { USE_MOCK } from './mock'

// 整体布局：左侧边栏 + 居中的内容区（限制最大宽度）。每 15 秒检查一次后台是否在线
const route = useRoute()
const { t, locale } = useI18n()
const epLocale = computed(() => (locale.value === 'en' ? en : zhCn))
const wide = computed(() => !!route.meta.wide)
// 内容区是自己滚动的，换页面时手动回到顶部
watch(() => route.path, () => document.getElementById('main')?.scrollTo(0, 0))
const online = ref(true)
let timer = null
async function ping() {
  if (USE_MOCK) return
  try {
    await http.get('/health', { quiet: true, timeout: 5000 })
    online.value = true
  } catch {
    online.value = false
  }
}
onMounted(() => { ping(); timer = setInterval(ping, 15000) })
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <el-config-provider :locale="epLocale">
    <div class="app">
      <AppSidebar />
      <main id="main" class="main">
        <div v-if="!online || USE_MOCK" class="banner-row">
          <el-alert v-if="!online" type="error" :closable="false" show-icon
            :title="t('app.offline')" />
          <el-alert v-else type="warning" :closable="false" show-icon :title="t('app.mockBanner')" />
        </div>
        <div :class="['container', { wide }]"><router-view /></div>
      </main>
    </div>
  </el-config-provider>
</template>

<style scoped>
.app { display: flex; height: 100%; }
.main { flex: 1; min-width: 0; overflow-y: auto; }
.banner-row { max-width: 1440px; margin: 16px auto 0; padding: 0 40px; }
.container { max-width: 1120px; margin: 0 auto; padding: 32px 40px 64px; }
.container.wide { max-width: 1440px; }
@media (max-width: 900px) { .container, .banner-row { padding-left: 20px; padding-right: 20px; } }
</style>
