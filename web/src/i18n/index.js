import { createI18n } from 'vue-i18n'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import zh from './zh-CN'
import en from './en'
import { getPref, setPref } from '../utils/prefs'

// 界面语言（中文 / English）。选择属于界面偏好，存在 localStorage；默认中文，不跟随浏览器
// （后台也按我们发的语言返回文字，见 api/client.js 的 Accept-Language 和 api/stream.js 的 ?lang=）
export const LANGS = [
  { value: 'zh', label: '中文' },
  { value: 'en', label: 'English' },
]
const saved = getPref('lang', 'zh')

export const i18n = createI18n({
  legacy: false,
  locale: LANGS.some((l) => l.value === saved) ? saved : 'zh',
  fallbackLocale: 'zh',
  messages: { zh, en },
  missingWarn: false,
  fallbackWarn: false,
})

export const lang = i18n.global.locale // ref：'zh' | 'en'
export const t = (...args) => i18n.global.t(...args)
export const te = (key) => i18n.global.te(key, 'zh') // 中文是全集：只要中文里有这个键就算有

function apply(v) {
  dayjs.locale(v === 'en' ? 'en' : 'zh-cn')
  document.documentElement.lang = v === 'en' ? 'en' : 'zh-CN'
}
apply(lang.value)

export function setLang(v) {
  lang.value = v
  setPref('lang', v)
  apply(v)
}
