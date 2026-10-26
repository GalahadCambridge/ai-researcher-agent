# 网页界面（GUI）

Vue 3 + Vite + Element Plus 做的网页，通过后台 HTTP 接口完成整个研究流程。设计见
[详细设计 5](../docs/详细设计/5_GUI_详细设计.md)，接口见启动后台后的 <http://127.0.0.1:8765/docs>。

网页**只负责显示和提交操作**：项目状态、审批、运行全部从后台读取，浏览器里只存“界面偏好”（语言、外观、侧边栏折叠、日志自动滚动、每页条数）。

## 用法

需要 Node.js 18 以上（conda 用户：`conda install -c conda-forge nodejs`）。第一次先安装依赖：

```bash
cd web
```

```bash
npm install
```

**开发**（改代码后页面自动刷新）：先在另一个终端运行 `air serve`，再：

```bash
npm run dev
```

浏览器打开 <http://localhost:5173/>。`/api` 会被代理到 `http://127.0.0.1:8765`；后台换了端口时先设置 `AIR_SERVER=http://127.0.0.1:8766`。

**交付**：

```bash
npm run build
```

生成 `web/dist/` 后，重启 `air serve`，直接打开 <http://127.0.0.1:8765/> 就是网页（不需要 `npm run dev`）。

**后台没开时看页面**（假数据模式，所有写操作会提示不能提交）：

```bash
npm run dev:mock
```

假数据在 `src/mock/data/`，是样例项目的真实返回值。数据格式变了时，登记样例项目、开着后台运行 `npm run mock:capture` 重新抓取。

## 页面

| 编号 | 页面 | 地址（`#` 后面） | 文件 |
|---|---|---|---|
| P1 | 项目列表 | `/projects` | `pages/ProjectList.vue` |
| P2 | 新建项目 | `/projects/new` | `pages/ProjectNew.vue` |
| P3 | 项目总览（状态、提示条、问题卡片、预算、进行中的运行、最近动态、检查点回滚） | `/projects/:pid` | `pages/ProjectOverview.vue` |
| P4 | 审批列表 | `/projects/:pid/approvals` | `pages/ApprovalList.vue` |
| P5 | 审批详情（内容、与上一版对比、相关材料、批准 / 退回 / 拒绝、在线编辑） | `/projects/:pid/approvals/:aid` | `pages/ApprovalDetail.vue` |
| P6 | 运行列表 | `/projects/:pid/runs` | `pages/RunList.vue` |
| P7 | 运行详情（实时日志、取消、人工标注） | `/projects/:pid/runs/:runId` | `pages/RunDetail.vue` |
| P8 | 文献 | `/projects/:pid/literature` | `pages/Literature.vue` |
| P9 | 图表 | `/projects/:pid/figures` | `pages/Figures.vue` |
| P10 | 论文与论断 | `/projects/:pid/paper` | `pages/Paper.vue` |
| P11 | 证据链 | `/projects/:pid/claims/:claimId` | `pages/ClaimTrace.vue` |
| P12 | 文件查看 | `/projects/:pid/files?path=` | `pages/FileView.vue` |
| P13 | 事件日志 | `/projects/:pid/events` | `pages/Events.vue` |
| P14 | 模板管理 | `/projects/:pid/templates` | `pages/Templates.vue` |

用 hash 路由（地址里带 `#`），这样由后台提供静态文件时刷新任何页面都不会 404。

## 代码结构

| 位置 | 内容 |
|---|---|
| `src/api/` | 每个接口一个函数，页面不直接写网址；`client.js` 统一错误提示，`stream.js` 封装 SSE |
| `src/i18n/` | 界面文字：`zh-CN.js`（全集）、`en.js`（键一一对应）；`index.js` 管当前语言（侧边栏底部切换，存在 localStorage） |
| `src/constants/labels.js` | 英文枚举 → 显示文字（在 `i18n/*.js` 的 `labels`）和颜色，新值显示原文 |
| `src/utils/events.js` | 事件的显示文字：中文用后台存的 `summary`，英文按 `type + data` 套模板（`en.js` 的 `events.tpl`） |
| `src/utils/useLoader.js` | 页面的“加载中 / 出错 / 数据”三态；`useProject()` 取项目上下文；`useProjectRefresh()` 收到 SSE 通知后静默刷新 |
| `src/style.css` | 主题：颜色、字号、圆角都是 CSS 变量（`--air-*`），并映射到 Element Plus 的 `--el-*`；`html.dark` 下是深色。改颜色只改这里 |
| `src/utils/theme.js` | 外观：跟随系统 / 浅色 / 深色（侧边栏底部切换） |
| `src/components/AppSidebar.vue` | 左侧边栏：全局导航 + 当前项目的菜单（审批带待审数量），可折叠 |
| `src/pages/ProjectLayout.vue` | 项目内页面的外框：顶部一行项目名，订阅一次项目 SSE；断线时显示提示并每 10 秒轮询 |
| `src/icons.js` | 只注册用到的图标；Element Plus 组件由 `vite.config.js` 按需引入。新用一个图标时先在这里加上 |
| `src/components/` | `StateTag`、`BudgetBar`、`MarkdownView`、`DiffView`、`LogViewer`、`PdfViewer`、`EvidenceChain`、`QuestionCard`、`PageState` 等 |

## 几条约定（改代码前读一下）

- **文字**：页面上不要直接写中文，写 `t('键')`，并在 `i18n/zh-CN.js` 和 `i18n/en.js` 里各加一条（消息里不要直接用 `{ } @ |` 这几个字符）。
  请求会自动带上 `Accept-Language`、SSE 地址带 `?lang=`，后台按这个语言返回状态说明、阻塞原因、错误提示；
  项目数据本身（标题、摘要、各阶段写的进度和问题）按原样显示，不翻译。
- **样式**：颜色一律用 `var(--air-*)` 变量，不要写死颜色值，否则深色模式会出问题；一个区域只放一个主按钮（`type="primary"`）；长文用 `<MarkdownView reading />`（阅读模式）。

- **SSE 只当通知**：收到后 0.5 秒内重新请求 REST 接口，多条通知合并成一次请求。
- **按钮是否出现只看后台的 `next_actions`**（例外：“取消项目”后台没有列出，按引擎规则在“已完成 / 出错停止”以外都显示）。
- **写操作都带 `request_id`**：审批、回答问题在打开页面时生成一个，网络重试沿用同一个；提交中按钮禁用；
  收到 409 时提示“已过期或已被处理”、刷新并换一个新的 `request_id`（详细设计 5 第 6 节）。
- **日志流收到 `end` 必须 `close()`**，否则浏览器会自动重连。
