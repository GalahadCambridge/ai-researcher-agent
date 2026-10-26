import { createRouter, createWebHashHistory } from 'vue-router'

// 路由表（详细设计 5 第 4.1 节）。用 hash 路由：后台用静态文件提供网页时，刷新任何页面都不会 404。
const routes = [
  { path: '/', redirect: '/projects' },
  { path: '/projects', component: () => import('./pages/ProjectList.vue'), },
  { path: '/projects/new', component: () => import('./pages/ProjectNew.vue'), },
  {
    path: '/projects/:pid',
    component: () => import('./pages/ProjectLayout.vue'),
    props: true,
    children: [
      { path: '', component: () => import('./pages/ProjectOverview.vue'), meta: { menu: 'overview' } },
      { path: 'approvals', component: () => import('./pages/ApprovalList.vue'), meta: { menu: 'approvals' } },
      { path: 'approvals/:aid', component: () => import('./pages/ApprovalDetail.vue'), meta: { menu: 'approvals', wide: true } },
      { path: 'runs', component: () => import('./pages/RunList.vue'), meta: { menu: 'runs', wide: true } },
      { path: 'runs/:runId', component: () => import('./pages/RunDetail.vue'), meta: { menu: 'runs', wide: true } },
      { path: 'literature', component: () => import('./pages/Literature.vue'), meta: { menu: 'literature', wide: true } },
      { path: 'figures', component: () => import('./pages/Figures.vue'), meta: { menu: 'figures' } },
      { path: 'paper', component: () => import('./pages/Paper.vue'), meta: { menu: 'paper', wide: true } },
      { path: 'claims/:claimId', component: () => import('./pages/ClaimTrace.vue'), meta: { menu: 'paper' } },
      { path: 'files', component: () => import('./pages/FileView.vue'), meta: { menu: 'files' } },
      { path: 'events', component: () => import('./pages/Events.vue'), meta: { menu: 'events' } },
      { path: 'templates', component: () => import('./pages/Templates.vue'), meta: { menu: 'templates' } },
    ],
  },
  { path: '/:rest(.*)*', redirect: '/projects' },
]

const router = createRouter({ history: createWebHashHistory(), routes })
export default router
