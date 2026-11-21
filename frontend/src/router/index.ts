import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import DrillExplorer from '@/views/DrillExplorer.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须排在动态模块路由之前，否则会被 /:moduleKey 抢先匹配。
    { path: '/drill_analysis', name: 'drill_analysis', component: DrillExplorer },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
