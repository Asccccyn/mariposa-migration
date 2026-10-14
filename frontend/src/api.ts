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
}

export class ApiError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

function onUnauthorized() {
  session.token = ''
  session.principal = ''
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
  return session.principal
}

export function logout() {
  session.token = ''
  session.principal = ''
}

export async function api<T = any>(capability: string, args?: object, idem?: string): Promise<T> {
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
  if (res.status === 401) {
    onUnauthorized()
    throw new ApiError('UNAUTHORIZED', '登录已过期，请重新输入密码')
  }
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new ApiError(
      String(body.error?.code || res.status),
      String(body.error?.message || body.error?.detail || res.status),
    )
  }
  return body.data as T
}

export async function uploadSource(file: File): Promise<string> {
  // RSRC-03：上传必须带 filename query（服务端按原始扩展名分流格式）
  const res = await fetch('/api/source/upload?filename=' + encodeURIComponent(file.name), {
    method: 'PUT',
    headers: { Authorization: 'Bearer ' + session.token },
    body: file,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok || body.ok === false) {
    throw new ApiError(String(body.error?.code || res.status), String(body.error?.message || res.status))
  }
  return body.data.upload_path
}

/* ---------- 真实数据形状（以后端返回为准的精简声明） ---------- */
export type Word = {
  word_id: string
  speaker: 'jiaming' | 'qiaosheng'
  text: string
  expression_kind: 'verbatim' | 'paraphrase' | 'unspecified'
}
export type Recollection = {
  recollection_id: string
  text: string
  author?: string
  author_principal?: string
  created_at?: string
}
export type RelationEdge = {
  relation_id: string
  direction: 'out' | 'in'
  other_memory: string
  relation_type: 'continuation_of' | 'related_to' | 'contradicts' | 'custom'
  custom_label?: string | null
}
export type Binding = {
  binding_id: string
  conversation_id: string
  start_message_id: string
  end_message_id: string
}
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
  // 附加面（memory.cards.batch）
  words?: Word[]
  recollections?: Recollection[]
  relations?: RelationEdge[]
  bindings?: Binding[]
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
  // 附加面批量（cards.batch ≤100/次；逐卡拉取是 429 根因，禁止回退）
  for (let i = 0; i < items.length; i += 100) {
    const ids = items.slice(i, i + 100).map((b) => b.memory_id)
    const d = await api<{ cards: Record<string, any> }>('memory.cards.batch', { memory_ids: ids })
    for (const b of items.slice(i, i + 100)) {
      const c = d.cards[b.memory_id] || {}
      b.words = c.words || []
      b.recollections = c.recollections || []
      b.relations = c.relations || []
      b.bindings = c.bindings || []
    }
  }
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
