import { ref, watch } from 'vue'
import { getPref, setPref } from './prefs'

// 主题：跟随系统 / 浅色 / 深色。属于界面偏好，存在 localStorage；实际是否深色由 <html class="dark"> 决定
export const themePref = ref(getPref('theme', 'system'))
const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null

function apply() {
  const dark = themePref.value === 'dark' || (themePref.value === 'system' && !!media?.matches)
  document.documentElement.classList.toggle('dark', dark)
}

export function initTheme() {
  apply()
  media?.addEventListener?.('change', apply)
  watch(themePref, (v) => {
    setPref('theme', v)
    apply()
  })
}
