<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import StateTag from './StateTag.vue'
import { currentProject } from '../utils/currentProject'
import { themePref } from '../utils/theme'
import { getPref, setPref } from '../utils/prefs'
import { useI18n } from 'vue-i18n'
import { LANGS, setLang } from '../i18n'

// 左侧边栏：上面是全局导航，进入项目后显示这个项目的菜单（审批带待审数量）；可以折叠成只有图标
const route = useRoute()
const { t, locale } = useI18n()
const collapsed = ref(getPref('sidebar.collapsed', false))
watch(collapsed, (v) => setPref('sidebar.collapsed', v))

const pid = computed(() => route.params.pid || null)
const st = computed(() => (pid.value && currentProject.pid === pid.value ? currentProject.status : null))
const base = computed(() => `/projects/${pid.value}`)
const items = computed(() => [
  { key: 'overview', to: '', icon: 'Odometer', label: t('nav.overview') },
  { key: 'approvals', to: '/approvals', icon: 'Stamp', label: t('nav.approvals'), badge: st.value?.pending_approvals?.length || 0 },
  { key: 'runs', to: '/runs', icon: 'Cpu', label: t('nav.runs'), badge: st.value?.active_runs?.length || 0, quiet: true },
  { key: 'literature', to: '/literature', icon: 'Collection', label: t('nav.literature') },
  { key: 'figures', to: '/figures', icon: 'PieChart', label: t('nav.figures') },
  { key: 'paper', to: '/paper', icon: 'Document', label: t('nav.paper') },
  { key: 'events', to: '/events', icon: 'List', label: t('nav.events') },
  { key: 'templates', to: '/templates', icon: 'Files', label: t('nav.templates') },
])
const active = computed(() => route.meta.menu || (route.path === '/projects/new' ? 'new' : route.path.startsWith('/projects') && !pid.value ? 'list' : ''))

const THEMES = computed(() => [
  { value: 'system', icon: 'Monitor', label: t('nav.themeSystem') },
  { value: 'light', icon: 'Sunny', label: t('nav.themeLight') },
  { value: 'dark', icon: 'Moon', label: t('nav.themeDark') },
])
const themeIcon = computed(() => THEMES.value.find((x) => x.value === themePref.value)?.icon || 'Monitor')
const langLabel = computed(() => LANGS.find((l) => l.value === locale.value)?.label)
</script>

<template>
  <aside :class="['sidebar', { collapsed }]">
    <div class="brand-row">
      <router-link v-if="!collapsed" to="/projects" class="brand">
        <el-icon size="20"><Reading /></el-icon><span>AI Researcher</span>
      </router-link>
      <button class="icon-btn" :title="collapsed ? t('nav.expand') : t('nav.fold')" @click="collapsed = !collapsed">
        <el-icon size="18"><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
      </button>
    </div>

    <nav class="nav">
      <router-link to="/projects/new" :class="['item', 'new', { active: active === 'new' }]" :title="t('nav.newProject')">
        <el-icon><Plus /></el-icon><span v-if="!collapsed">{{ t('nav.newProject') }}</span>
      </router-link>
      <router-link to="/projects" :class="['item', { active: active === 'list' }]" :title="t('nav.projects')">
        <el-icon><FolderOpened /></el-icon><span v-if="!collapsed">{{ t('nav.projects') }}</span>
      </router-link>
    </nav>

    <template v-if="pid">
      <div v-if="!collapsed" class="project">
        <div class="label">{{ t('nav.currentProject') }}</div>
        <router-link :to="base" class="ptitle" :title="st?.title">{{ st?.title || pid }}</router-link>
        <StateTag v-if="st" :value="st.state" size="small" />
      </div>
      <div v-else class="divider" />
      <nav class="nav">
        <router-link v-for="it in items" :key="it.key" :to="base + it.to" :class="['item', { active: active === it.key }]" :title="it.label">
          <el-icon><component :is="it.icon" /></el-icon>
          <span v-if="!collapsed">{{ it.label }}</span>
          <span v-if="it.badge" :class="['badge', { quiet: it.quiet, dot: collapsed }]">{{ collapsed ? '' : it.badge }}</span>
        </router-link>
      </nav>
    </template>

    <div class="spacer" />
    <div class="foot">
      <el-dropdown trigger="click" placement="top-start" @command="(v) => (themePref = v)">
        <button class="item foot-btn" :title="t('nav.appearance')">
          <el-icon><component :is="themeIcon" /></el-icon><span v-if="!collapsed">{{ t('nav.appearance') }}</span>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="t in THEMES" :key="t.value" :command="t.value" :class="{ chosen: themePref === t.value }">
              <el-icon><component :is="t.icon" /></el-icon>{{ t.label }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-dropdown trigger="click" placement="top-start" @command="setLang">
        <button class="item foot-btn" :title="t('nav.language')">
          <el-icon><ChatDotRound /></el-icon><span v-if="!collapsed">{{ langLabel }}</span>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="l in LANGS" :key="l.value" :command="l.value" :class="{ chosen: locale === l.value }">{{ l.label }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <a href="/docs" target="_blank" rel="noopener" class="item" :title="t('nav.apiDocs')">
        <el-icon><Link /></el-icon><span v-if="!collapsed">{{ t('nav.apiDocs') }}</span>
      </a>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 232px; flex: none; display: flex; flex-direction: column; gap: 4px;
  background: var(--air-sidebar); border-right: 1px solid var(--air-border); padding: 12px 10px; overflow-y: auto;
  transition: width 0.15s ease;
}
.sidebar.collapsed { width: 60px; align-items: center; }
.brand-row { display: flex; align-items: center; justify-content: space-between; padding: 2px 4px 12px; width: 100%; }
.collapsed .brand-row { justify-content: center; }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; color: var(--air-text); }
.brand:hover { text-decoration: none; }
.brand .el-icon { color: var(--air-accent); }
.icon-btn { border: none; background: none; color: var(--air-text-2); cursor: pointer; padding: 6px; border-radius: 6px; display: flex; }
.icon-btn:hover { background: var(--air-fill-2); color: var(--air-text); }
.nav { display: flex; flex-direction: column; gap: 2px; width: 100%; }
.item {
  display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: var(--air-radius-sm);
  color: var(--air-text-2); font-size: var(--air-fs-body); cursor: pointer; border: none; background: none; width: 100%;
  font-family: inherit; text-align: left; box-sizing: border-box;
}
.collapsed .item { justify-content: center; padding: 8px; position: relative; }
.item:hover { background: var(--air-fill-2); color: var(--air-text); text-decoration: none; }
.item.active { background: var(--air-surface); color: var(--air-text); font-weight: 500; box-shadow: var(--air-shadow); }
.item .el-icon { font-size: 16px; flex: none; }
.item.new { color: var(--air-accent); font-weight: 500; }
.badge {
  margin-left: auto; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px; box-sizing: border-box;
  background: var(--air-accent); color: #fff; font-size: 11px; line-height: 18px; text-align: center; font-weight: 600;
}
.badge.quiet { background: var(--air-fill-2); color: var(--air-text-2); }
.badge.dot { position: absolute; top: 4px; right: 4px; min-width: 8px; width: 8px; height: 8px; padding: 0; }
.project { padding: 16px 10px 8px; display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.label { font-size: 11.5px; color: var(--air-text-3); letter-spacing: 0.02em; }
.ptitle { color: var(--air-text); font-weight: 500; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.divider { height: 1px; background: var(--air-border); width: 32px; margin: 10px 0; }
.foot { display: flex; flex-direction: column; gap: 2px; width: 100%; border-top: 1px solid var(--air-border); padding-top: 8px; }
.foot :deep(.el-dropdown) { width: 100%; }
.foot-btn { outline: none; }
:global(.el-dropdown-menu__item.chosen) { color: var(--air-accent); font-weight: 500; }
</style>
