import { useEffect, useMemo, useRef, useState } from 'react'
import {
  api, ApiError, loadAllBuckets, loadMoodVocab, login, logout, NAMES,
  session, uploadSource,
  type Bucket, type Word,
} from './api'

/* ==================================================================
 * 接入说明（程知行 2026-10-13）：
 * 画风/布局/交互逐字保留原设计稿；占位数据（INITIAL/RELATIONS/SELF/
 * sourceOf/SNIPPETS）全部退役，数据一律来自 mariposa 真实接口：
 *   首页   memory.list + memory.cards.batch（批量，禁逐卡 N+1）
 *   搜索   memory.recall（关键词=后端短语引擎；any/all=逐词真实检索
 *          的并集/交集；分类/心情 any-all=category_match/mood_match）
 *   关系   cards.batch relations 边 → 连通分量=故事线
 *   原文   source.conversations.list / source.conversation.get /
 *          source.search / source.import / source.bindings.of_conversation
 *          （§6.1 反查：原文段→桶，点击跳桶；桶抽屉"查看原文"反向跳入）
 *   后台   maintenance.recall_policy.* / activity.list / jobs.status /
 *          outbox.status / settings.get / semantic.warmup
 *   I 抽屉 i.items.list + relations.list(item)
 * 编辑   memory.update（标题/正文/日期，乐观锁）+ categories.replace；
 *          心情区只读（当时心情仅char同期可写——她 2026-10-13 裁定）
 * ================================================================== */

type Tab = 'search' | 'relation' | 'home' | 'source' | 'admin'

// 九分类以后端为准（categories.py LABELS）；顺序沿用设计稿的视觉顺序
const CATEGORIES: { key: string; label: string }[] = [
  { key: 'milestone', label: '重大转折' },
  { key: 'anniversary', label: '纪念' },
  { key: 'date', label: '约会' },
  { key: 'sweet', label: '甜蜜' },
  { key: 'daily', label: '日常' },
  { key: 'sex', label: '做爱' },
  { key: 'reloplay', label: '剧本' },
  { key: 'sad', label: '伤心的事' },
  { key: 'plan', label: '计划' },
]
const CAT_LABELS: Record<string, string> = Object.fromEntries(CATEGORIES.map((c) => [c.key, c.label]))
// 心情七大类：登录后从 memory.mood.vocab 动态拉取（单一事实源），
// 未拉到前用 2026-10-05 终版表兜底显示
const MOOD_FALLBACK = ['开心', '爱', '生气', '吃醋', '悲伤', '渴望', '不安']
const PAGE_SIZE = 30

const REL_TYPE_LABEL: Record<string, string> = {
  continuation_of: '接着这段',
  related_to: '相关',
  contradicts: '有出入',
  custom: '关联',
}

/* ---------- Icons（设计稿原样） ---------- */
const ic = 'w-6 h-6'
const Icons: Record<Tab, (a: boolean) => React.ReactNode> = {
  search: (a) => (
    <svg viewBox="0 0 24 24" className={ic} fill="none" stroke="currentColor" strokeWidth={a ? 2.2 : 1.7} strokeLinecap="round">
      <circle cx="10.5" cy="10.5" r="6.5" fill={a ? 'rgba(212,169,74,.25)' : 'none'} />
      <path d="M15.5 15.5 20 20" />
      <path d="M8 9.2a3 3 0 0 1 2.5-1.7" opacity=".6" />
    </svg>
  ),
  relation: (a) => (
    <svg viewBox="0 0 24 24" className={ic} fill="none" stroke="currentColor" strokeWidth={a ? 2.1 : 1.7} strokeLinecap="round">
      <circle cx="6" cy="7" r="2.6" fill={a ? 'rgba(212,169,74,.3)' : 'none'} />
      <circle cx="18" cy="7" r="2.6" fill={a ? 'rgba(212,169,74,.3)' : 'none'} />
      <circle cx="12" cy="18" r="2.6" fill={a ? 'rgba(212,169,74,.3)' : 'none'} />
      <path d="M8.4 8.3 10.8 15.7M15.6 8.3 13.2 15.7M8.6 7h6.8" />
    </svg>
  ),
  home: () => (
    <svg viewBox="0 0 32 32" className="w-8 h-8" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round">
      <path d="M16 15C13 8 6 5 4 8.5s1.5 8 7 8.5c-4 1.5-5 5.5-2.5 7s6-2 7.5-7z" fill="#d9b44a" fillOpacity=".35" />
      <path d="M16 15c3-7 10-10 12-6.5s-1.5 8-7 8.5c4 1.5 5 5.5 2.5 7s-6-2-7.5-7z" fill="#d9b44a" fillOpacity=".35" />
      <path d="M16 13v12M16 13l-2-4M16 13l2-4" strokeLinecap="round" />
    </svg>
  ),
  source: (a) => (
    <svg viewBox="0 0 24 24" className={ic} fill="none" stroke="currentColor" strokeWidth={a ? 2.1 : 1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 5.5C6.5 4.5 9.5 4.5 12 6c2.5-1.5 5.5-1.5 8-.5v13c-2.5-1-5.5-1-8 .5-2.5-1.5-5.5-1.5-8-.5z" fill={a ? 'rgba(212,169,74,.25)' : 'none'} />
      <path d="M12 6v13.5" />
    </svg>
  ),
  admin: (a) => (
    <svg viewBox="0 0 24 24" className={ic} fill="none" stroke="currentColor" strokeWidth={a ? 2.1 : 1.7} strokeLinecap="round">
      <path d="M5 7h9M18 7h1M5 12h3M12 12h7M5 17h11M20 17h-0" />
      <circle cx="16" cy="7" r="2" fill={a ? '#d4a94a' : 'none'} />
      <circle cx="10" cy="12" r="2" fill={a ? '#d4a94a' : 'none'} />
      <circle cx="18" cy="17" r="2" fill={a ? '#d4a94a' : 'none'} />
    </svg>
  ),
}
const TABS: Tab[] = ['search', 'relation', 'home', 'source', 'admin']
const TAB_NAMES: Record<Tab, string> = { search: '搜索', relation: '关系', home: '首页', source: '原文', admin: '后台' }

function Butterfly({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 32" className={className} fill="none">
      <path d="M20 15C16 6 6 2 3 7s2 10 9 11c-5 2-6 7-3 9s8-3 11-10z" fill="#1f3a68" />
      <path d="M20 15c4-9 14-13 17-8s-2 10-9 11c5 2 6 7 3 9s-8-3-11-10z" fill="#1f3a68" />
      <path d="M20 12v14" stroke="#d9b44a" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="10" cy="10" r="2" fill="#d9b44a" />
      <circle cx="30" cy="10" r="2" fill="#d9b44a" />
    </svg>
  )
}

/* ---------- Drawer（设计稿原样） ---------- */
function Drawer({ open, side, onClose, children, glass }: { open: boolean; side: 'left' | 'right'; onClose: () => void; children: React.ReactNode; glass?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className={`fixed inset-0 z-50 ${open ? '' : 'pointer-events-none'}`}>
      <div onClick={onClose} className={`absolute inset-0 ${glass ? 'bg-[#1f3a68]/10' : 'bg-[#1e2a4a]/30 backdrop-blur-[2px]'} transition-opacity duration-300 ${open ? 'opacity-100' : 'opacity-0'}`} />
      <aside
        className={`absolute top-0 bottom-0 ${side === 'left' ? 'left-0' : 'right-0'} w-[88%] max-w-[440px] flex flex-col overflow-hidden ${glass ? `bg-white/45 backdrop-blur-2xl backdrop-saturate-150 border-white/70 ${side === 'left' ? 'border-r shadow-[20px_0_60px_rgba(31,58,104,0.18)]' : 'border-l shadow-[-20px_0_60px_rgba(31,58,104,0.18)]'}` : 'bg-[#fffdf7] shadow-2xl'} transition-transform duration-[380ms] ease-[cubic-bezier(.22,1,.36,1)] ${
          open ? 'translate-x-0' : side === 'left' ? '-translate-x-full' : 'translate-x-full'
        }`}
        style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        {children}
      </aside>
    </div>
  )
}

/* ---------- 工具 ---------- */
const fmtTime = (t?: string | null) =>
  t ? t.replace('T', ' ').replace(/\.\d+.*$/, '') : '—'
const dateOf = (b: Bucket) => b.memory_date || ''
function collectMemoryIds(v: any, out: Set<string>) {
  // 容错提取：relations.list 返回里的 memory 端点
  if (!v || typeof v !== 'object') return
  if (Array.isArray(v)) { v.forEach((x) => collectMemoryIds(x, out)); return }
  if (typeof v.memory_id === 'string') out.add(v.memory_id)
  for (const k of Object.keys(v)) if (k !== 'type') collectMemoryIds(v[k], out)
}

/* ---------- 关系分组（真实边 → 连通分量=故事线） ---------- */
function buildGroups(buckets: Bucket[]) {
  const edges = new Map<string, Set<string>>()
  const labels = new Map<string, string>()
  for (const b of buckets) {
    for (const r of b.relations || []) {
      const a = b.memory_id, c = r.other_memory
      if (!edges.has(a)) edges.set(a, new Set())
      if (!edges.has(c)) edges.set(c, new Set())
      edges.get(a)!.add(c)
      edges.get(c)!.add(a)
      const lab = r.custom_label || REL_TYPE_LABEL[r.relation_type] || '关联'
      labels.set(a + '|' + c, lab)
      labels.set(c + '|' + a, lab)
    }
  }
  const seen = new Set<string>()
  const groups: { id: string; name: string; ids: string[]; labels: Record<string, string> }[] = []
  for (const start of edges.keys()) {
    if (seen.has(start)) continue
    const comp: string[] = []
    const stack = [start]
    while (stack.length) {
      const n = stack.pop()!
      if (seen.has(n)) continue
      seen.add(n)
      comp.push(n)
      for (const m of edges.get(n) || []) if (!seen.has(m)) stack.push(m)
    }
    const ids = comp
      .filter((id) => buckets.some((b) => b.memory_id === id))
      .sort((x, y) => {
        const bx = buckets.find((b) => b.memory_id === x)!
        const by = buckets.find((b) => b.memory_id === y)!
        return (dateOf(bx) || '9999').localeCompare(dateOf(by) || '9999')
      })
    if (ids.length < 2) continue
    const first = buckets.find((b) => b.memory_id === ids[0])!
    const l2 = first.relations?.find((r) => ids.includes(r.other_memory))
    groups.push({
      id: ids.join('+'),
      name: (l2?.custom_label || REL_TYPE_LABEL[l2?.relation_type || 'custom'] || '故事线') + ' · ' + ids.length + ' 段',
      ids,
      labels: Object.fromEntries(ids.slice(1).map((id, k) => [id, labels.get(ids[k] + '|' + id) || ''])),
    })
  }
  return groups
}
function groupOfBucket(b: Bucket, buckets: Bucket[]) {
  if (!(b.relations || []).length) return null
  return buildGroups(buckets).find((g) => g.ids.includes(b.memory_id)) || null
}

/* ---------- App ---------- */
export default function App() {
  const [authed, setAuthed] = useState(false)
  const [booting, setBooting] = useState(false)
  const [bootError, setBootError] = useState('')
  const [buckets, setBuckets] = useState<Bucket[]>([])
  const [moods, setMoods] = useState<string[]>(MOOD_FALLBACK)

  const [tab, setTab] = useState<Tab>('home')
  const [category, setCategory] = useState<string | null>(null)
  const [pages, setPages] = useState<Record<string, number>>({})
  const scrollMemo = useRef<Record<string, number>>({})
  const [openId, setOpenId] = useState<string | null>(null)
  const [lastId, setLastId] = useState<string | null>(null)
  const [selfOpen, setSelfOpen] = useState(false)
  const [focusRel, setFocusRel] = useState<string | null>(null)
  const [query, setQuery] = useState<Query>(EMPTY_Q)
  // 原文页跳转目标：桶抽屉"查看原文"→ {conversation_id, around_seq}
  const [srcJump, setSrcJump] = useState<{ conversation_id: string; around_seq?: number } | null>(null)

  useEffect(() => {
    const h = () => { setAuthed(false); setBuckets([]) }
    window.addEventListener('mariposa:unauthorized', h)
    return () => window.removeEventListener('mariposa:unauthorized', h)
  }, [])

  const boot = async (pw: string) => {
    setBootError('')
    setBooting(true)
    try {
      await login(pw)
      const [bs, mv] = await Promise.all([loadAllBuckets(), loadMoodVocab()])
      setBuckets(bs)
      if (mv.length) setMoods(mv)
      setAuthed(true)
    } catch (e) {
      setBootError(e instanceof Error ? e.message : String(e))
    } finally {
      setBooting(false)
    }
  }

  const refreshBuckets = async () => {
    try {
      const bs = await loadAllBuckets()
      setBuckets(bs)
    } catch { /* 401 已由 api 层处理；其余保持现状 */ }
  }

  const key = category ?? '__all'
  const page = pages[key] ?? 1
  const list = useMemo(() => (category ? buckets.filter((b) => (b.categories || []).includes(category)) : buckets), [buckets, category])
  const totalPages = Math.max(1, Math.ceil(list.length / PAGE_SIZE))
  const shown = list.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  const current = buckets.find((b) => b.memory_id === openId) ?? null
  const byId = (id: string) => buckets.find((b) => b.memory_id === id)

  const switchCategory = (c: string | null) => {
    scrollMemo.current[key] = window.scrollY
    setCategory(c)
    requestAnimationFrame(() => window.scrollTo({ top: scrollMemo.current[c ?? '__all'] ?? 0 }))
  }
  const goPage = (p: number) => {
    setPages((s) => ({ ...s, [key]: p }))
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const openBucket = (id: string) => {
    setOpenId(id)
    setLastId(id)
  }
  const goTab = (t: Tab) => {
    if (t === tab) return
    if (tab === 'home') scrollMemo.current['__home_' + key] = window.scrollY
    setTab(t)
    requestAnimationFrame(() => window.scrollTo({ top: t === 'home' ? scrollMemo.current['__home_' + key] ?? 0 : 0 }))
  }
  const patchBucket = (b: Bucket) =>
    setBuckets((bs) => bs.map((x) => (x.memory_id === b.memory_id ? b : x)))

  if (!authed) {
    return <LoginPage onLogin={boot} loading={booting} error={bootError} />
  }

  return (
    <div className="min-h-screen relative overflow-x-hidden bg-[#fdfaf2]">
      {/* Header（设计稿原样 + 身份回显/退出） */}
      <header className="sticky top-0 z-30 bg-[#fdfaf2]/90 backdrop-blur-md border-b border-[#e9dcb8]/60" style={{ paddingTop: 'env(safe-area-inset-top)' }}>
        <div className="max-w-5xl mx-auto px-4 pt-3 pb-2 flex items-center gap-2">
          <Butterfly className="w-8 h-7 flutter" />
          <h1 className="serif text-xl font-bold tracking-[0.08em] italic text-[#1e2a4a]">Mariposa</h1>
          <span className="ml-auto text-xs text-[#8a7a52]">{tab === 'home' ? `${list.length} 个记忆桶` : TAB_NAMES[tab]}</span>
          <span className="text-[11px] text-[#8a7a52]">· {(NAMES[session.principal] || session.principal).split('（')[0]}</span>
          <button
            onClick={() => { logout(); setAuthed(false); setBuckets([]) }}
            className="text-[11px] text-[#a8862c] border-b border-[#a8862c]/50"
          >退出</button>
        </div>
        {tab === 'home' && (
          <nav className="max-w-5xl mx-auto px-3 pb-2.5 grid grid-cols-5 gap-1 sm:flex sm:flex-wrap sm:gap-1.5">
            {[null, ...CATEGORIES].map((c) => {
              const active = c?.key === category
              return (
                <button
                  key={c?.key ?? 'all'}
                  onClick={() => switchCategory(c?.key ?? null)}
                  className={`px-1 sm:px-3 py-1 rounded-full text-[12.5px] sm:text-[13px] truncate transition-all duration-300 ${
                    active ? 'bg-[#1f3a68] text-[#f6e3a8]' : 'text-[#3a4a6e] hover:bg-[#f3e6c2]/70'
                  }`}
                >
                  {c?.label ?? '全部'}
                </button>
              )
            })}
          </nav>
        )}
      </header>

      <main className="max-w-5xl mx-auto px-3 sm:px-4 pt-3 pb-36">
        {tab === 'home' ? (
          <>
            {category === 'plan' && (
              <PlansBlock />
            )}
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 sm:gap-x-8 border-t border-[#e6dcc3]">
              {shown.map((b) => (
                <article
                  key={b.memory_id}
                  onClick={() => openBucket(b.memory_id)}
                  className={`relative cursor-pointer py-3 pl-3 pr-1 border-b border-[#e6dcc3] transition-colors duration-200 hover:bg-[#f5ead0]/50 ${lastId === b.memory_id ? 'bg-[#f5ead0]/60' : ''}`}
                >
                  <span className={`absolute left-0 top-3 bottom-3 w-[3px] rounded-full transition-colors ${lastId === b.memory_id ? 'bg-[#d9b44a]' : 'bg-transparent'}`} />
                  <div className="flex items-baseline gap-2">
                    <h3 className="serif font-bold text-[15px] text-[#1f3a68] leading-snug flex-1 truncate">{b.original_title}</h3>
                    <time className="shrink-0 text-[11.5px] text-[#a8862c] tabular-nums">{dateOf(b) || '日期未知'}</time>
                  </div>
                  <p className="mt-1 text-[13px] leading-relaxed text-[#4a5675] line-clamp-3">{b.text}</p>
                  <span className="mt-1 inline-block text-[11px] text-[#7d8aa8]">{(b.categories || []).map((c) => `#${CAT_LABELS[c] || c}`).join(' ')}</span>
                </article>
              ))}
              {shown.length === 0 && <p className="col-span-full text-center text-sm text-[#8a7a52] py-16">这个分类还没有记忆</p>}
            </div>
            {totalPages > 1 && <Pager page={page} total={totalPages} onChange={goPage} />}
          </>
        ) : tab === 'search' ? (
          <SearchPage q={query} setQ={setQuery} buckets={buckets} byId={byId} onOpen={openBucket} lastId={lastId} moods={moods} />
        ) : tab === 'relation' ? (
          <RelationPage focus={focusRel} setFocus={setFocusRel} buckets={buckets} byId={byId} onOpen={openBucket} />
        ) : tab === 'source' ? (
          <SourcePage jump={srcJump} clearJump={() => setSrcJump(null)} buckets={buckets} onOpen={openBucket} refreshBuckets={refreshBuckets} />
        ) : (
          <AdminPage bucketsCount={buckets.length} />
        )}
      </main>

      {/* Floating I */}
      <button
        onClick={() => setSelfOpen(true)}
        aria-label="char的自我认知"
        className="fixed right-4 z-40 w-11 h-11 rounded-full serif font-bold text-lg text-[#f6e3a8] bg-[#1f3a68] shadow-lg shadow-[#1f3a68]/25 ring-2 ring-[#d9b44a] active:scale-90 transition-transform"
        style={{ bottom: 'calc(env(safe-area-inset-bottom) + 92px)' }}
      >
        I
      </button>

      <BottomNav tab={tab} onChange={(t) => (t === 'home' && tab === 'home' ? switchCategory(null) : goTab(t))} />

      {/* Bucket drawer (left) */}
      <Drawer open={openId !== null} side="left" onClose={() => setOpenId(null)}>
        {current && (
          <BucketDetail
            key={current.memory_id + ':' + current.version}
            b={current}
            allBuckets={buckets}
            onClose={() => setOpenId(null)}
            onSaved={patchBucket}
            onRelation={(gid) => { setFocusRel(gid); setOpenId(null); goTab('relation') }}
            onSource={async () => {
              // 桶→原文：取绑定，跳到原文页对应会话/序号
              try {
                const bid = (current.bindings || [])[0]
                if (!bid) { window.alert('这个桶还没有绑定原文'); return }
                const d = await api<{ bindings: { memory_id: string; start_seq: number }[] }>(
                  'source.bindings.of_conversation', { conversation_id: bid.conversation_id })
                const hit = d.bindings.find((x) => x.memory_id === current.memory_id)
                setSrcJump({ conversation_id: bid.conversation_id, around_seq: hit?.start_seq })
                setOpenId(null)
                goTab('source')
              } catch (e) {
                window.alert('打开原文失败：' + (e instanceof Error ? e.message : e))
              }
            }}
          />
        )}
      </Drawer>

      {/* Self drawer (right), frosted glass（视觉原样；内容接 i.items.list） */}
      <SelfDrawer open={selfOpen} onClose={() => setSelfOpen(false)} buckets={buckets} onOpen={(id) => { setSelfOpen(false); openBucket(id) }} />
    </div>
  )
}

/* ---------- 登录页（新增：稿内无登录界面；密码→OAuth token，token 只存内存） ---------- */
function LoginPage({ onLogin, loading, error }: { onLogin: (pw: string) => void; loading: boolean; error: string }) {
  const [pw, setPw] = useState('')
  return (
    <div className="min-h-screen bg-[#fdfaf2] flex flex-col items-center justify-center px-6">
      <Butterfly className="w-20 h-16 flutter" />
      <h1 className="serif text-3xl font-bold tracking-[0.08em] italic text-[#1e2a4a] mt-4">Mariposa</h1>
      <p className="text-xs tracking-[0.2em] text-[#a8862c] mt-2">记忆图书馆</p>
      <form
        className="mt-10 w-full max-w-xs"
        onSubmit={(e) => { e.preventDefault(); if (pw && !loading) onLogin(pw) }}
      >
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="登录密码"
          autoComplete="current-password"
          className="w-full bg-transparent border-0 border-b border-dashed border-[#d9b44a] px-1 py-2 text-center serif text-lg text-[#1f3a68] outline-none focus:border-solid focus:border-[#1f3a68] placeholder:text-[#c2b48f] placeholder:italic"
        />
        <button
          disabled={!pw || loading}
          className="mt-6 w-full py-2.5 rounded-full bg-[#1f3a68] text-[#f6e3a8] serif italic tracking-wider disabled:opacity-40 active:scale-95 transition"
        >
          {loading ? '正在取回记忆…' : '进入'}
        </button>
      </form>
      {error && <p className="mt-4 text-[13px] text-[#c0392b]">{error}</p>}
      <p className="mt-10 text-[11px] text-[#b5ad98] text-center leading-relaxed">
        网页密码 = 宿主用户（qiaosheng）· MCP 密码 = char（jiaming）<br />
        身份由所输密码决定；token 不落本地，刷新后需重新登录
      </p>
    </div>
  )
}

/* ---------- 计划区块（她 2026-10-13 裁定：计划全套接首页"计划"分类下） ---------- */
const PLAN_STATES = ['planned', 'active', 'waiting', 'blocked', 'done', 'cancelled']
const PLAN_STATE_LABEL: Record<string, string> = {
  planned: '计划中', active: '进行中', waiting: '等待', blocked: '受阻', done: '完成', cancelled: '取消',
}
type Plan = { plan_id: string; title: string; content?: string | null; state: string; version: number; due_at?: string | null; starts_at?: string | null }

function PlansBlock() {
  const [plans, setPlans] = useState<Plan[]>([])
  const [title, setTitle] = useState('')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const load = async () => {
    try {
      const d = await api<{ plans: Plan[] }>('plan.list', {})
      setPlans(d.plans || [])
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }
  useEffect(() => { load() }, [])
  const create = async () => {
    if (!title.trim()) return
    setErr(''); setBusy(true)
    try {
      await api('plan.create', { title: title.trim(), state: 'planned', ...(due ? { due_at: due } : {}) }, 'ui-plan-' + Date.now())
      setTitle(''); setDue('')
      load()
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const setState_ = async (p: Plan, state: string) => {
    setErr('')
    try {
      await api('plan.update', { plan_id: p.plan_id, expected_version: p.version, state })
      load()
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); load() }
  }
  const stateTag = (s: string) =>
    s === 'done' ? 'bg-[#f5ead0] text-[#7a5d14]'
    : s === 'cancelled' ? 'bg-[#e9e4d6] text-[#9aa3b8] line-through'
    : ['active', 'waiting', 'blocked'].includes(s) ? 'bg-[#1f3a68] text-[#f6e3a8]'
    : 'bg-[#e8edf6] text-[#1f3a68]'
  return (
    <section className="mb-4 rounded-2xl bg-white/70 border border-[#e6dcc3] px-4 pt-3 pb-3.5">
      <h3 className="serif font-bold text-[#1f3a68]">计划 <span className="text-[11px] font-normal text-[#8a7a52]">（{plans.length}）· 下面是绑定了计划的记忆桶</span></h3>
      <div className="mt-2 flex gap-2 flex-wrap">
        <input value={title} onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && create()}
          placeholder="新计划标题…" className="flex-1 min-w-[140px] bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 py-1 text-[14px] text-[#1f3a68] outline-none focus:border-solid focus:border-[#1f3a68] placeholder:text-[#c2b48f] placeholder:italic" />
        <input type="date" value={due} onChange={(e) => setDue(e.target.value)} className="bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 py-1 text-[12px] text-[#7a5d14] tabular-nums outline-none focus:border-solid" />
        <button onClick={create} disabled={!title.trim() || busy}
          className="serif italic text-[13px] text-[#1f3a68] px-2 pb-0.5 border-b border-[#1f3a68] disabled:text-[#c2b48f] disabled:border-transparent">创建</button>
      </div>
      <div className="mt-2 space-y-1.5">
        {plans.map((p) => (
          <div key={p.plan_id} className="flex items-center gap-2 py-1 border-b border-[#ece3cc] last:border-0">
            <span className={`shrink-0 text-[11px] px-2 py-0.5 rounded-full ${stateTag(p.state)}`}>{PLAN_STATE_LABEL[p.state] || p.state}</span>
            <span className={`serif font-bold text-[14px] text-[#1f3a68] truncate flex-1 ${p.state === 'cancelled' ? 'opacity-50' : ''}`}>{p.title}</span>
            {p.due_at && <span className="shrink-0 text-[11px] text-[#a8862c] tabular-nums">{p.due_at.slice(0, 10)} 到期</span>}
            <span className="shrink-0 flex gap-1">
              {PLAN_STATES.filter((s) => s !== p.state).map((s) => (
                <button key={s} onClick={() => setState_(p, s)} title={`改为 ${PLAN_STATE_LABEL[s]}`}
                  className="text-[10.5px] text-[#8a7a52] hover:text-[#1f3a68] border-b border-transparent hover:border-[#a8862c]">{PLAN_STATE_LABEL[s]}</button>
              ))}
            </span>
          </div>
        ))}
        {!plans.length && <p className="text-[12px] text-[#9aa3b8] py-1">还没有计划（创建后，hold 记忆时以 plan_ids 绑定即归入计划分类）</p>}
      </div>
      {err && <p className="mt-1.5 text-[11.5px] text-[#c0392b]">{err}</p>}
    </section>
  )
}

/* ---------- Pager（设计稿原样） ---------- */
function Pager({ page, total, onChange }: { page: number; total: number; onChange: (p: number) => void }) {
  const nums: (number | '…')[] = []
  for (let i = 1; i <= total; i++) {
    if (i === 1 || i === total || Math.abs(i - page) <= 1) nums.push(i)
    else if (nums[nums.length - 1] !== '…') nums.push('…')
  }
  const arrow = (dir: -1 | 1) => (
    <button
      disabled={dir < 0 ? page === 1 : page === total}
      onClick={() => onChange(page + dir)}
      aria-label={dir < 0 ? '上一页' : '下一页'}
      className="w-8 h-8 flex items-center justify-center text-[#1f3a68] disabled:opacity-25 hover:text-[#a8862c] transition-colors"
    >
      <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
        <path d={dir < 0 ? 'M15 5 8 12l7 7' : 'm9 5 7 7-7 7'} />
      </svg>
    </button>
  )
  return (
    <div className="mt-6 flex flex-col items-center gap-2">
      <div className="flex items-center gap-0.5">
        {arrow(-1)}
        {nums.map((n, i) =>
          n === '…' ? (
            <span key={i} className="w-6 text-center text-[#c9b27a] text-xs tracking-widest">···</span>
          ) : (
            <button
              key={i}
              onClick={() => onChange(n)}
              className={`relative w-8 h-9 serif tabular-nums transition-all duration-300 ${n === page ? 'text-[#1f3a68] text-[17px] font-bold' : 'text-[#8f98ad] text-[14px] hover:text-[#1f3a68]'}`}
            >
              {n}
              <span className={`absolute left-1/2 -translate-x-1/2 bottom-0.5 h-[5px] w-[5px] rotate-45 bg-[#d9b44a] transition-all duration-300 ${n === page ? 'opacity-100 scale-100' : 'opacity-0 scale-0'}`} />
            </button>
          ),
        )}
        {arrow(1)}
      </div>
      <label className="flex items-center gap-1.5 text-[12px] text-[#a8862c] serif italic">
        翻到第
        <select
          value={page}
          onChange={(e) => onChange(Number(e.target.value))}
          className="appearance-none bg-transparent border-b border-dashed border-[#d9b44a] px-1.5 pb-0.5 text-center not-italic text-[13px] text-[#1f3a68] outline-none focus:border-solid cursor-pointer"
        >
          {Array.from({ length: total }, (_, i) => (
            <option key={i} value={i + 1}>{i + 1}</option>
          ))}
        </select>
        页 <span className="not-italic text-[#c9b27a]">/ {total}</span>
      </label>
    </div>
  )
}

/* ---------- BottomNav（设计稿原样） ---------- */
function BottomNav({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const idx = TABS.indexOf(tab)
  return (
    <div className="fixed left-0 right-0 bottom-0 z-40 flex justify-center px-3" style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 10px)' }}>
      <nav className="relative w-full max-w-md h-16 rounded-[28px] bg-white/85 backdrop-blur-xl border border-[#ecdfbf] shadow-xl shadow-[#1e2a4a]/10 grid grid-cols-5 items-center">
        <span
          className={`absolute top-1/2 -translate-y-1/2 h-11 w-11 rounded-2xl bg-[#f5ead0] transition-all duration-500 ease-[cubic-bezier(.34,1.56,.64,1)] ${tab === 'home' ? 'opacity-0 scale-50' : 'opacity-100 scale-100'}`}
          style={{ left: `calc(${idx * 20 + 10}% - 22px)` }}
        />
        {TABS.map((t) => {
          const active = t === tab
          if (t === 'home')
            return (
              <div key={t} className="flex justify-center">
                <button
                  onClick={() => onChange(t)}
                  aria-label={TAB_NAMES[t]}
                  className={`-mt-7 w-16 h-16 rounded-full flex items-center justify-center text-[#f6e3a8] bg-[#1f3a68] ring-4 ring-[#fdfaf2] shadow-lg transition-all duration-500 ease-[cubic-bezier(.34,1.56,.64,1)] active:scale-90 ${
                    active ? 'scale-105 shadow-[#1f3a68]/40' : 'scale-90 opacity-90'
                  }`}
                >
                  <span className={active ? 'flutter' : ''}>{Icons.home(active)}</span>
                </button>
              </div>
            )
          return (
            <button
              key={t}
              onClick={() => onChange(t)}
              aria-label={TAB_NAMES[t]}
              className={`relative z-10 h-full flex items-center justify-center transition-all duration-300 active:scale-90 ${active ? 'text-[#1e3a7a] -translate-y-0.5' : 'text-[#9aa3b8] hover:text-[#3b63b8]'}`}
            >
              {Icons[t](active)}
            </button>
          )
        })}
      </nav>
    </div>
  )
}

/* ---------- Bucket detail（视觉原样；数据/编辑接真实接口） ---------- */
const Section = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <section className="py-3 border-b border-[#ece3cc]">
    <h4 className="text-[11px] tracking-[0.2em] text-[#a8862c] mb-1.5">{label}</h4>
    {children}
  </section>
)

function MultiSelect({ value, options, editing, onChange, tone }: { value: string[]; options: string[]; editing: boolean; onChange: (v: string[]) => void; tone: 'blue' | 'gold' }) {
  const [open, setOpen] = useState(false)
  const chip = tone === 'blue' ? 'bg-[#e8edf6] text-[#1f3a68]' : 'bg-[#f5ead0] text-[#7a5d14]'
  return (
    <div className="relative inline-block">
      <button
        type="button"
        disabled={!editing}
        onClick={() => setOpen((o) => !o)}
        className={`flex flex-wrap items-center gap-1 rounded-lg ${editing ? 'px-1.5 py-1 -mx-1.5 border border-dashed border-[#d9b44a] cursor-pointer' : 'cursor-default'}`}
      >
        {value.length ? value.map((v) => <span key={v} className={`text-[12px] px-2 py-0.5 rounded-full ${chip}`}>{v}</span>) : <span className="text-xs text-[#9aa3b8]">未选择</span>}
        {editing && <span className="text-[#a8862c] text-xs ml-0.5">▾</span>}
      </button>
      {editing && open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute z-20 mt-1 w-44 rounded-xl bg-white border border-[#e6dcc3] shadow-xl py-1 max-h-64 overflow-y-auto">
            {options.map((o) => {
              const on = value.includes(o)
              return (
                <button key={o} type="button" onClick={() => onChange(on ? value.filter((x) => x !== o) : [...value, o])} className="w-full flex items-center justify-between px-3 py-1.5 text-sm text-[#1f3a68] hover:bg-[#fbf5e4]">
                  {o}
                  <span className={`w-4 h-4 rounded border flex items-center justify-center text-[10px] ${on ? 'bg-[#1f3a68] border-[#1f3a68] text-[#f6e3a8]' : 'border-[#cfc6ad]'}`}>{on ? '✓' : ''}</span>
                </button>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

function RecallForm({ recall, setRecall, busy, onSubmit, field, autoFocus }: {
  recall: string; setRecall: (v: string) => void; busy: string
  onSubmit: () => void; field: string; autoFocus?: boolean
}) {
  return (
    <form
      className="mt-2.5 flex gap-2"
      onSubmit={(e) => { e.preventDefault(); onSubmit() }}
    >
      <div className="relative flex-1">
        <svg viewBox="0 0 24 24" className="absolute left-0 top-1/2 -translate-y-1/2 w-4 h-4 text-[#d9b44a]" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round"><path d="M20 4C12 4 6 10 5 19M20 4c0 7-5 12-12 12M9 15l-5 5" /></svg>
        <input value={recall} onChange={(e) => setRecall(e.target.value)} placeholder="再次看到时，想说点什么…" className={`${field} text-sm pl-6`} disabled={!!busy} autoFocus={autoFocus} />
      </div>
      <button
        disabled={!recall.trim() || !!busy}
        className="shrink-0 serif italic text-[13px] text-[#1f3a68] px-1 pb-0.5 border-b border-[#1f3a68] disabled:text-[#c2b48f] disabled:border-transparent transition-colors"
      >
        留下
      </button>
    </form>
  )
}

function WordBubbles({ words }: { words: Word[] }) {
  if (!words.length) return <p className="text-[13px] text-[#9aa3b8]">（还没有）</p>
  return (
    <div className="space-y-1.5">
      {words.map((l) => (
        <div key={l.word_id} className={`flex ${l.speaker === 'qiaosheng' ? 'justify-end' : 'justify-start'}`}>
          <div className={`max-w-[78%] px-3 py-1.5 text-[14px] leading-relaxed ${l.speaker === 'qiaosheng' ? 'bg-[#1f3a68] text-[#fdf6e0] rounded-2xl rounded-br-md' : 'bg-white/75 text-[#1f3a68] rounded-2xl rounded-bl-md border border-white'}`}>
            {l.text}
            {l.expression_kind === 'verbatim' && <span className="ml-1.5 align-middle text-[10px] px-1 py-px rounded-full bg-[#f5ead0] text-[#7a5d14]">原话</span>}
            {l.expression_kind === 'paraphrase' && <span className="ml-1.5 align-middle text-[10px] px-1 py-px rounded-full bg-[#e8edf6] text-[#1f3a68]">转述</span>}
          </div>
        </div>
      ))}
      <div className="flex justify-between text-[10px] text-[#9aa3b8] px-1 pt-0.5"><span>char</span><span>我</span></div>
    </div>
  )
}

function BucketDetail({ b, allBuckets, onClose, onSaved, onRelation, onSource }: {
  b: Bucket
  allBuckets: Bucket[]
  onClose: () => void
  onSaved: (b: Bucket) => void
  onRelation: (gid: string) => void
  onSource: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(b.original_title)
  const [text, setText] = useState(b.text)
  const [date, setDate] = useState(dateOf(b))
  const [cats, setCats] = useState<string[]>(b.categories || [])
  const [recall, setRecall] = useState('')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  // 删除申请（她 2026-10-13 裁定进桶抽屉；申请=qiaosheng，审批=jiaming）
  const [delOpen, setDelOpen] = useState(false)
  const [delReason, setDelReason] = useState('')
  const [delDone, setDelDone] = useState('')
  const [recallOpen, setRecallOpen] = useState(false)
  const rel = groupOfBucket(b, allBuckets)
  const field = 'w-full bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 px-0.5 py-1 outline-none transition-colors focus:border-solid focus:border-[#1f3a68] placeholder:text-[#c2b48f] placeholder:italic'
  const paper = 'w-full resize-none bg-[#fbf6e8] border-l-2 border-[#d9b44a] rounded-r-md px-3 py-2 outline-none transition-colors focus:bg-[#f8f0da] leading-relaxed'

  const save = async () => {
    setErr(''); setBusy('保存中…')
    try {
      const args: Record<string, unknown> = { memory_id: b.memory_id, expected_version: b.version }
      if (title !== b.original_title) args.original_title = title
      if (text !== b.text) args.text = text
      if (date !== dateOf(b)) args.memory_date = date || null
      if (Object.keys(args).length > 2) await api('memory.update', args, 'ui-upd-' + b.memory_id + '-' + Date.now())
      if (JSON.stringify(cats) !== JSON.stringify(b.categories || [])) {
        if (!cats.length) throw new ApiError('INVALID', '至少选一个分类（清空需走删除）')
        await api('memory.categories.replace', { memory_id: b.memory_id, categories: cats })
      }
      // 重读当前桶（正文/版本/附加面）
      const fresh = await api<Bucket>('memory.get', { memory_id: b.memory_id })
      const d = await api<{ cards: Record<string, any> }>('memory.cards.batch', { memory_ids: [b.memory_id] })
      const c = d.cards[b.memory_id] || {}
      onSaved({ ...fresh, words: c.words || [], recollections: c.recollections || [], relations: c.relations || [], bindings: c.bindings || [] })
      setEditing(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  const submitRecall = async () => {
    if (!recall.trim()) return
    setErr(''); setBusy('正在写下回忆…')
    try {
      // 回忆链（后端规矩）：open 签票 → view.confirm 明确确认 → append
      const opened = await api<any>('memory.open', { memory_id: b.memory_id })
      await api('memory.view.confirm', {
        memory_id: b.memory_id, receipt_id: opened.view_receipt,
        confirm_key: opened.confirm_key })
      await api('memory.recollections.append', {
        memory_id: b.memory_id, receipt_id: opened.view_receipt,
        text: recall.trim() }, 'ui-rec-' + b.memory_id + '-' + Date.now())
      setRecall('')
      const d = await api<{ cards: Record<string, any> }>('memory.cards.batch', { memory_ids: [b.memory_id] })
      const c = d.cards[b.memory_id] || {}
      onSaved({ ...b, recollections: c.recollections || [] })
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  const submitDeletion = async () => {
    if (!delReason.trim()) return
    setErr(''); setBusy('提交删除申请…')
    try {
      const d = await api<{ request_id: string }>('memory.deletion.request', {
        memory_id: b.memory_id, reason: delReason.trim(),
        operation_id: 'ui-del-' + b.memory_id + '-' + Date.now(),
      })
      setDelDone('已提交申请 ' + d.request_id + '（pending，内容未变；审批由char处理）')
      setDelOpen(false); setDelReason('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy('') }
  }

  return (
    <>
      <div className="flex items-center gap-1 px-3 py-2 border-b border-[#ece3cc]">
        {rel && (
          <button onClick={() => onRelation(rel.id)} title={rel.name} className="flex items-center gap-1.5 h-8 px-2.5 rounded-full bg-[#e8edf6] text-[#1f3a68] text-xs active:scale-95 transition">
            <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round"><circle cx="5" cy="12" r="2.2" /><circle cx="12" cy="12" r="2.2" /><circle cx="19" cy="12" r="2.2" /><path d="M7.2 12h2.6M14.2 12h2.6" /></svg>
            {rel.ids.length} 段相连
          </button>
        )}
        {editing ? (
          <button onClick={() => { setEditing(false); setTitle(b.original_title); setText(b.text); setDate(dateOf(b)); setCats(b.categories || []) }} className="ml-auto w-8 h-8 rounded-full text-[#1f3a68] hover:bg-[#f5ead0] text-lg">×</button>
        ) : (
          <>
            <button onClick={() => setEditing(true)} aria-label="编辑" className="ml-auto w-8 h-8 rounded-full flex items-center justify-center transition text-[#1f3a68] hover:bg-[#f5ead0]">
              <svg viewBox="0 0 24 24" className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="m13.5 6.5 4 4" /></svg>
            </button>
            <button onClick={onClose} aria-label="关闭" className="w-8 h-8 rounded-full hover:bg-[#f5ead0] text-[#1f3a68] text-lg">×</button>
          </>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-5 pb-6">
        <div className="pt-4 pb-3 border-b border-[#ece3cc]">
          {editing ? (
            <>
              <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={30} className={`${field} serif text-xl font-bold text-[#1f3a68]`} />
              <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
                <MultiSelect value={cats.map((c) => CAT_LABELS[c] || c)} options={CATEGORIES.map((c) => c.label)} editing onChange={(labels) => setCats(labels.map((l) => CATEGORIES.find((c) => c.label === l)?.key || l))} tone="blue" />
                <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 px-0.5 py-0.5 text-sm text-[#7a5d14] tabular-nums outline-none focus:border-solid" />
              </div>
            </>
          ) : (
            <>
              <h2 className="serif text-2xl font-bold text-[#1f3a68] leading-snug">{b.original_title}</h2>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
                <MultiSelect value={(b.categories || []).map((c) => CAT_LABELS[c] || c)} options={CATEGORIES.map((c) => c.label)} editing={false} onChange={() => {}} tone="blue" />
                <time className="text-sm text-[#a8862c] tabular-nums">{dateOf(b) || '日期未知'}</time>
              </div>
            </>
          )}
        </div>

        {/* 心情：只读（她 2026-10-13 裁定——当时心情仅char同期可写，网页身份动不得） */}
        <Section label="心情">
          {(b.mood?.tags?.length || b.mood?.text)
            ? (<>
              {!!b.mood?.tags?.length && <MultiSelect value={b.mood.tags} options={b.mood.tags} editing={false} onChange={() => {}} tone="gold" />}
              {b.mood?.text && <p className="mt-1.5 text-sm leading-relaxed text-[#5a6480] italic">{b.mood.text}</p>}
            </>)
            : <p className="text-[13px] text-[#9aa3b8]">（未记——当时心情只能由char在同期窗口写下）</p>}
        </Section>

        <Section label="事件">
          {editing ? (
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} className={`${paper} text-[15px] text-[#33405f]`} />
          ) : (
            <p className="text-[15px] leading-loose text-[#33405f] whitespace-pre-wrap">{b.text}</p>
          )}
          <div className="mt-3 rounded-2xl bg-[#1f3a68]/[0.05] border border-white/70 p-3">
            <p className="text-[11px] text-[#a8862c] mb-1">我们的话</p>
            <WordBubbles words={b.words || []} />
          </div>
        </Section>

        {/* 回忆：写了才有格式（她 2026-10-13 裁定）——没写过时只留一行
            极简入口，不渲染回忆区本体 */}
        {(b.recollections || []).length > 0 ? (
          <Section label="回忆">
            <div className="space-y-2">
              {(b.recollections || []).map((r, i) => (
                <div key={r.recollection_id || i} className="pl-3 border-l-2 border-[#d9b44a]">
                  <p className="text-sm text-[#33405f] leading-relaxed">{r.text}</p>
                  <p className="text-[11px] text-[#a8862c] mt-0.5">{(r.created_at || '').slice(0, 10)} 再读时 · {(r.author_principal || r.author) === 'jiaming' ? 'char' : '宿主用户'}</p>
                </div>
              ))}
            </div>
            <RecallForm recall={recall} setRecall={setRecall} busy={busy} onSubmit={submitRecall} field={field} />
          </Section>
        ) : (
          recallOpen && (
            <Section label="回忆">
              <RecallForm recall={recall} setRecall={setRecall} busy={busy} onSubmit={submitRecall} field={field} autoFocus />
            </Section>
          )
        )}

        {busy && <p className="mt-3 text-[12px] text-[#a8862c]">{busy}</p>}
        {err && <p className="mt-3 text-[12px] text-[#c0392b]">{err}</p>}

        {editing && (
          <button onClick={save} className="mt-4 w-full py-2.5 rounded-full bg-[#1f3a68] text-[#f6e3a8] serif italic tracking-wider active:scale-95 transition">
            保存修订
          </button>
        )}

        {/* 删除申请（qiaosheng；审批权在char） */}
        {session.principal === 'qiaosheng' && (
          <div className="mt-3">
            {delDone
              ? <p className="text-[11px] text-[#a8622c] leading-relaxed">{delDone}</p>
              : delOpen ? (
                <div className="rounded-xl border border-[#e0c8b8] bg-[#faf3ec] px-3 py-2.5">
                  <p className="text-[11px] text-[#a8622c] leading-relaxed">申请物理删除「{b.original_title}」？理由必填；提交后内容不变，char批准才执行。</p>
                  <div className="mt-1.5 flex gap-2">
                    <input value={delReason} onChange={(e) => setDelReason(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && submitDeletion()}
                      placeholder="删除理由（必填）" className="flex-1 bg-transparent border-0 border-b border-dashed border-[#c98b5f]/70 py-1 text-[13px] text-[#5c3a24] outline-none focus:border-solid placeholder:text-[#d3b8a3] placeholder:italic" />
                    <button onClick={submitDeletion} disabled={!delReason.trim() || !!busy}
                      className="shrink-0 text-[12.5px] text-[#a83c1c] px-1 pb-0.5 border-b border-[#a83c1c]/70 disabled:text-[#d3b8a3] disabled:border-transparent">提交申请</button>
                    <button onClick={() => { setDelOpen(false); setDelReason('') }}
                      className="shrink-0 text-[12px] text-[#8a7a52]">算了</button>
                  </div>
                </div>
              ) : (
                <button onClick={() => setDelOpen(true)} className="text-[11px] text-[#c9a08a] hover:text-[#a83c1c] border-b border-transparent hover:border-[#c9a08a]">
                  申请删除这个记忆…
                </button>
              )}
          </div>
        )}

        {!(b.recollections || []).length && !recallOpen && (
          <button onClick={() => setRecallOpen(true)} className="mt-3 mx-auto block text-[11px] text-[#c2b48f] hover:text-[#a8862c]">
            ✎ 留下回忆
          </button>
        )}

        <button onClick={onSource} className="group mt-6 w-full flex items-center gap-3 text-[#1f3a68]">
          <span className="flex-1 h-px bg-[#d9b44a]/50" />
          <span className="flex items-center gap-1.5 serif italic text-[14px] tracking-wider transition-transform duration-300 group-hover:-translate-y-0.5">
            <Butterfly className="w-5 h-4 transition-transform duration-500 group-hover:rotate-12" />
            查看原文
            <svg viewBox="0 0 24 24" className="w-3.5 h-3.5 text-[#a8862c] transition-transform duration-300 group-hover:translate-x-1" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round"><path d="M5 12h13M13 7l5 5-5 5" /></svg>
          </span>
          <span className="flex-1 h-px bg-[#d9b44a]/50" />
        </button>
      </div>
    </>
  )
}

/* ---------- 关系页（时间线视觉保留设计稿；数据=真实关系边分组
   + episode 区块——她 2026-10-13 裁定：正在绑定关系的事在这边浮现） ---------- */
function RelationPage({ focus, setFocus, buckets, byId, onOpen }: { focus: string | null; setFocus: (id: string | null) => void; buckets: Bucket[]; byId: (id: string) => Bucket | undefined; onOpen: (id: string) => void }) {
  const groups = useMemo(() => buildGroups(buckets), [buckets])
  const rels = focus ? groups.filter((r) => r.id === focus) : groups
  const [episodes, setEpisodes] = useState<any[] | null>(null)
  useEffect(() => {
    if (focus) return
    ;(async () => {
      try {
        const d = await api<{ episodes: any[] }>('episode.list', {
          scope_id: 'primary', states: ['OPEN', 'QUIESCENT'], limit: 20 })
        setEpisodes(d.episodes || [])
      } catch { setEpisodes([]) }
    })()
  }, [focus, buckets])
  const episodeBuckets = (ep: any): string[] => {
    const ids = new Set<string>()
    collectMemoryIds(ep.source_selections, ids)
    collectMemoryIds(ep.terminal_source_ref, ids)
    collectMemoryIds(ep.decision_source_refs, ids)
    return [...ids].filter((id) => buckets.some((b) => b.memory_id === id))
  }
  if (!groups.length && !episodes?.length)
    return (
      <div className="flex flex-col items-center justify-center py-24 text-[#8a7a52]">
        <Butterfly className="w-12 h-10 flutter opacity-70" />
        <p className="mt-3 serif italic text-sm">还没有相连的记忆</p>
        <p className="mt-1 text-[11px] text-[#b5ad98]">桶与桶之间的关联由char经 MCP 建立（memory.relations.link）</p>
      </div>
    )
  return (
    <div className="space-y-6">
      {focus && (
        <button onClick={() => setFocus(null)} className="text-sm text-[#1f3a68]">‹ 全部关系</button>
      )}
      {!!episodes?.length && !focus && (
        <section>
          <h3 className="serif font-bold text-lg text-[#1f3a68] mb-2">正在发生的事</h3>
          <div className="space-y-2">
            {episodes.map((ep) => {
              const mem = episodeBuckets(ep)
              const st = ep.state === 'OPEN' ? 'bg-[#1f3a68] text-[#f6e3a8]' : 'bg-[#e8edf6] text-[#1f3a68]'
              return (
                <div key={ep.episode_id} className="flex items-center gap-2 py-1 border-b border-[#ece3cc]">
                  <span className={`shrink-0 text-[11px] px-2 py-0.5 rounded-full ${st}`}>{ep.state === 'OPEN' ? '进行中' : '静置'}</span>
                  <span className="serif font-bold text-[14px] text-[#1f3a68] truncate flex-1">{ep.label || ep.episode_id}</span>
                  <span className="shrink-0 text-[11px] text-[#a8862c] tabular-nums">{(ep.updated_at || '').slice(0, 10)}</span>
                  {mem.map((id) => {
                    const b = byId(id)
                    if (!b) return null
                    return (
                      <button key={id} onClick={() => onOpen(id)}
                        className="shrink-0 flex items-center gap-1 pl-2 pr-2.5 py-1 rounded-full bg-[#f5ead0] text-[11.5px] text-[#1f3a68] active:scale-95 transition">
                        <span className="w-1.5 h-1.5 rounded-full bg-[#d9b44a]" />
                        <span className="max-w-[8rem] truncate">{b.original_title}</span>
                      </button>
                    )
                  })}
                </div>
              )
            })}
          </div>
        </section>
      )}
      {rels.map((r) => (
        <section key={r.id}>
          <h3 className="serif font-bold text-lg text-[#1f3a68] mb-2">{r.name}</h3>
          <ol className="relative ml-2 border-l-2 border-[#e6dcc3]">
            {r.ids.map((id, k) => {
              const b = byId(id)
              if (!b) return null
              return (
                <li key={id} className="relative pl-5 pb-4">
                  <span className="absolute -left-[7px] top-1.5 w-3 h-3 rounded-full bg-[#d9b44a] ring-4 ring-[#fdfaf2]" />
                  {k > 0 && r.labels[id] && <p className="text-[11px] text-[#7d8aa8] mb-1">↳ {r.labels[id]}</p>}
                  <button onClick={() => onOpen(id)} className="text-left w-full">
                    <div className="flex items-baseline gap-2">
                      <span className="serif font-bold text-[#1f3a68]">{b.original_title}</span>
                      <time className="text-[11.5px] text-[#a8862c] tabular-nums">{dateOf(b)}</time>
                    </div>
                    <p className="text-[13px] text-[#4a5675] line-clamp-2 mt-0.5">{b.text}</p>
                  </button>
                </li>
              )
            })}
          </ol>
        </section>
      ))}
    </div>
  )
}

/* ---------- 原文页（新增真实层：会话列表主线 + 搜索 + 反查跳桶） ---------- */
/* thinking 折叠块（取自她的聊天页组件：默认隐藏，点开展开竖线步骤） */
function Thinking({ steps }: { steps: { kind: 'think' | 'tool'; text: string }[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mb-2 -ml-[6px]">
      <button onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex min-h-9 items-center gap-1.5 text-[13px] text-[#8a8780] hover:text-[#3d3929] transition-colors">
        <svg viewBox="0 0 24 24" className="w-[14px] h-[14px]" fill="currentColor"><path d="M12 2l1.9 5.7L19.6 9.6l-5.7 1.9L12 17.2l-1.9-5.7L4.4 9.6l5.7-1.9L12 2zm7 12l.95 2.85L22.8 17.8l-2.85.95L19 21.6l-.95-2.85-2.85-.95 2.85-.95L19 14z" /></svg>
        <span>thinking</span>
        <svg viewBox="0 0 24 24" className={`w-[14px] h-[14px] transition-transform ${open ? '-rotate-90' : ''}`} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M15 5 8 12l7 7" /></svg>
      </button>
      {open && (
        <div className="mt-1.5 ml-[6px]">
          {steps.map((s, i) => (
            <div key={i} className="relative flex gap-3 pb-3">
              {i < steps.length - 1 && <span className="absolute left-[5px] top-4 bottom-0 w-px bg-[#dcd8cc]" />}
              <span className="mt-[5px] flex h-[11px] w-[11px] shrink-0 items-center justify-center rounded-full border border-[#c9c4b6] bg-[#fdfaf2]">
                {s.kind === 'tool' && <span className="h-[5px] w-[5px] rounded-full bg-[#c96442]" />}
              </span>
              <p className={s.kind === 'tool'
                ? 'flex items-center gap-1 font-mono text-[12.5px] leading-relaxed text-[#8a8780]'
                : 'serif text-[13.5px] leading-relaxed text-[#8a8780]'}>
                {s.kind === 'tool' && (
                  <svg viewBox="0 0 24 24" className="w-3 h-3 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>
                )}
                {s.text}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

type ConvItem = {
  id: string; title: string; summary?: string | null
  message_count: number; first_message_at?: string; last_message_at?: string
  provider_conversation_id: string
}
type SrcMsg = {
  provider_message_id: string; normalized_sender: string
  speaker_display?: string; created_at?: string; text?: string | null
  has_thinking?: boolean; thinking_text?: string | null
  thinking_summaries?: string[] | null
  has_tool_content?: boolean
  sequence: number
}
type ConvBinding = { binding_id: string; memory_id: string; start_seq: number; end_seq: number }

function SourcePage({ jump, clearJump, buckets, onOpen, refreshBuckets }: {
  jump: { conversation_id: string; around_seq?: number } | null
  clearJump: () => void
  buckets: Bucket[]
  onOpen: (id: string) => void
  refreshBuckets: () => void
}) {
  const [convs, setConvs] = useState<ConvItem[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  // 搜索（她 2026-10-13：原文页加一个搜索——source.search 专项检索）
  const [sq, setSq] = useState('')
  const [sSender, setSSender] = useState('')
  const [sFrom, setSFrom] = useState('')
  const [sTo, setSTo] = useState('')
  const [sHits, setSHits] = useState<any[] | null>(null)
  // 会话视图
  const [view, setView] = useState<{ conv: ConvItem; msgs: SrcMsg[]; more: boolean; cursor?: string } | null>(null)
  const [bindings, setBindings] = useState<ConvBinding[]>([])
  const [importing, setImporting] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const loadConvs = async (offset = 0) => {
    setLoading(true); setErr('')
    try {
      const d = await api<{ conversations: ConvItem[]; has_more: boolean; total: number }>(
        'source.conversations.list', { limit: 50, offset, titled: true, order: 'first_desc' })
      setConvs(offset ? (prev: ConvItem[]) => [...prev, ...d.conversations] : d.conversations)
      setTotal(d.total ?? 0)
      setHasMore(d.has_more)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setLoading(false) }
  }
  useEffect(() => { loadConvs(0) }, [])

  const openConv = async (convId: string, aroundSeq?: number) => {
    setErr(''); setLoading(true)
    try {
      const d = await api<{ conversation: ConvItem; messages: SrcMsg[]; has_more: boolean; next_after_cursor?: string }>(
        'source.conversation.get', { conversation_id: convId, limit: 100, ...(aroundSeq != null ? { around_seq: aroundSeq } : {}) })
      setView({ conv: d.conversation, msgs: d.messages, more: d.has_more, cursor: d.next_after_cursor })
      // 反查（§6.1 会话方向）：这个会话被哪些桶绑定
      try {
        const bd = await api<{ bindings: ConvBinding[] }>('source.bindings.of_conversation', { conversation_id: convId })
        setBindings(bd.bindings || [])
      } catch { setBindings([]) }
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setLoading(false) }
  }
  useEffect(() => {
    if (jump) { openConv(jump.conversation_id, jump.around_seq); clearJump() }
  }, [jump])

  const loadMoreMsgs = async () => {
    if (!view?.cursor) return
    try {
      const d = await api<{ messages: SrcMsg[]; has_more: boolean; next_after_cursor?: string }>(
        'source.conversation.get', { conversation_id: view.conv.id, after_cursor: view.cursor, limit: 100 })
      setView({ ...view, msgs: [...view.msgs, ...d.messages], more: d.has_more, cursor: d.next_after_cursor })
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }

  const doSearch = async () => {
    setErr(''); setLoading(true)
    try {
      const args: Record<string, unknown> = { limit: 30 }
      if (sq.trim()) args.query = sq.trim()
      if (sSender) args.senders = sSender
      if (sFrom) args.date_from = sFrom
      if (sTo) args.date_to = sTo
      const d = await api<{ hits: any[] }>('source.search', args)
      setSHits(d.hits)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) } finally { setLoading(false) }
  }

  const doImport = async (f: File) => {
    setImporting(`上传 ${f.name}（${(f.size / 1048576).toFixed(1)} MB）…`)
    try {
      const path = await uploadSource(f)
      const d = await api<any>('source.import', { path, filename: f.name }, 'ui-srcimp-' + Date.now())
      if (d.status === 'failed') {
        setImporting(`导入失败：${d.error || '未知错误'}`)
        return
      }
      const s = d.stats || {}
      setImporting(`导入完成：${s.conversations_total ?? '?'} 会话 / ${s.messages_new ?? '?'} 新消息` +
        (s.verify_problems?.length ? ` · ⚠ 校验问题 ${s.verify_problems.length}` : ' · 校验通过'))
      loadConvs(0)
      refreshBuckets()
    } catch (e) { setImporting('导入失败：' + (e instanceof Error ? e.message : e)) }
  }

  const bindingAt = (seq: number) => bindings.filter((b) => b.start_seq <= seq && seq <= b.end_seq)
  const startBinding = (seq: number) => bindings.find((b) => b.start_seq === seq)
  const bucketTitle = (mid: string) => buckets.find((b) => b.memory_id === mid)?.original_title || mid
  const line = 'bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 outline-none focus:border-solid focus:border-[#1f3a68] transition-colors'

  /* --- 会话视图（消息流按她的聊天页组件样式：thinking 折叠在上方、
     时间戳在气泡下方、左char右我、75% 宽）--- */
  if (view) {
    return (
      <div>
        <div className="flex items-center justify-between mb-3">
          <button onClick={() => { setView(null); setBindings([]) }} className="text-sm text-[#1f3a68]">‹ 全部会话</button>
          <span className="text-[11px] text-[#a8862c]">{view.conv.message_count} 条 · {fmtTime(view.conv.first_message_at)} → {fmtTime(view.conv.last_message_at)}</span>
        </div>
        <h3 className="serif font-bold text-lg text-[#1f3a68]">{view.conv.title}</h3>
        {bindings.length > 0 && (
          <p className="mt-1 text-[11.5px] text-[#7a5d14]">
            这段会话被 {[...new Set(bindings.map((b) => b.memory_id))].length} 个记忆桶绑定 · 金色边条的消息段可点击跳桶
          </p>
        )}
        <div className="mt-4">
          {view.msgs.map((m) => {
            const sb = startBinding(m.sequence)
            const inRange = bindingAt(m.sequence).length > 0
            const isHuman = m.normalized_sender === 'human'
            const isMain = isHuman || m.normalized_sender === 'assistant'
            const time = (fmtTime(m.created_at) || '').slice(5, 16)
            const steps: { kind: 'think' | 'tool'; text: string }[] = []
            if (m.thinking_text) steps.push({ kind: 'think', text: m.thinking_text })
            for (const s of m.thinking_summaries || []) steps.push({ kind: 'think', text: `官方思考摘要：${s}` })
            if (m.has_tool_content) steps.push({ kind: 'tool', text: '工具调用（证据面）' })
            return (
              <div key={m.provider_message_id} className={`mb-7 ${inRange ? 'border-l-2 border-[#d9b44a] pl-3' : ''}`}>
                {sb && (
                  <button
                    onClick={() => onOpen(sb.memory_id)}
                    className="flex items-center gap-1.5 mb-2 px-2 py-1 rounded-full bg-[#f5ead0] text-[#1f3a68] text-[11.5px] active:scale-95 transition"
                  >
                    <span className="w-1.5 h-1.5 rounded-full bg-[#d9b44a]" />
                    已收入记忆《{bucketTitle(sb.memory_id)}》↗
                  </button>
                )}
                {isMain ? (
                  <div className={isHuman
                    ? 'ml-auto flex w-[78%] flex-col items-end pr-[1em]'
                    : 'flex w-[78%] flex-col items-start pl-[1em]'}>
                    {/* thinking：上方折叠，点开才显示（她 2026-10-13 要求） */}
                    {!isHuman && steps.length > 0 && <Thinking steps={steps} />}
                    <div className={isHuman
                      ? 'whitespace-pre-wrap break-words text-[15px] leading-relaxed text-[#1f1e1d]'
                      : 'serif whitespace-pre-wrap break-words text-[15.5px] leading-relaxed text-[#1f1e1d]'}>
                      {m.text || '（无用户可见正文）'}
                    </div>
                    <div className={`mt-1 flex items-center ${isHuman ? 'pr-1.5' : 'pl-1.5'}`}>
                      <span className="text-[11px] tabular-nums text-[#a3a099]">{time}</span>
                    </div>
                  </div>
                ) : (
                  <p className="text-[11px] text-[#9aa3b8] text-center py-0.5">· {m.speaker_display || m.normalized_sender} ·（证据面，不入正文）·</p>
                )}
              </div>
            )
          })}
        </div>
        {view.more && (
          <button onClick={loadMoreMsgs} className="mt-2 mx-auto block text-[12px] serif italic text-[#a8862c] border-b border-[#a8862c]/50">
            加载更多消息 ↓
          </button>
        )}
        {loading && <p className="mt-3 text-center text-[12px] text-[#a8862c]">加载中…</p>}
        {err && <p className="mt-3 text-[12px] text-[#c0392b]">{err}</p>}
      </div>
    )
  }

  /* --- 搜索结果 --- */
  if (sHits) {
    return (
      <div>
        <div className="flex items-center justify-between mb-2">
          <button onClick={() => setSHits(null)} className="text-sm text-[#1f3a68]">‹ 返回会话列表</button>
          <span className="text-[11px] text-[#a8862c]">原文命中 {sHits.length} 条 · 专项检索，不属于记忆召回</span>
        </div>
        <div className="border-t border-[#e6dcc3]">
          {sHits.map((h, i) => (
            <button key={i} onClick={() => openConv(h.provider_conversation_id, h.sequence)}
              className="w-full text-left py-2.5 border-b border-[#e6dcc3] hover:bg-[#f5ead0]/50 transition-colors">
              <div className="flex items-baseline gap-2">
                <span className="text-[11px] text-[#9aa3b8] tabular-nums shrink-0">{fmtTime(h.created_at)}</span>
                <span className="text-[11.5px] text-[#a8862c] shrink-0">{h.speaker_display || h.normalized_sender}</span>
                <span className="text-[12px] text-[#1f3a68] truncate flex-1">{h.conversation_title || ''}</span>
              </div>
              <p className="mt-0.5 text-[13px] leading-relaxed text-[#4a5675] line-clamp-2">{h.excerpt || h.text || ''}</p>
            </button>
          ))}
          {!sHits.length && <p className="py-16 text-center serif italic text-sm text-[#a8862c]">原文无命中（默认只查双方正文）</p>}
        </div>
      </div>
    )
  }

  /* --- 会话列表 --- */
  return (
    <div>
      <div className="flex items-center gap-2 pb-2 border-b border-[#ece3cc]">
        <h4 className="shrink-0 w-9 text-[11px] tracking-[0.15em] text-[#a8862c]">搜原文</h4>
        <input value={sq} onChange={(e) => setSq(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && doSearch()}
          placeholder="全文关键词（只查双方正文）…" className={`${line} flex-1 py-1.5 text-[14px] text-[#1f3a68] placeholder:text-[#c2b48f] placeholder:italic`} />
        <select value={sSender} onChange={(e) => setSSender(e.target.value)}
          className="shrink-0 text-[12px] text-[#7a5d14] bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 outline-none">
          <option value="">双方</option>
          <option value="human">只我的</option>
          <option value="assistant">只他的</option>
        </select>
        <input type="date" value={sFrom} onChange={(e) => setSFrom(e.target.value)} className={`${line} w-[7.5rem] py-1 text-[12px] text-[#7a5d14] tabular-nums`} />
        <input type="date" value={sTo} onChange={(e) => setSTo(e.target.value)} className={`${line} w-[7.5rem] py-1 text-[12px] text-[#7a5d14] tabular-nums`} />
        <button onClick={doSearch} className="shrink-0 serif italic text-[13px] text-[#1f3a68] px-1 border-b border-[#1f3a68]">搜索</button>
      </div>

      <div className="flex items-baseline mt-4 mb-1">
        <h3 className="serif font-bold text-lg text-[#1f3a68] flex-1">原文会话（最新在前）</h3>
        <button onClick={() => fileRef.current?.click()} className="text-[12px] text-[#a8862c] border-b border-[#a8862c]/50">
          {importing ? '导入…' : '导入 conversations.json / zip / md'}
        </button>
        <input ref={fileRef} type="file" accept=".json,.zip,.md,.markdown" className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) doImport(f); e.target.value = '' }} />
      </div>
      {importing && <p className="text-[11.5px] text-[#7a5d14]">{importing}</p>}

      <div className="border-t border-[#e6dcc3]">
        {convs.map((c) => (
          <button key={c.id} onClick={() => openConv(c.id)}
            className="w-full text-left py-2.5 border-b border-[#e6dcc3] hover:bg-[#f5ead0]/50 transition-colors">
            <div className="flex items-baseline gap-2">
              <span className="serif font-bold text-[14px] text-[#1f3a68] truncate flex-1">{c.title || '（未命名会话）'}</span>
              <span className="text-[11px] text-[#a8862c] tabular-nums shrink-0">{(fmtTime(c.first_message_at) || '').slice(0, 10)}</span>
              <span className="text-[11px] text-[#9aa3b8] shrink-0">{c.message_count} 条</span>
            </div>
            {c.summary && <p className="mt-0.5 text-[12px] text-[#7d8aa8] line-clamp-1">{c.summary.replace(/\s+/g, ' ')}</p>}
          </button>
        ))}
        {!convs.length && !loading && (
          <div className="flex flex-col items-center py-16 text-[#8a7a52]">
            <Butterfly className="w-12 h-10 flutter opacity-70" />
            <p className="mt-3 serif italic text-sm">还没有原文</p>
            <p className="mt-1 text-[11px] text-[#b5ad98]">导入 Claude 官方导出的 conversations.json 开始</p>
          </div>
        )}
        {hasMore && (
          <button onClick={() => loadConvs(convs.length)} className="w-full py-2.5 text-center text-[12px] text-[#a8862c] serif italic border-b border-[#e6dcc3]">
            加载更多（已显示 {convs.length}{total ? ` / ${total}` : ''}）
          </button>
        )}
      </div>
      {loading && <p className="py-3 text-center text-[12px] text-[#a8862c]">加载中…</p>}
      {err && <p className="mt-2 text-[12px] text-[#c0392b]">{err}</p>}
    </div>
  )
}

/* ---------- 搜索页（视觉原样；引擎=memory.recall 真实语义） ---------- */
type Mode = 'any' | 'all'
type Query = { from: string; to: string; cats: string[]; catMode: Mode; moods: string[]; moodMode: Mode; kw: string; kwMode: Mode }
const EMPTY_Q: Query = { from: '', to: '', cats: [], catMode: 'any', moods: [], moodMode: 'any', kw: '', kwMode: 'any' }
const terms = (kw: string) => kw.trim().split(/\s+/).filter(Boolean)

async function runRealQuery(q: Query): Promise<{ ids: string[]; more: boolean }> {
  // D04：同字段 any/all=category_match/mood_match；跨维度交集由服务端池过滤
  const filters: Record<string, unknown> = {}
  if (q.cats.length) { filters.categories = q.cats; filters.category_match = q.catMode }
  if (q.moods.length) { filters.mood_tags = q.moods; filters.mood_match = q.moodMode }
  if (q.from || q.to) filters.event_date = { from: q.from || null, to: q.to || null }
  const ts = terms(q.kw)
  const call = (query: string) => api<{ hits: { memory_id: string }[]; next_cursor?: any }>(
    'memory.recall', { query, filters, limit: 100 })
  if (!ts.length) {
    const d = await call('')
    return { ids: d.hits.map((h) => h.memory_id), more: !!d.next_cursor }
  }
  // 关键词语义（后端=整串短语引擎）：any=逐词真实检索取并集；all=逐词取交集
  const per = await Promise.all(ts.map((t) => call(t)))
  const sets = per.map((d) => d.hits.map((h) => h.memory_id))
  let ids: string[]
  if (ts.length === 1) ids = sets[0]
  else if (q.kwMode === 'any') {
    const seen = new Set<string>(); ids = []
    for (const s of sets) for (const id of s) if (!seen.has(id)) { seen.add(id); ids.push(id) }
  } else {
    const [first, ...rest] = sets
    ids = first.filter((id) => rest.every((s) => s.includes(id)))
  }
  return { ids, more: per.some((d) => !!d.next_cursor) }
}

function Highlight({ text, ts }: { text: string; ts: string[] }) {
  if (!ts.length) return <>{text}</>
  const re = new RegExp(`(${ts.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi')
  return <>{String(text).split(re).map((part, i) => (i % 2 ? <mark key={i} className="bg-[#f6e3a8] text-[#1f3a68] rounded-sm px-0.5">{part}</mark> : part))}</>
}

function ModeToggle({ mode, onChange }: { mode: Mode; onChange: (m: Mode) => void }) {
  return (
    <span className="shrink-0 ml-auto flex items-center gap-1.5 text-[11px] serif italic">
      {(['any', 'all'] as Mode[]).map((m) => (
        <button key={m} onClick={() => onChange(m)} className={`pb-0.5 border-b transition-colors ${mode === m ? 'text-[#1f3a68] border-[#1f3a68]' : 'text-[#b5a57c] border-transparent'}`}>
          {m}
        </button>
      ))}
    </span>
  )
}

const Row = ({ label, children, toggleEl }: { label: string; children: React.ReactNode; toggleEl?: React.ReactNode }) => (
  <div className="flex items-center gap-2 py-1.5 border-b border-[#ece3cc]">
    <h4 className="shrink-0 w-7 text-[11px] text-[#a8862c]">{label}</h4>
    <div className="flex-1 min-w-0 overflow-x-auto no-scrollbar [mask-image:linear-gradient(to_right,#000_92%,transparent)]">{children}</div>
    {toggleEl}
  </div>
)

function SearchPage({ q, setQ, buckets, byId, onOpen, lastId, moods }: {
  q: Query; setQ: (q: Query) => void
  buckets: Bucket[]; byId: (id: string) => Bucket | undefined
  onOpen: (id: string) => void; lastId: string | null; moods: string[]
}) {
  const [result, setResult] = useState<{ ids: string[]; more: boolean } | null>(null)
  const [running, setRunning] = useState(false)
  const [err, setErr] = useState('')
  const ts = terms(q.kw)
  const active = !!(q.from || q.to || q.cats.length || q.moods.length || ts.length)
  const toggle = (arr: string[], v: string) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v])
  const line = 'bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 outline-none focus:border-solid focus:border-[#1f3a68] transition-colors'
  const chip = (on: boolean, tone: 'blue' | 'gold') =>
    `shrink-0 px-2 py-[3px] rounded-full text-[12.5px] whitespace-nowrap transition-all duration-200 ${on ? (tone === 'blue' ? 'bg-[#1f3a68] text-[#f6e3a8]' : 'bg-[#d9b44a] text-[#1f3a68]') : 'text-[#4a5675] hover:bg-[#f5ead0]'}`

  // 条件一变即查（真实引擎；350ms 防抖）
  const runRef = useRef(0)
  const run = async () => {
    const my = ++runRef.current
    setRunning(true); setErr('')
    try {
      const r = await runRealQuery(q)
      if (my === runRef.current) setResult(r)
    } catch (e) {
      if (my === runRef.current) { setResult({ ids: [], more: false }); setErr(e instanceof Error ? e.message : String(e)) }
    } finally { if (my === runRef.current) setRunning(false) }
  }
  useEffect(() => {
    if (!active) { setResult(null); return }
    const t = setTimeout(run, 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])

  const res = useMemo(() => {
    if (!result) return []
    return result.ids.map((id) => byId(id)).filter(Boolean) as Bucket[]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, buckets])
  const shown = res.slice(0, 60)

  return (
    <div className="lg:grid lg:grid-cols-[340px_1fr] lg:gap-8">
      <div className="lg:sticky lg:top-24 lg:self-start">
        <div className="relative">
          <svg viewBox="0 0 24 24" className="absolute left-0.5 top-1/2 -translate-y-1/2 w-5 h-5 text-[#1f3a68]" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round"><circle cx="10.5" cy="10.5" r="6" /><path d="M15 15l5 5" /></svg>
          <input
            value={q.kw}
            onChange={(e) => setQ({ ...q, kw: e.target.value })}
            placeholder="事件关键词，空格分隔多个…"
            className={`${line} w-full pl-8 pr-2 py-2 serif text-[16px] text-[#1f3a68] placeholder:text-[#c2b48f] placeholder:italic`}
          />
        </div>
        {ts.length > 1 && (
          <div className="flex pt-1.5">
            <span className="text-[11px] text-[#9aa3b8]">{ts.length} 个关键词</span>
            <ModeToggle mode={q.kwMode} onChange={(m) => setQ({ ...q, kwMode: m })} />
          </div>
        )}

        <Row label="日期">
          <div className="flex items-center gap-2 text-[13px] text-[#7a5d14] w-max">
            <input type="date" value={q.from} onChange={(e) => setQ({ ...q, from: e.target.value })} className={`${line} w-[8.5rem] py-0 tabular-nums`} />
            <span className="text-[#c2b48f] serif italic">至</span>
            <input type="date" value={q.to} onChange={(e) => setQ({ ...q, to: e.target.value })} className={`${line} w-[8.5rem] py-0 tabular-nums`} />
          </div>
        </Row>
        <Row label="分类" toggleEl={q.cats.length > 1 && <ModeToggle mode={q.catMode} onChange={(m) => setQ({ ...q, catMode: m })} />}>
          <div className="flex gap-0.5 w-max pr-4">
            {CATEGORIES.map((c) => (
              <button key={c.key} onClick={() => setQ({ ...q, cats: toggle(q.cats, c.key) })} className={chip(q.cats.includes(c.key), 'blue')}>{c.label}</button>
            ))}
          </div>
        </Row>
        <Row label="心情" toggleEl={q.moods.length > 1 && <ModeToggle mode={q.moodMode} onChange={(m) => setQ({ ...q, moodMode: m })} />}>
          <div className="flex gap-0.5 w-max pr-4">
            {moods.map((m) => (
              <button key={m} onClick={() => setQ({ ...q, moods: toggle(q.moods, m) })} className={chip(q.moods.includes(m), 'gold')}>{m}</button>
            ))}
          </div>
        </Row>
        <p className="text-[10.5px] text-[#b5ad98] leading-relaxed mt-2">
          关键词走真实检索引擎（标题/事件/话语按桶所处阶段可见；心情文字永不参与）；
          分类/心情多选 any/all、不同维度取交集——与后端口径一致
        </p>
      </div>

      <div className="mt-4 lg:mt-0">
        <div className="flex items-baseline mb-1">
          <span className="serif text-[#1f3a68]">
            {active && !running && <>找到 <b className="text-lg">{res.length}</b> 段记忆</>}
            {running && <span className="text-[13px] italic text-[#a8862c]">检索中…</span>}
          </span>
          {active && (
            <button onClick={() => setQ(EMPTY_Q)} className="ml-auto serif italic text-[12px] text-[#a8862c] border-b border-[#a8862c]/60">清空条件</button>
          )}
        </div>
        {!active && (
          <div className="flex flex-col items-center py-20 text-center">
            <Butterfly className="w-12 h-10 flutter opacity-80" />
            <p className="mt-3 serif italic text-sm text-[#a8862c]">输入关键词，或选个日期、分类、心情</p>
            <p className="mt-1 text-[11px] text-[#b5ad98]">同项多选任一符合，可切换 all；不同项同时满足</p>
          </div>
        )}
        {active && (
          <div className="border-t border-[#e6dcc3]">
            {shown.map((b) => (
              <article
                key={b.memory_id}
                onClick={() => onOpen(b.memory_id)}
                className={`relative cursor-pointer py-3 pl-3 pr-1 border-b border-[#e6dcc3] transition-colors hover:bg-[#f5ead0]/50 ${lastId === b.memory_id ? 'bg-[#f5ead0]/60' : ''}`}
              >
                <span className={`absolute left-0 top-3 bottom-3 w-[3px] rounded-full ${lastId === b.memory_id ? 'bg-[#d9b44a]' : 'bg-transparent'}`} />
                <div className="flex items-baseline gap-2">
                  <h3 className="serif font-bold text-[15px] text-[#1f3a68] flex-1 truncate"><Highlight text={b.original_title} ts={ts} /></h3>
                  <time className="shrink-0 text-[11.5px] text-[#a8862c] tabular-nums">{dateOf(b) || '日期未知'}</time>
                </div>
                <p className="mt-1 text-[13px] leading-relaxed text-[#4a5675] line-clamp-3"><Highlight text={b.text} ts={ts} /></p>
                <span className="mt-1 inline-block text-[11px] text-[#7d8aa8]">
                  {(b.categories || []).map((c) => `#${CAT_LABELS[c] || c}`).join(' ')} <span className="text-[#a8862c]">· {(b.mood?.tags || []).join(' / ')}</span>
                </span>
              </article>
            ))}
            {!running && res.length === 0 && <p className="py-16 text-center serif italic text-sm text-[#a8862c]">没有符合的记忆，换个条件试试</p>}
            {res.length > 60 && <p className="py-3 text-center text-[11px] text-[#9aa3b8]">仅显示前 60 条，可继续缩小条件</p>}
            {res.length <= 60 && result?.more && <p className="py-3 text-center text-[11px] text-[#9aa3b8]">后端还有更多页未列出（当前页上限 100/次）</p>}
          </div>
        )}
        {err && <p className="mt-2 text-[12px] text-[#c0392b]">{err}</p>}
      </div>
    </div>
  )
}

/* ---------- I 抽屉（视觉原样；内容=i.items.list 真实条目） ---------- */
function SelfDrawer({ open, onClose, buckets, onOpen }: { open: boolean; onClose: () => void; buckets: Bucket[]; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<{ item_id: string; content: string; updated_at?: string; ids: string[] }[]>([])
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    if (!open || items.length || loading) return
    setLoading(true)
    ;(async () => {
      try {
        const d = await api<{ items: any[] }>('i.items.list', {})
        const out = []
        for (const it of d.items || []) {
          let ids: string[] = []
          try {
            const r = await api<any>('relations.list', { item_id: it.item_id, revision: it.revision })
            const s = new Set<string>()
            collectMemoryIds(r, s)
            ids = [...s].filter((id) => buckets.some((b) => b.memory_id === id))
          } catch { /* 无关联即空 */ }
          out.push({ item_id: it.item_id, content: it.content, updated_at: it.updated_at, ids })
        }
        setItems(out)
      } catch { /* 未登录等场景静默 */ } finally { setLoading(false) }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  return (
    <Drawer open={open} side="right" onClose={onClose} glass>
      <span className="pointer-events-none absolute -top-16 -right-10 w-56 h-56 rounded-full bg-[#d9b44a]/35 blur-3xl" />
      <span className="pointer-events-none absolute top-1/2 -left-20 w-60 h-60 rounded-full bg-[#1f3a68]/20 blur-3xl" />
      <span className="pointer-events-none absolute -bottom-10 right-6 w-40 h-40 rounded-full bg-[#f6e3a8]/50 blur-2xl" />

      <div className="relative flex items-start gap-3 px-5 pt-5 pb-4">
        <span className="serif italic font-bold text-[44px] leading-none text-[#1f3a68]">I</span>
        <div className="pt-1.5">
          <p className="serif font-bold text-[#1f3a68] text-[17px]">char</p>
          <p className="text-[11px] tracking-[0.18em] text-[#8a7a52]">我眼中的自己 · {items.length} 则</p>
        </div>
        <button onClick={onClose} aria-label="关闭" className="ml-auto w-8 h-8 rounded-full bg-white/50 border border-white/70 backdrop-blur text-[#1f3a68] text-lg">×</button>
      </div>

      <div className="relative flex-1 overflow-y-auto px-4 pb-6 space-y-3">
        {loading && <p className="text-center text-[12px] text-[#a8862c] py-10">读取中…</p>}
        {!loading && !items.length && (
          <div className="text-center py-10">
            <Butterfly className="w-10 h-8 flutter mx-auto opacity-70" />
            <p className="mt-2 serif italic text-sm text-[#a8862c]">还没有自我认知条目</p>
            <p className="mt-1 text-[11px] text-[#b5ad98]">由char经 MCP 写入（i.item.create）</p>
          </div>
        )}
        {items.map((it, i) => (
          <article key={it.item_id} className="rounded-[22px] bg-white/45 border border-white/80 backdrop-blur-xl shadow-[0_6px_24px_rgba(31,58,104,0.08)] px-4 pt-3.5 pb-3">
            <div className="flex items-baseline gap-2">
              <span className="serif italic text-[12px] text-[#a8862c] tabular-nums">No.{String(i + 1).padStart(2, '0')}</span>
              {it.updated_at && <span className="text-[10.5px] text-[#b5ad98]">{(fmtTime(it.updated_at) || '').slice(0, 10)}</span>}
            </div>
            <p className="mt-1.5 text-[13.5px] leading-relaxed text-[#33405f]">
              <span className="serif text-[#d9b44a] text-lg leading-none mr-0.5">“</span>
              {it.content}
            </p>
            {it.ids.length > 0 && (
              <div className="mt-2.5 pt-2 border-t border-white/80">
                <p className="flex items-center gap-1 text-[10.5px] tracking-[0.15em] text-[#8a7a52] mb-1.5">
                  <svg viewBox="0 0 24 24" className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round"><circle cx="5" cy="12" r="2.2" /><circle cx="12" cy="12" r="2.2" /><circle cx="19" cy="12" r="2.2" /><path d="M7.2 12h2.6M14.2 12h2.6" /></svg>
                  绑定记忆 · {it.ids.length}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {it.ids.map((id) => {
                    const m = buckets.find((b) => b.memory_id === id)
                    return (
                      <button
                        key={id}
                        onClick={() => onOpen(id)}
                        className="group flex items-center gap-1.5 pl-2 pr-2.5 py-1 rounded-full bg-white/60 border border-white/90 text-[12px] text-[#1f3a68] hover:bg-white/90 active:scale-95 transition"
                      >
                        <span className="w-1.5 h-1.5 rounded-full bg-[#d9b44a]" />
                        <span className="max-w-[9rem] truncate">{m?.original_title || id}</span>
                        {m && <span className="text-[10px] text-[#a8862c] tabular-nums">{(dateOf(m) || '').slice(2).replace(/-/g, '.')}</span>}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}
          </article>
        ))}
      </div>
    </Drawer>
  )
}

/* ---------- 后台页（新增：判断开关/向量状态/维护任务/审计日志——全部真实接口） ---------- */
function AdminPage({ bucketsCount }: { bucketsCount: number }) {
  const [policy, setPolicy] = useState<any>(null)
  const [policyRevision, setPolicyRevision] = useState(0)
  const [enabled, setEnabled] = useState(false)
  const [activity, setActivity] = useState<any[]>([])
  const [jobs, setJobs] = useState<any>(null)
  const [outbox, setOutbox] = useState<any>(null)
  const [settings, setSettings] = useState<any>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  const loadAll = async () => {
    setErr('')
    try {
      const [p, a, j, o, s] = await Promise.all([
        api<any>('maintenance.recall_policy.get', {}),
        api<{ events: any[] }>('maintenance.activity.list', { limit: 100 }),
        api<any>('maintenance.jobs.status', {}),
        api<any>('maintenance.outbox.status', {}),
        api<any>('maintenance.settings.get', {}),
      ])
      setPolicy(p); setPolicyRevision(p.revision || 0)
      setEnabled(p.enabled === true)
      setActivity(a.events || []); setJobs(j); setOutbox(o); setSettings(s)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }
  useEffect(() => { loadAll() }, [])

  const savePolicy = async () => {
    if (enabled && !policy?.provider) { setErr('开启判断需要先在环境变量配好 provider（MARIPOSA_RECALL_JUDGE_PROVIDER，见 mariposa.env.example）'); return }
    if (!enabled && !window.confirm('确认关闭判断？关闭后不调用任何判断模型，本次检索的合法候选全集将按页全部交给char自行判断。')) return
    setErr(''); setBusy('保存中…')
    const idem = 'ui-judge-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8)
    try {
      await api('maintenance.recall_policy.update', {
        expected_revision: policyRevision, enabled,
        provider: policy?.provider || null,
        model_id: policy?.model_id || null,
        idempotency_key: idem,
      }, idem)
      setBusy(''); loadAll()
    } catch (e) {
      setBusy(''); setErr(e instanceof Error ? e.message : String(e)); loadAll()
    }
  }

  const warmup = async () => {
    setErr(''); setBusy('向量预热中…')
    try {
      const d = await api<any>('maintenance.semantic.warmup', {})
      setBusy('预热完成：' + JSON.stringify(d).slice(0, 120))
    } catch (e) { setBusy(''); setErr(e instanceof Error ? e.message : String(e)) }
  }

  const card = 'rounded-2xl bg-white/70 border border-[#e6dcc3] px-4 pt-3 pb-3.5'
  const modeText = policy ? ({ on: '判断开启（' + (policy.provider || '?') + '）', off: '判断关闭（候选全集分页交付）', unconfigured: '未配置（判断通道阻断：候选正文不直出）' } as any)[policy.mode] || policy.mode : '…'
  const readiness = policy?.provider_readiness || {}
  const eventTypes = [...new Set(activity.map((e) => e.event_type))]

  return (
    <div className="space-y-5">
      <p className="text-[11px] text-[#b5ad98] leading-relaxed">
        打开本页不调用任何模型；政策仅你本人（qiaosheng）可写。共 {bucketsCount} 个记忆桶。
      </p>

      {/* 召回判断 */}
      <section className={card}>
        <h3 className="serif font-bold text-[#1f3a68]">召回判断</h3>
        <div className="mt-2 flex items-center gap-2 flex-wrap text-[13px] text-[#33405f]">
          <span className={policy?.mode === 'off' ? 'text-[#a8622c] font-bold' : 'text-[#1f3a68] font-bold'}>{modeText}</span>
          <span className="text-[11px] text-[#8a7a52]">
            revision {policy?.revision ?? '—'}
            {policy?.provider && ` · ${policy.provider}${readiness.ready ? '（就绪）' : '（未就绪：' + (readiness.blocked_reason || '?') + '）'}`}
            {policy?.model_id && ` · model ${policy.model_id}`}
          </span>
        </div>
        <div className="mt-2.5 flex items-center gap-2 flex-wrap">
          <label className="flex items-center gap-1.5 text-[13px] text-[#1f3a68]">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            启用判断
          </label>
          <button onClick={savePolicy} className="px-3 py-1.5 rounded-full bg-[#1f3a68] text-[#f6e3a8] text-[12.5px] active:scale-95 transition">保存</button>
          <button onClick={loadAll} className="px-3 py-1.5 rounded-full border border-[#d9b44a] text-[#7a5d14] text-[12.5px]">刷新</button>
        </div>
        {policy?.mode === 'unconfigured' && (
          <p className="mt-2 text-[11.5px] text-[#a8622c]">尚未选择判断模式：这不等于"关闭"。配置前候选正文不直出。</p>
        )}
        <p className="mt-2 text-[11px] text-[#b5ad98] leading-relaxed">
          provider/模型/key 都在环境变量里配（仓库 mariposa.env.example 有样板，生产在
          ~/Data/mariposa-secrets/start.sh 注入后 kickstart）；本页只有开关，就绪状态如实显示
          api_key_missing 等原因——秘密不经屏。
        </p>
      </section>

      {/* 向量 */}
      <section className={card}>
        <h3 className="serif font-bold text-[#1f3a68]">向量模型</h3>
        <p className="mt-1.5 text-[13px] text-[#33405f]">
          状态：<b className={settings?.retrieval?.semantic_provider ? 'text-[#1f3a68]' : 'text-[#a8622c]'}>
            {settings?.retrieval?.semantic_provider
              ? `已配置（${settings.retrieval.semantic_provider}）`
              : '未配置（MARIPOSA_SEMANTIC_PROVIDER 空）'}
          </b>
        </p>
        <p className="mt-1 text-[11px] text-[#b5ad98] leading-relaxed">
          本地模型无需在此配置：端点/模型名由部署环境注入，这里只读状态。语义检索未配置时关键词检索照常工作。
        </p>
        <button onClick={warmup} className="mt-2 px-3 py-1.5 rounded-full border border-[#d9b44a] text-[#7a5d14] text-[12.5px]">
          预热语义向量（maintenance.semantic.warmup）
        </button>
      </section>

      {/* 任务 */}
      <section className={card}>
        <h3 className="serif font-bold text-[#1f3a68]">维护任务</h3>
        <div className="mt-1.5 flex gap-6 text-[13px] text-[#33405f] flex-wrap">
          <span>outbox 待处理 <b className="serif">{outbox?.pending ?? '—'}</b> / {outbox?.total ?? '—'}</span>
          {jobs?.import_jobs && Object.entries(jobs.import_jobs).map(([k, v]) => (
            <span key={k}>导入 {k} <b className="serif">{String(v)}</b></span>
          ))}
        </div>
      </section>

      {/* 日志 */}
      <section className={card}>
        <div className="flex items-baseline gap-2">
          <h3 className="serif font-bold text-[#1f3a68] flex-1">审计日志（最近 {activity.length} 条）</h3>
          <select onChange={(e) => e.target.value ? (async () => {
            try { const d = await api<{ events: any[] }>('maintenance.activity.list', { limit: 100, event_type: e.target.value }); setActivity(d.events || []) } catch { }
          })() : loadAll()} className="text-[12px] text-[#7a5d14] bg-transparent border-b border-dashed border-[#d9b44a]/70 outline-none">
            <option value="">全部类型</option>
            {eventTypes.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="mt-2 max-h-80 overflow-y-auto">
          {activity.map((e, i) => (
            <div key={e.event_id || i} className="flex items-baseline gap-2 py-1 border-b border-[#ece3cc] text-[12px]">
              <span className="text-[#9aa3b8] tabular-nums shrink-0">{fmtTime(e.occurred_at)}</span>
              <span className="text-[#1f3a68] shrink-0">{e.event_type}</span>
              <span className="text-[#8a7a52] shrink-0">{e.actor_principal}</span>
              <span className="text-[#7d8aa8] truncate flex-1" title={e.resource_id}>{e.resource_id || ''}</span>
              {e.resource_version != null && <span className="text-[#b5ad98] shrink-0">v{e.resource_version}</span>}
            </div>
          ))}
          {!activity.length && <p className="py-6 text-center text-[12px] text-[#9aa3b8]">暂无审计事件</p>}
        </div>
      </section>

      {busy && <p className="text-[12px] text-[#a8862c]">{busy}</p>}
      {err && <p className="text-[12px] text-[#c0392b]">{err}</p>}
    </div>
  )
}
