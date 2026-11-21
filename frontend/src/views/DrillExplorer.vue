<template>
  <section class="page drill-page">
    <header class="page-head">
      <div>
        <h2>下钻分析</h2>
        <p class="page-desc">
          按授权范围重新聚合立方事实明细。打开或继续展开既有下钻路径时，响应只含能从仍被授权的输入证明的数据。
        </p>
      </div>
      <div class="page-actions">
        <label class="scope-picker">
          <span>取数账号</span>
          <select v-model="account" data-testid="drill-account" @change="switchAccount">
            <option v-for="item in accounts" :key="item.account" :value="item.account">
              {{ item.account_name }}
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="drill-export" type="button" :disabled="busy" @click="exportCurrent">
          下载当前下钻
        </button>
      </div>
    </header>

    <div class="scope-banner" data-testid="scope-banner">
      <div>
        <strong>{{ scope?.scope_label ?? '正在确定授权范围…' }}</strong>
        <span v-if="scope" class="scope-version">范围标识 scope:{{ scope.scope_version }}</span>
      </div>
      <div v-if="scope" class="scope-flags">
        <span v-if="scope.readonly">只读读者</span>
        <span v-if="!scope.complete">存在受限子地区（{{ scope.hidden_city_count }} 个城市不可见）</span>
        <span v-else>全部地区可见</span>
      </div>
    </div>

    <nav class="breadcrumb" data-testid="drill-breadcrumb">
      <button
        v-for="(node, index) in path"
        :key="node"
        class="crumb"
        type="button"
        :class="{ current: index === path.length - 1 }"
        @click="goTo(index)"
      >
        {{ node }}
      </button>
      <span v-if="childLevel" class="crumb-hint">可下钻到 {{ childLevel }}</span>
    </nav>

    <p v-if="errorMessage" class="error-text" data-testid="drill-error">{{ errorMessage }}</p>

    <div v-if="current" class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前节点</span>
        <strong class="stat-value">{{ current.parent.name }}</strong>
      </article>
      <article class="stat-card" data-testid="parent-subtotal">
        <span class="stat-label">
          {{ current.parent.subtotal_allowed ? '全量小计' : '小计（受限）' }}
        </span>
        <strong class="stat-value">{{ parentValue }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可见子节点</span>
        <strong class="stat-value">{{ (current.groups ?? []).length }}</strong>
      </article>
    </div>

    <p v-if="current && current.parent.reason" class="scope-note" data-testid="parent-note">
      {{ current.parent.reason }}
    </p>

    <table class="data-table">
      <thead>
        <tr>
          <th>子节点</th>
          <th>度量（revenue / orders）</th>
          <th>完整小计</th>
          <th>范围标识</th>
          <th>说明</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="group in current?.groups ?? []"
          :key="group.name"
          :data-testid="`group-${group.name}`"
          :data-complete="group.complete"
        >
          <td>{{ group.name }}</td>
          <td>{{ group.withheld ? '不可展示' : formatMetrics(group.metrics) }}</td>
          <td>{{ group.subtotal_allowed ? '是' : '否' }}</td>
          <td class="mono">{{ group.scope_tag }}</td>
          <td>{{ group.reason ?? '—' }}</td>
          <td>
            <button
              class="link"
              type="button"
              :disabled="group.withheld || !group.path.length"
              :data-testid="`drill-into-${group.name}`"
              @click="expand(group)"
            >
              继续下钻
            </button>
          </td>
        </tr>
        <tr v-if="current && !(current.groups ?? []).length">
          <td colspan="6" class="empty-state">已到叶子层级，没有更细的下钻节点</td>
        </tr>
      </tbody>
    </table>

    <details class="manifest-block">
      <summary>共享页面（只读，按当前读者地区重新取数）</summary>
      <div class="shared-bar">
        <label class="scope-picker">
          <span>读者账号</span>
          <select v-model="reader" data-testid="shared-reader" @change="reloadSharedList">
            <option v-for="item in accounts" :key="item.account" :value="item.account">
              {{ item.account_name }}
            </option>
          </select>
        </label>
        <span class="scope-note">共享清单只暴露路径，金额需按读者范围重新计算。</span>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>清单编号</th><th>下钻路径</th><th>作者范围</th><th>读者可访问</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in sharedItems" :key="String(item.id)">
            <td>{{ item['清单编号'] }}</td>
            <td>{{ (item['下钻路径'] as string[]).join(' > ') }}</td>
            <td>{{ item['作者范围'] }}</td>
            <td>{{ item['读者可访问'] ? '是' : '否' }}</td>
            <td>
              <button
                class="link"
                type="button"
                :disabled="!item['读者可访问']"
                @click="openShared(Number(item.id))"
              >
                以读者范围查看
              </button>
            </td>
          </tr>
          <tr v-if="!sharedItems.length">
            <td colspan="5" class="empty-state">暂无共享下钻清单，先在上方展开一条路径</td>
          </tr>
        </tbody>
      </table>
      <p v-if="sharedNotice" class="scope-note" data-testid="shared-notice">{{ sharedNotice }}</p>
      <div v-if="sharedView" class="shared-result" data-testid="shared-result">
        <p>
          读者范围：<strong>{{ sharedView.scope.scope_label }}</strong>
          （读者权限版本 scope:{{ sharedView.scope.scope_version }}）
        </p>
        <p v-if="sharedView.shared_from" class="scope-note">
          原作者范围：{{ sharedView.shared_from['作者范围'] }}；
          金额已按读者范围重新聚合，作者的全量组结果不会下发。
        </p>
        <p class="scope-note">
          当前路径 {{ sharedView.path.join(' > ') }}：
          {{ sharedView.parent.subtotal_allowed ? formatMetrics(sharedView.parent.subtotal) : '受限小计不可展示' }}
        </p>
        <ul class="shared-groups">
          <li v-for="group in sharedView.groups" :key="group.name">
            {{ group.name }} —
            {{ group.withheld ? '不可展示' : formatMetrics(group.metrics) }}
            <span class="mono">{{ group.scope_tag }}</span>
          </li>
        </ul>
      </div>
    </details>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  type AccountScope,
  type DrillGroup,
  type DrillResult,
  downloadDrill,
  formatMetrics,
  loadAccounts,
  loadDrill,
  loadShared,
  loadSharedList,
} from '@/api/drill-service'

const CUBE_ID = 2

const accounts = ref<AccountScope[]>([])
const account = ref('analyst_east_west')
const reader = ref('reader_south')
const current = ref<DrillResult | null>(null)
const path = ref<string[]>(['全国'])
const busy = ref(false)
const errorMessage = ref('')
const sharedItems = ref<Record<string, unknown>[]>([])
const sharedView = ref<DrillResult | null>(null)
const sharedNotice = ref('')

const scope = computed(() => current.value?.scope ?? null)
const childLevel = computed(() => current.value?.child_level ?? null)
const parentValue = computed(() => {
  const parent = current.value?.parent
  if (!parent) return '—'
  if (parent.subtotal_allowed) return formatMetrics(parent.subtotal)
  if (parent.withheld) return '不可展示'
  return formatMetrics(parent.partial_metrics)
})

function resetState() {
  current.value = null
  sharedView.value = null
  sharedNotice.value = ''
}

async function load() {
  busy.value = true
  errorMessage.value = ''
  try {
    const version = current.value?.scope.scope_version ?? ''
    current.value = await loadDrill(account.value, CUBE_ID, path.value, version)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下钻读取失败'
    // 权限版本变化时，退回根路径重新取数，避免继续使用旧版本结果。
    if (path.value.join('>') !== '全国') {
      path.value = ['全国']
      try {
        current.value = await loadDrill(account.value, CUBE_ID, path.value, '')
      } catch {
        current.value = null
      }
    }
  } finally {
    busy.value = false
  }
}

async function switchAccount() {
  path.value = ['全国']
  resetState()
  await load()
  await reloadSharedList()
}

async function expand(group: DrillGroup) {
  if (group.withheld) return
  path.value = group.path
  sharedView.value = null
  await load()
}

async function goTo(index: number) {
  path.value = path.value.slice(0, index + 1)
  sharedView.value = null
  await load()
}

async function exportCurrent() {
  if (!current.value) return
  errorMessage.value = ''
  busy.value = true
  try {
    await downloadDrill(
      account.value,
      CUBE_ID,
      path.value,
      current.value.scope.scope_version,
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  } finally {
    busy.value = false
  }
}

async function reloadSharedList() {
  try {
    const payload = await loadSharedList(reader.value)
    sharedItems.value = payload.items
  } catch (error) {
    sharedItems.value = []
    sharedNotice.value = error instanceof Error ? error.message : '共享清单读取失败'
  }
}

async function openShared(manifestId: number) {
  sharedNotice.value = ''
  try {
    sharedView.value = await loadShared(manifestId, reader.value, '')
  } catch (error) {
    sharedNotice.value = error instanceof Error ? error.message : '共享下钻读取失败'
  }
}

onMounted(async () => {
  try {
    accounts.value = await loadAccounts()
  } catch {
    accounts.value = []
  }
  await load()
  await reloadSharedList()
})
</script>

<style scoped>
.drill-page .scope-picker { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.scope-banner { display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; font-size: 13px; }
.scope-version { margin-left: 8px; color: var(--muted); font-family: monospace; }
.scope-flags { display: flex; gap: 12px; color: var(--muted); }
.breadcrumb { display: flex; align-items: center; gap: 6px; margin: 8px 0; }
.crumb { border: 1px solid var(--border); background: #fff; border-radius: 999px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
.crumb.current { background: var(--brand); color: #fff; border-color: var(--brand); }
.crumb-hint { color: var(--muted); font-size: 12px; }
.scope-note { color: var(--muted); font-size: 12px; margin: 6px 0; }
.mono { font-family: monospace; font-size: 11px; color: var(--muted); }
.manifest-block { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.manifest-block summary { cursor: pointer; font-size: 13px; }
.shared-bar { display: flex; align-items: flex-end; gap: 12px; margin: 10px 0; }
.shared-result { margin-top: 10px; padding: 10px; border-top: 1px dashed var(--border); }
.shared-groups { margin: 6px 0 0; padding-left: 18px; font-size: 13px; }
</style>
