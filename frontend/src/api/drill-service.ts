/** 下钻取数 API：账号范围、下钻明细、直查、下载与共享只读。 */

export type VisibleScope = {
  account: string
  account_name: string
  mode: 'interactive' | 'readonly'
  readonly: boolean
  scope_version: string
  scope_label: string
  visible_cities: Record<string, string[]>
  hidden_city_count: number
  complete: boolean
}

export type Metrics = { revenue: number; orders: number }

export type DrillGroup = {
  name: string
  level: string
  path: string[]
  parent_path: string[]
  metrics: Metrics | null
  complete: boolean
  subtotal_allowed: boolean
  partial: boolean
  restricted_children: boolean
  withheld: boolean
  reason?: string
  row_count: number
  scope_version: string
  scope_tag: string
  scope_label: string
}

export type DrillParent = {
  path: string[]
  name: string
  level: string
  complete: boolean
  subtotal_allowed: boolean
  subtotal: Metrics | null
  partial_metrics: Metrics | null
  restricted_children: boolean
  withheld: boolean
  reason?: string
  scope_version: string
  scope_tag: string
  scope_label: string
}

export type DrillResult = {
  ok: boolean
  cube_id: number
  account: string
  readonly: boolean
  mode?: string
  scope: VisibleScope
  path: string[]
  level: string
  child_level: string | null
  hierarchy: {
    levels: string[]
    hierarchy_source: string
    transitions: { start: string; target: string }[]
  }
  parent: DrillParent
  groups: DrillGroup[]
  rows?: DrillGroup[]
  manifest?: { 清单编号: string; id: number; 权限版本: string; 组数: number; 父级完整: boolean }
  shared_from?: Record<string, string | number>
}

export type AccountScope = VisibleScope

async function readJson<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = (body as { detail?: { message?: string } | string }).detail
    const message =
      typeof detail === 'string' ? detail : detail?.message ?? `接口返回 ${response.status}`
    throw new Error(message)
  }
  return body as T
}

export async function loadAccounts(): Promise<AccountScope[]> {
  const response = await fetch('/api/drill/accounts')
  const body = await readJson<{ items: AccountScope[] }>(response)
  return body.items
}

export async function loadScope(account: string): Promise<VisibleScope> {
  const response = await fetch(`/api/drill/scope?account=${encodeURIComponent(account)}`)
  const body = await readJson<{ scope: VisibleScope }>(response)
  return body.scope
}

export async function loadDrill(
  account: string,
  cubeId: number,
  path: string[],
  scopeVersion: string,
): Promise<DrillResult> {
  const params = new URLSearchParams({
    account,
    cube_id: String(cubeId),
    path: path.join('>'),
    scope_version: scopeVersion,
  })
  return readJson<DrillResult>(await fetch(`/api/drill/detail?${params}`))
}

export async function loadDirect(
  account: string,
  cubeId: number,
  groupBy: 'region' | 'city',
  region: string,
  scopeVersion: string,
): Promise<DrillResult> {
  const params = new URLSearchParams({
    account,
    cube_id: String(cubeId),
    group_by: groupBy,
    region,
    scope_version: scopeVersion,
  })
  return readJson<DrillResult>(await fetch(`/api/drill/direct?${params}`))
}

export async function loadShared(
  manifestId: number,
  reader: string,
  scopeVersion: string,
): Promise<DrillResult> {
  const params = new URLSearchParams({ reader, scope_version: scopeVersion })
  return readJson<DrillResult>(
    await fetch(`/api/drill/shared/${manifestId}?${params}`),
  )
}

export async function downloadDrill(
  account: string,
  cubeId: number,
  path: string[],
  scopeVersion: string,
): Promise<void> {
  const params = new URLSearchParams({
    account,
    cube_id: String(cubeId),
    path: path.join('>'),
    scope_version: scopeVersion,
  })
  const response = await fetch(`/api/drill/export?${params}`)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = (body as { detail?: { message?: string } }).detail
    throw new Error(detail?.message ?? '导出失败：权限范围可能已变化')
  }
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `drill_cube${cubeId}_${response.headers.get('X-Scope-Version') ?? ''}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadManifest(account?: string): Promise<Record<string, unknown>[]> {
  const query = account ? `?account=${encodeURIComponent(account)}` : ''
  const response = await fetch(`/api/drill/manifest${query}`)
  const body = await readJson<{ items: Record<string, unknown>[] }>(response)
  return body.items
}

export async function loadSharedList(
  reader: string,
): Promise<{ items: Record<string, unknown>[]; reader: VisibleScope }> {
  const response = await fetch(`/api/drill/shared?reader=${encodeURIComponent(reader)}`)
  return readJson(response)
}

export function formatMetrics(metrics: Metrics | null): string {
  if (!metrics) return '不可展示'
  return `¥${metrics.revenue.toLocaleString()} / ${metrics.orders} 单`
}
