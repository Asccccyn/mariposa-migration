// mariposa 真实接口层（程知行 2026-10-13 接入）：
// 占位数据全部退役——本文件是前端唯一取数通道，语义以后端为准。
// token 只存内存（对齐旧前端纪律：不落 localStorage 长驻凭据）。
export const NAMES: Record<string, string> = {
  qiaosheng: '宿主用户（qiaosheng）',
  jiaming: 'char（jiaming）',
  worker: '工具人（worker）',
}

export const session = {
  token: '',
  principal: '' as string,
  expiresAt: 0,
  generation: 0,
}

export class ApiError extends Error {
  code: string
  detail: Record<string, unknown>
  constructor(code: string, message: string, detail: Record<string, unknown> = {}) {
    super(message)
    this.code = code
    this.detail = detail
  }
}

function onUnauthorized() {
  logout()
  window.dispatchEvent(new CustomEvent('mariposa:unauthorized'))
}

export async function login(password: string): Promise<string> {
  const res = await fetch('/oauth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'grant_type=password&password=' + encodeURIComponent(password),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok || !body.access_token) {
    throw new ApiError(
      String(body.error || res.status),
      body.error_description || body.error || '登录失败',
    )
  }
  session.token = body.access_token
  session.principal = body.principal_id || ''
  session.expiresAt = Date.now() + (body.expires_in || 0) * 1000
  session.generation++
  return session.principal
}

export function logout() {
  session.token = ''
  session.principal = ''
  session.expiresAt = 0
  session.generation++
}

export const operationId = (prefix: string) => `${prefix}-${crypto.randomUUID()}`

export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : String(error)
  const detail = error.detail
  const suffix = detail.retry_after ? `（请在 ${detail.retry_after} 秒后重试）`
    : detail.current != null ? `（当前版本 ${detail.current}，请重新打开）` : ''
  return error.message + suffix
}

export async function api<T = any>(capability: string, args?: object, idem?: string): Promise<T> {
  const generation = session.generation
  const headers: Record<string, string> = {
    Authorization: 'Bearer ' + session.token,
    'Content-Type': 'application/json',
  }
  if (idem) headers['Idempotency-Key'] = idem
  const res = await fetch('/api/capability/' + capability, {
    method: 'POST',
    headers,
    body: JSON.stringify({ arguments: args || {} }),
  })
  if (generation !== session.generation) throw new ApiError('SESSION_CHANGED', '登录身份已改变，请重试')
  if (res.status === 401) {
    onUnauthorized()
    throw new ApiError('UNAUTHORIZED', '登录已过期，请重新输入密码')
  }
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new ApiError(
      String(body.error?.code || res.status),
      String(body.error?.message || body.error?.detail || res.status),
      body.error?.detail || {},
    )
  }
  if (generation !== session.generation) throw new ApiError('SESSION_CHANGED', '登录身份已改变，请重试')
  return body.data as T
}

export async function uploadSource(file: File): Promise<string> {
  const generation = session.generation
  // RSRC-03：上传必须带 filename query（服务端按原始扩展名分流格式）
  const res = await fetch('/api/source/upload?filename=' + encodeURIComponent(file.name), {
    method: 'PUT',
    headers: { Authorization: 'Bearer ' + session.token },
    body: file,
  })
  if (generation !== session.generation) throw new ApiError('SESSION_CHANGED', '登录身份已改变，请重试')
  if (res.status === 401) {
    onUnauthorized()
    throw new ApiError('UNAUTHORIZED', '登录已过期，请重新输入密码')
  }
  const body = await res.json().catch(() => ({}))
  if (!res.ok || body.ok === false) {
    throw new ApiError(String(body.error?.code || res.status), String(body.error?.message || res.status), body.error?.detail || {})
  }
  if (generation !== session.generation) throw new ApiError('SESSION_CHANGED', '登录身份已改变，请重试')
  return body.data.upload_path
}

/* ---------- 真实数据形状（以后端返回为准的精简声明） ---------- */
export type Word = {
  word_id: string
  speaker: 'jiaming' | 'qiaosheng'
  text: string
}
export type Recollection = {
  recollection_id: string
  text: string
  author?: string
  author_principal?: string
  written_at?: string
}
export type RelationEdge = {
  relation_id: string
  direction: 'out' | 'in'
  other_memory: string
  relation_type: 'continuation_of' | 'related_to' | 'contradicts' | 'custom'
  custom_label?: string | null
  reverse_label?: string | null
}
export type Binding = {
  binding_id: string
  conversation_id: string
  start_message_id: string
  end_message_id: string
}
export type KeepMark = { mark_id: string; owner: string; revoked_at?: string | null }
export type Bucket = {
  memory_id: string
  memory_date: string | null
  original_title: string
  text: string
  categories: string[]
  mood?: { text?: string | null; tags?: string[] } | null
  version: number
  representation?: string
  compression_state?: string
  creation_mode?: string
  content_gap?: { code: string; reason: string; note: string }
  // 附加面（memory.cards.batch）
  words?: Word[]
  recollections?: Recollection[]
  relations?: RelationEdge[]
  bindings?: Binding[]
  keeps?: KeepMark[]
}

export type OpenedBucket = Bucket & { view_receipt: string; confirm_key?: string }
export type ViewReceipt = { memoryId: string; version: number; id: string; confirmKey?: string }

export async function loadBucket(id: string, opened?: Bucket): Promise<Bucket> {
  const [bucket, extra, keeps] = await Promise.all([
    opened ? Promise.resolve(opened) : api<Bucket>('memory.get', { memory_id: id }),
    api<{ cards: Record<string, Partial<Bucket>> }>('memory.cards.batch', { memory_ids: [id] }),
    api<{ marks: KeepMark[] }>('memory.keeps.list', { memory_id: id }),
  ])
  return { ...bucket, ...extra.cards[id], keeps: keeps.marks }
}

export async function loadBucketsByIds(ids: string[]): Promise<Bucket[]> {
  if (!ids.length) return []
  const extra = await api<{ cards: Record<string, Partial<Bucket> & { memory: Bucket }> }>('memory.cards.batch', { memory_ids: ids, include_memory: true })
  return ids.map(id => {
    const { memory, ...fields } = extra.cards[id]
    return { ...memory, ...fields }
  })
}

export async function loadAllBuckets(): Promise<Bucket[]> {
  const items: Bucket[] = []
  let cursor: any = undefined
  do {
    const d = await api<{ items: Bucket[]; next_cursor?: any }>('memory.list', {
      limit: 200, ...(cursor ? { cursor } : {}),
    })
    items.push(...d.items)
    cursor = d.next_cursor
  } while (cursor)
  // 首页只需标题/事件；附加面在详情、关系页按需批量读取。
  return items
}

export async function loadMoodVocab(): Promise<string[]> {
  try {
    const d = await api<{ categories: string[] }>('memory.mood.vocab', {})
    return d.categories || []
  } catch {
    return []
  }
}
