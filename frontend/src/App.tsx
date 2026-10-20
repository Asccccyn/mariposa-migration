import { useEffect, useMemo, useRef, useState } from 'react'
import SourceSearchForm from './SourceSearchForm'
import {
  api, ApiError, loadAllBuckets, loadMoodVocab, login, logout, NAMES,
  session, uploadSource,
  loadBucket, loadBucketsByIds, operationId, errorMessage,
  type Bucket, type Word, type OpenedBucket, type ViewReceipt,
} from './api'
import DeletionRequests from './DeletionRequests'
import SourceEvidence, { type EvidenceTarget, type SourceSelection } from './SourceEvidence'

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
 *          心情按已授权的整理期规则编辑
 * ================================================================== */

type Tab = 'search' | 'relation' | 'home' | 'source' | 'admin'

// 十分类以后端为准（categories.py LABELS）；顺序沿用设计稿的视觉顺序，
// diary（她 2026-10-17 加）暂列队尾——视觉位次她以后排
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
  { key: 'diary', label: '日记' },
]
const CAT_LABELS: Record<string, string> = Object.fromEntries(CATEGORIES.map((c) => [c.key, c.label]))
// 剧本/日记单分类（她 2026-10-20 终裁）：选中即独占整组；之后再选其他分类会替换掉独占类
const EXCLUSIVE_CATS = ['reloplay', 'diary']
const normalizeCats = (v: string[]): string[] => {
  const last = v[v.length - 1]
  if (last && EXCLUSIVE_CATS.includes(last)) return [last]
  const rest = v.filter((c) => !EXCLUSIVE_CATS.includes(c))
  return rest.length ? rest : v.filter((c) => EXCLUSIVE_CATS.includes(c))
}
// 心情七大类：登录后从 memory.mood.vocab 动态拉取（单一事实源），
// 未拉到前用 2026-10-05 终版表兜底显示
const MOOD_FALLBACK = ['开心', '爱', '生气', '吃醋', '悲伤', '渴望', '不安']
const PAGE_SIZE = 30
const SOURCE_PAGE_SIZE = 30
const MESSAGE_PAGE_SIZE = 30

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
    if (!open) return
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open, onClose])
  return (
    <div aria-hidden={!open} inert={!open} className={`fixed inset-0 z-50 ${open ? '' : 'pointer-events-none'}`}>
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
  const byId = new Map(buckets.map(bucket => [bucket.memory_id, bucket]))
  const neighbors = new Map<string, Set<string>>()
  for (const bucket of buckets) for (const relation of bucket.relations || []) {
    const from = bucket.memory_id, to = relation.other_memory
    if (!byId.has(to)) continue
    if (!neighbors.has(from)) neighbors.set(from, new Set())
    if (!neighbors.has(to)) neighbors.set(to, new Set())
    neighbors.get(from)!.add(to); neighbors.get(to)!.add(from)
  }
  const seen = new Set<string>()
  const groups: { id: string; ids: string[] }[] = []
  for (const start of neighbors.keys()) {
    if (seen.has(start)) continue
    const ids: string[] = [], pending = [start]
    while (pending.length) {
      const id = pending.pop()!
      if (seen.has(id)) continue
      seen.add(id); ids.push(id)
      for (const other of neighbors.get(id) || []) if (!seen.has(other)) pending.push(other)
    }
    ids.sort()
    groups.push({ id: ids.join('+'), ids })
  }
  return groups
}

/* ---------- App ---------- */
export default function App() {
  const [authed, setAuthed] = useState(false)
  const [booting, setBooting] = useState(false)
  const [bootError, setBootError] = useState('')
  const headerRef = useRef<HTMLElement>(null)
  const [headerHeight, setHeaderHeight] = useState(0)

  useEffect(() => {
    const header = headerRef.current
    if (!header) return
    const measure = () => setHeaderHeight(header.getBoundingClientRect().height)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(header)
    return () => observer.disconnect()
  }, [authed])
  const [buckets, setBuckets] = useState<Bucket[]>([])
  const [moods, setMoods] = useState<string[]>(MOOD_FALLBACK)

  const [tab, setTab] = useState<Tab>('home')
  const [category, setCategory] = useState<string | null>(null)
  const [pages, setPages] = useState<Record<string, number>>({})
  const scrollMemo = useRef<Record<string, number>>({})
  const [openId, setOpenId] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [receipt, setReceipt] = useState<ViewReceipt | null>(null)
  const detailRequest = useRef(0)
  const [refreshError, setRefreshError] = useState('')
  const [lastId, setLastId] = useState<string | null>(null)
  const [selfOpen, setSelfOpen] = useState(false)
  const [focusRel, setFocusRel] = useState<string | null>(null)
  const [query, setQuery] = useState<Query>(EMPTY_Q)
  // 原文页跳转目标：桶抽屉"查看原文"→ {conversation_id, around_seq}
  const [srcJump, setSrcJump] = useState<SourceJump | null>(null)

  const resetSession = () => {
    detailRequest.current++
    setAuthed(false); setBuckets([]); setOpenId(null); setReceipt(null)
    setSelfOpen(false); setSrcJump(null); setQuery(EMPTY_Q); setPages({})
    setCategory(null); setTab('home'); setFocusRel(null); setLastId(null)
    setRefreshError(''); setDetailError(''); setDetailLoading(false)
    scrollMemo.current = {}
  }

  useEffect(() => {
    const h = () => resetSession()
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
    const generation = session.generation
    try {
      const bs = await loadAllBuckets()
      if (generation !== session.generation) return
      setBuckets(previous => {
        const cached = new Map(previous.map(bucket => [bucket.memory_id, bucket]))
        return bs.map(fresh => {
          const old = cached.get(fresh.memory_id)
          if (old && old.version > fresh.version) return old
          return old?.version === fresh.version ? { ...old, ...fresh } : fresh
        })
      })
      setRefreshError('')
    } catch (e) { if (generation === session.generation) setRefreshError(errorMessage(e)) }
  }

  const key = category ?? '__all'
  const list = useMemo(() => (category ? buckets.filter((b) => (b.categories || []).includes(category)) : buckets), [buckets, category])
  const totalPages = Math.max(1, Math.ceil(list.length / PAGE_SIZE))
  const page = Math.min(pages[key] ?? 1, totalPages)
  const shown = list.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  const current = buckets.find((b) => b.memory_id === openId) ?? null
  const bucketsById = useMemo(() => new Map(buckets.map(b => [b.memory_id, b])), [buckets])
  const byId = (id: string) => bucketsById.get(id)

  const switchCategory = (c: string | null) => {
    scrollMemo.current[key] = window.scrollY
    setCategory(c)
    requestAnimationFrame(() => window.scrollTo({ top: scrollMemo.current[c ?? '__all'] ?? 0 }))
  }
  const goPage = (p: number) => {
    setPages((s) => ({ ...s, [key]: p }))
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const openBucket = async (id: string) => {
    const request = ++detailRequest.current
    setOpenId(id)
    setLastId(id)
    setReceipt(null); setDetailError(''); setDetailLoading(true)
    try {
      const opened = await api<OpenedBucket>('memory.open', { memory_id: id })
      const fresh = await loadBucket(id, opened)
      if (request !== detailRequest.current) return
      patchBucket(fresh)
      setReceipt({ memoryId: id, version: fresh.version, id: opened.view_receipt, confirmKey: opened.confirm_key })
    } catch (e) { if (request === detailRequest.current) setDetailError(errorMessage(e)) }
    finally { if (request === detailRequest.current) setDetailLoading(false) }
  }
  const closeBucket = () => { detailRequest.current++; setOpenId(null); setReceipt(null); setDetailError(''); setDetailLoading(false) }
  const goTab = (t: Tab) => {
    if (t === tab) return
    if (tab === 'home') scrollMemo.current['__home_' + key] = window.scrollY
    setTab(t)
    requestAnimationFrame(() => window.scrollTo({ top: t === 'home' ? scrollMemo.current['__home_' + key] ?? 0 : 0 }))
  }
  const patchBucket = (b: Bucket) =>
    setBuckets((bs) => bs.some(x => x.memory_id === b.memory_id)
      ? bs.map((x) => (x.memory_id === b.memory_id && b.version >= x.version ? b : x)) : [...bs, b])
  const patchRelations = (cards: Record<string, Partial<Bucket>>) => setBuckets(previous => previous.map(bucket =>
    cards[bucket.memory_id] ? { ...bucket, relations: cards[bucket.memory_id].relations || [] } : bucket))
  const goSource = (jump: SourceJump) => { setSrcJump(jump); closeBucket(); goTab('source') }

  if (!authed) {
    return <LoginPage onLogin={boot} loading={booting} error={bootError} />
  }

  return (
    <div className="min-h-screen relative overflow-x-clip bg-[#fdfaf2]" style={{ '--app-header-height': `${headerHeight}px` } as React.CSSProperties}>
      {/* Header（设计稿原样 + 身份回显/退出） */}
      <header ref={headerRef} className="sticky top-0 z-30 bg-[#fdfaf2]/90 backdrop-blur-md border-b border-[#e9dcb8]/60" style={{ paddingTop: 'env(safe-area-inset-top)' }}>
        <div className="max-w-5xl mx-auto px-4 pt-3 pb-2 flex items-center gap-2">
          <Butterfly className="w-8 h-7 flutter" />
          <h1 className="serif text-xl font-bold tracking-[0.08em] italic text-[#1e2a4a]">Mariposa</h1>
          <span className="ml-auto text-xs text-[#8a7a52]">{tab === 'home' ? `${list.length} 个记忆桶` : TAB_NAMES[tab]}</span>
          <span className="text-[11px] text-[#8a7a52]">· {(NAMES[session.principal] || session.principal).split('（')[0]}</span>
          <button
            onClick={() => { logout(); resetSession() }}
            className="text-[11px] text-[#a8862c] border-b border-[#a8862c]/50"
          >退出</button>
        </div>
        {tab === 'home' && (
          <nav className="max-w-5xl mx-auto px-3 pb-2.5 grid grid-cols-5 gap-1 sm:flex sm:flex-wrap sm:gap-1.5">
            {[null, ...CATEGORIES].map((c) => {
              const active = (c?.key ?? null) === category
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
        {refreshError && <p role="alert" className="text-sm text-[#c0392b]">刷新失败：{refreshError} <button onClick={refreshBuckets} className="underline">重试</button></p>}
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
          <SearchPage q={query} setQ={setQuery} buckets={buckets} byId={byId} onOpen={openBucket} lastId={lastId} moods={moods} onLoaded={patchBucket} />
        ) : tab === 'relation' ? (
          <RelationPage focus={focusRel} setFocus={setFocusRel} buckets={buckets} byId={byId} onOpen={openBucket} onSource={goSource} onRelations={patchRelations} />
        ) : tab === 'source' ? (
          <SourcePage jump={srcJump} clearJump={() => setSrcJump(null)} buckets={buckets} onOpen={openBucket} refreshBuckets={refreshBuckets} />
        ) : (
          <AdminPage bucketsCount={buckets.length} onChanged={refreshBuckets} />
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
      <Drawer open={openId !== null} side="left" onClose={closeBucket}>
        {detailLoading && <p className="px-5 py-2 text-xs text-[#a8862c]" role="status">正在读取当前版本…</p>}
        {detailError && <p className="px-5 py-2 text-xs text-[#c0392b]" role="alert">{detailError} <button onClick={() => openId && openBucket(openId)} className="underline">重试</button></p>}
        {current && (
          <BucketDetail
            key={current.memory_id}
            b={current}
            receipt={receipt}
            opening={detailLoading}
            onReopen={() => openBucket(current.memory_id)}
            moods={moods}
            onClose={closeBucket}
            onSaved={patchBucket}
            onDelete={(id) => { setBuckets(bs => bs.filter(x => x.memory_id !== id)); closeBucket() }}
            onRelation={(gid) => { setFocusRel(gid); setOpenId(null); goTab('relation') }}
            onSource={() => goSource({ memory_id: current.memory_id })}
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
  const inflight = useRef(false)
  const createAttempt = useRef<{ payload: string; key: string } | null>(null)
  const loadRequest = useRef(0)
  const load = async () => {
    const request = ++loadRequest.current
    try {
      const d = await api<{ plans: Plan[] }>('plan.list', {})
      if (request === loadRequest.current) setPlans(d.plans || [])
    } catch (e) { if (request === loadRequest.current) setErr(errorMessage(e)) }
  }
  useEffect(() => { load(); return () => { loadRequest.current++ } }, [])
  const create = async () => {
    if (!title.trim() || inflight.current) return
    inflight.current = true
    setErr(''); setBusy(true)
    try {
      const args = { title: title.trim(), state: 'planned', ...(due ? { due_at: due } : {}) }
      const payload = JSON.stringify(args)
      if (createAttempt.current?.payload !== payload) createAttempt.current = { payload, key: operationId('ui-plan') }
      await api('plan.create', args, createAttempt.current.key)
      createAttempt.current = null
      setTitle(''); setDue('')
      await load()
    } catch (e) { setErr(errorMessage(e)) } finally { inflight.current = false; setBusy(false) }
  }
  const setState_ = async (p: Plan, state: string) => {
    if (inflight.current) return
    inflight.current = true; setBusy(true); setErr('')
    try {
      await api('plan.update', { plan_id: p.plan_id, expected_version: p.version, state })
      await load()
    } catch (e) { setErr(errorMessage(e)); await load() }
    finally { inflight.current = false; setBusy(false) }
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
                <button key={s} disabled={busy} onClick={() => setState_(p, s)} title={`改为 ${PLAN_STATE_LABEL[s]}`}
                  className="text-[10.5px] text-[#8a7a52] hover:text-[#1f3a68] border-b border-transparent hover:border-[#a8862c]">{PLAN_STATE_LABEL[s]}</button>
              ))}
            </span>
          </div>
        ))}
        {!plans.length && <p className="text-[12px] text-[#9aa3b8] py-1">还没有计划</p>}
      </div>
      {err && <p className="mt-1.5 text-[11.5px] text-[#c0392b]">{err}</p>}
    </section>
  )
}

/* ---------- Pager（记忆桶与原文共用） ---------- */
function Pager({ page, total, onChange, disabled = false, compact = false }: {
  page: number; total: number; onChange: (p: number) => void; disabled?: boolean; compact?: boolean
}) {
  const nums: (number | '…')[] = []
  for (let i = 1; i <= total; i++) {
    if (i === 1 || i === total || Math.abs(i - page) <= 1) nums.push(i)
    else if (nums[nums.length - 1] !== '…') nums.push('…')
  }
  const arrow = (dir: -1 | 1) => (
    <button type="button"
      disabled={disabled || (dir < 0 ? page === 1 : page === total)}
      onClick={() => onChange(page + dir)}
      aria-label={dir < 0 ? '上一页' : '下一页'}
      className="w-8 h-8 shrink-0 flex items-center justify-center text-[#1f3a68] disabled:opacity-25 hover:text-[#a8862c] transition-colors">
      <svg viewBox="0 0 24 24" className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
        <path d={dir < 0 ? 'M15 5 8 12l7 7' : 'm9 5 7 7-7 7'} />
      </svg>
    </button>
  )
  const jump = (
    <label className="flex items-center gap-1 text-[11px] text-[#a8862c] serif italic whitespace-nowrap">
      翻到
      <span className="inline-flex items-baseline gap-0.5 not-italic tabular-nums">
        <select aria-label="跳转页码" value={page} disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
          className="appearance-none bg-transparent border-b border-dashed border-[#d9b44a] px-1.5 py-1 text-center text-[12px] text-[#1f3a68] outline-none focus:border-solid cursor-pointer disabled:opacity-50">
          {Array.from({ length: total }, (_, i) => <option key={i + 1} value={i + 1}>{i + 1}</option>)}
        </select>
        <span className="text-[#a8862c]">/{total}</span>
      </span>
      页
    </label>
  )
  if (compact) return <div className="flex items-center justify-center gap-2">{arrow(-1)}{jump}{arrow(1)}</div>
  return (
    <div className="mt-5 flex flex-col items-center gap-1">
      <div className="flex items-center gap-0.5">
        {arrow(-1)}
        {nums.map((n, i) => n === '…' ? (
          <span key={i} className="w-5 text-center text-[#c9b27a] text-[10px]">···</span>
        ) : (
          <button type="button" key={i} disabled={disabled} onClick={() => onChange(n)} aria-current={n === page ? 'page' : undefined}
            className={`relative w-7 h-8 serif tabular-nums transition-colors disabled:opacity-50 ${n === page ? 'text-[#1f3a68] text-[14px] font-bold' : 'text-[#8f98ad] text-[12px] hover:text-[#1f3a68]'}`}>
            {n}
            <span className={`absolute left-1/2 -translate-x-1/2 bottom-0.5 h-1 w-1 rotate-45 bg-[#d9b44a] ${n === page ? 'opacity-100' : 'opacity-0'}`} />
          </button>
        ))}
        {arrow(1)}
      </div>
      {jump}
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
const Section = ({ label, right, children }: { label: string; right?: React.ReactNode; children: React.ReactNode }) => (
  <section className="py-3 border-b border-[#ece3cc]">
    <div className="flex items-center mb-1.5">
      <h4 className="text-[11px] tracking-[0.2em] text-[#a8862c]">{label}</h4>
      {right && <div className="ml-auto">{right}</div>}
    </div>
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
          </div>
        </div>
      ))}
      <div className="flex justify-between text-[10px] text-[#9aa3b8] px-1 pt-0.5"><span>char</span><span>我</span></div>
    </div>
  )
}

function BucketDetail({ b, receipt, opening, onReopen, moods, onClose, onSaved, onDelete, onRelation, onSource }: {
  b: Bucket
  receipt: ViewReceipt | null
  opening: boolean
  onReopen: () => Promise<void>
  moods: string[]
  onClose: () => void
  onSaved: (b: Bucket) => void
  onDelete: (id: string) => void
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
  // 心情编辑（她 2026-10-14 整理期临时放开）
  const [moodEdit, setMoodEdit] = useState(false)
  const [moodText, setMoodText] = useState(b.mood?.text || '')
  const [moodTags, setMoodTags] = useState(b.mood?.tags || [])
  const [keepWide, setKeepWide] = useState(false)
  const [confirmedReceipt, setConfirmedReceipt] = useState('')
  const [conflict, setConflict] = useState(false)
  const [revising, setRevising] = useState<string | null>(null)
  const [revisionText, setRevisionText] = useState('')
  const inflight = useRef(false)
  const recallAttempt = useRef<{ text: string; key: string } | null>(null)
  const [recallOpen, setRecallOpen] = useState(false)
  useEffect(() => {
    if (!editing) { setTitle(b.original_title); setText(b.text); setDate(dateOf(b)); setCats(b.categories || []) }
    if (!moodEdit) { setMoodText(b.mood?.text || ''); setMoodTags(b.mood?.tags || []) }
  }, [b, editing, moodEdit])
  useEffect(() => {
    setConfirmedReceipt('')
    if (editing || !receipt || receipt.memoryId !== b.memory_id || receipt.version !== b.version) return
    let active = true
    // The effect runs after this exact version has rendered; previews never confirm.
    const frame = requestAnimationFrame(() => {
      api('memory.view.confirm', { memory_id: b.memory_id, receipt_id: receipt.id,
        ...(receipt.confirmKey ? { confirm_key: receipt.confirmKey } : {}) })
        .then(() => { if (active) { setConfirmedReceipt(receipt.id); setConflict(false) } })
        .catch(e => { if (active) { setErr(errorMessage(e)); setConflict(true) } })
    })
    return () => { active = false; cancelAnimationFrame(frame) }
  }, [receipt, b.memory_id, b.version, editing])
  const canRecall = !!receipt && confirmedReceipt === receipt.id && receipt.version === b.version && !opening && !editing
  const acquire = (label: string) => {
    if (inflight.current || opening) return false
    inflight.current = true; setErr(''); setBusy(label); return true
  }
  const release = () => { inflight.current = false; setBusy('') }
  const failed = (e: unknown) => {
    setErr(errorMessage(e))
    if (e instanceof ApiError && ['VERSION_CONFLICT', 'VIEW_RECEIPT_INVALID'].includes(e.code)) setConflict(true)
  }
  const hasRelations = !!b.relations?.length
  const field = 'w-full bg-transparent border-0 border-b border-dashed border-[#d9b44a]/70 px-0.5 py-1 outline-none transition-colors focus:border-solid focus:border-[#1f3a68] placeholder:text-[#c2b48f] placeholder:italic'
  const paper = 'w-full resize-none bg-[#fbf6e8] border-l-2 border-[#d9b44a] rounded-r-md px-3 py-2 outline-none transition-colors focus:bg-[#f8f0da] leading-relaxed'

  // char直删（P-D02，她 2026-10-14 要的行首入口）：无申请无审批，
  // 物理删除；仅 jiaming 身份渲染本按钮，后端合同同样只认 jiaming
  const directDelete = async () => {
    if (inflight.current || opening) return
    if (!window.confirm(`char直删「${b.original_title || b.memory_id}」？\n\nP-D02：无申请无审批，物理删除，不可恢复。`)) return
    if (!acquire('删除中…')) return
    try {
      await api('memory.deletion.direct', { memory_id: b.memory_id }, 'ui-del-' + b.memory_id + '-' + Date.now())
      onDelete(b.memory_id)
    } catch (e) {
      failed(e)
    } finally { release() }
  }

  // 整理期授权保留；文字与标签一起送出，不另造权限规则。
  const saveMood = async () => {
    if (!acquire('保存心情…')) return
    try {
      await api('memory.mood.update', { memory_id: b.memory_id, mood: { text: moodText, tags: moodTags } }, operationId('ui-mood'))
      const fresh = await api<Bucket>('memory.get', { memory_id: b.memory_id })
      onSaved({ ...b, ...fresh })
      setMoodEdit(false)
    } catch (e) {
      failed(e)
    } finally { release() }
  }

  const save = async () => {
    if (!cats.length) { setErr('至少选一个分类'); return }
    if (!title.trim() || !text.trim()) { setErr('标题和事件正文不能为空'); return }
    if (!acquire('保存中…')) return
    try {
      const args: Record<string, unknown> = { memory_id: b.memory_id, expected_version: b.version }
      if (title !== b.original_title) args.original_title = title
      if (text !== b.text) args.text = text
      if (date !== dateOf(b)) args.memory_date = date || null
      if (JSON.stringify(cats) !== JSON.stringify(b.categories || [])) {
        args.categories = cats
      }
      if (Object.keys(args).length > 2) await api('memory.update', args, operationId('ui-upd'))
      setEditing(false)
      await onReopen()
    } catch (e) {
      failed(e)
    } finally { release() }
  }

  const submitRecall = async () => {
    if (!recall.trim() || !canRecall || !receipt || !acquire('正在写下回忆…')) return
    try {
      const payload = JSON.stringify([recall.trim(), keepWide, receipt.id])
      if (recallAttempt.current?.text !== payload) recallAttempt.current = { text: payload, key: operationId('ui-rec') }
      await api('memory.recollections.append', {
        memory_id: b.memory_id, receipt_id: receipt.id,
        text: recall.trim(), keep_wide: keepWide }, recallAttempt.current.key)
      recallAttempt.current = null
      setRecall(''); setKeepWide(false)
      onSaved(await loadBucket(b.memory_id))
    } catch (e) {
      failed(e)
    } finally { release() }
  }

  const reviseRecall = async () => {
    if (!revising || !revisionText.trim() || !acquire('保存回忆修订…')) return
    try {
      await api('memory.recollections.revise', { recollection_id: revising, text: revisionText.trim() }, operationId('ui-rec-revise'))
      setRevising(null); onSaved(await loadBucket(b.memory_id))
    } catch (e) { failed(e) } finally { release() }
  }
  const revokeKeep = async (markId: string) => {
    if (!acquire('撤销留标记…')) return
    try {
      await api('memory.keep.revoke', { mark_id: markId })
      onSaved(await loadBucket(b.memory_id))
    } catch (e) { failed(e) } finally { release() }
  }

  const submitDeletion = async () => {
    if (!delReason.trim() || !acquire('提交删除申请…')) return
    try {
      await api<{ request_id: string }>('memory.deletion.request', {
        memory_id: b.memory_id, reason: delReason.trim(),
        operation_id: 'ui-del-' + b.memory_id + '-' + Date.now(),
      })
      setDelDone('删除申请已提交，等待char确认。')
      setDelOpen(false); setDelReason('')
    } catch (e) {
      failed(e)
    } finally { release() }
  }

  return (
    <>
      <div className="flex items-center gap-1 px-3 py-2 border-b border-[#ece3cc]">
        {session.principal === 'jiaming' && !editing && (
          <button onClick={directDelete} disabled={!!busy || opening} aria-label="直删" title="char直删（无申请无审批，物理删除）"
                  className="w-8 h-8 rounded-full flex items-center justify-center transition text-[#b3423a] hover:bg-[#f6dede] active:scale-95">
            <svg viewBox="0 0 24 24" className="w-[17px] h-[17px]" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><path d="M4 7h16" /><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /><path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" /><path d="M10 11v6M14 11v6" /></svg>
          </button>
        )}
        {hasRelations && (
          <button onClick={() => onRelation(b.memory_id)} title="查看实际关联" className="flex items-center gap-1.5 h-8 px-2.5 rounded-full bg-[#e8edf6] text-[#1f3a68] text-xs active:scale-95 transition">
            <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round"><circle cx="5" cy="12" r="2.2" /><circle cx="12" cy="12" r="2.2" /><circle cx="19" cy="12" r="2.2" /><path d="M7.2 12h2.6M14.2 12h2.6" /></svg>
            查看关联
          </button>
        )}
        {editing ? (
          <button onClick={() => { setEditing(false); setTitle(b.original_title); setText(b.text); setDate(dateOf(b)); setCats(b.categories || []) }} className="ml-auto w-8 h-8 rounded-full text-[#1f3a68] hover:bg-[#f5ead0] text-lg">×</button>
        ) : (
          <>
            <button onClick={() => setEditing(true)} disabled={!!busy || opening || !!b.content_gap} aria-label="编辑" className="ml-auto w-8 h-8 rounded-full flex items-center justify-center transition text-[#1f3a68] hover:bg-[#f5ead0] disabled:opacity-30">
              <svg viewBox="0 0 24 24" className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><path d="M4 20h4L19 9l-4-4L4 16z" /><path d="m13.5 6.5 4 4" /></svg>
            </button>
            <button onClick={onClose} aria-label="关闭" className="w-8 h-8 rounded-full hover:bg-[#f5ead0] text-[#1f3a68] text-lg">×</button>
          </>
        )}
      </div>

      <div className="flex-1 min-h-0 min-w-0 overflow-y-auto px-5 pb-6">
        {b.content_gap && <p role="note" className="mt-3 rounded-lg bg-[#faf3ec] p-3 text-xs text-[#a8622c]">{b.content_gap.note}。此表示不能原地编辑。</p>}
        <div className="pt-4 pb-3 border-b border-[#ece3cc]">
          {editing ? (
            <>
              <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={30} className={`${field} serif text-xl font-bold text-[#1f3a68]`} />
              <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
                <MultiSelect value={cats.map((c) => CAT_LABELS[c] || c)} options={CATEGORIES.map((c) => c.label)} editing onChange={(labels) => setCats(normalizeCats(labels.map((l) => CATEGORIES.find((c) => c.label === l)?.key || l)))} tone="blue" />
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

        {/* 心情（她 2026-10-14 整理期临时放开自编辑：memory.mood.update，
            豁免口 MARIPOSA_MOOD_EDIT_OVERRIDE——整理完删 env 重启即恢复只读合同） */}
        <Section label="心情" right={!editing && (
          <button disabled={!!busy || opening} onClick={() => setMoodEdit(m => !m)} className="text-[11px] text-[#1f3a68] underline underline-offset-2">
            {moodEdit ? '收起' : '编辑心情'}
          </button>
        )}>
          {moodEdit ? (
            <div className="w-full">
              <MultiSelect value={moodTags} options={moods} editing onChange={(tags) => {
                if (tags.length <= 3) { setMoodTags(tags); setErr('') } else setErr('心情标签最多选择三个')
              }} tone="gold" />
              <textarea value={moodText} onChange={(e) => setMoodText(e.target.value)}
                placeholder="心情的话（当时心情=感受+为什么记得+想说没说出口的话）"
                className="w-full min-h-[90px] resize-y bg-[#fbf6e8] border-l-2 border-[#d9b44a] rounded-r-md px-3 py-2 text-sm leading-relaxed outline-none" />
              <div className="mt-1.5 flex items-center gap-2">
                <button onClick={saveMood} disabled={!!busy} className="px-3 py-1 rounded-full bg-[#1f3a68] text-white text-xs active:scale-95 transition disabled:opacity-50">
                  保存心情
                </button>
                {err && <span className="text-xs text-[#b3423a]">{err}</span>}
              </div>
            </div>
          ) : (b.mood?.tags?.length || b.mood?.text)
            ? (<>
              {!!b.mood?.tags?.length && <MultiSelect value={b.mood.tags} options={b.mood.tags} editing={false} onChange={() => {}} tone="gold" />}
              {b.mood?.text && <p className="mt-1.5 text-sm leading-relaxed text-[#5a6480] italic">{b.mood.text}</p>}
            </>)
            : <p className="text-[13px] text-[#9aa3b8]">（未记——点"编辑心情"补写）</p>}
        </Section>

        <Section label="事件">
          {editing ? (
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} className={`${paper} text-[15px] text-[#33405f]`} />
          ) : (
            <p className="text-[15px] leading-loose text-[#33405f] whitespace-pre-wrap [overflow-wrap:anywhere]">{b.text}</p>
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
                  {revising === r.recollection_id ? <div>
                    <textarea aria-label="修订本人回忆" className={`${paper} text-sm`} value={revisionText} onChange={e => setRevisionText(e.target.value)} disabled={!!busy} />
                    <button disabled={!!busy || !revisionText.trim()} onClick={reviseRecall} className="text-xs underline text-[#1f3a68]">保存修订</button>
                    <button disabled={!!busy} onClick={() => setRevising(null)} className="ml-3 text-xs text-[#8a7a52]">取消</button>
                  </div> : <p className="text-sm text-[#33405f] leading-relaxed">{r.text}</p>}
                  <p className="text-[11px] text-[#a8862c] mt-0.5">{(r.written_at || '').slice(0, 10)} 再读时 · {(r.author_principal || r.author) === 'jiaming' ? 'char' : '宿主用户'}
                    {(r.author_principal || r.author) === session.principal && <button disabled={!!busy} onClick={() => { setRevising(r.recollection_id); setRevisionText(r.text) }} className="ml-2 underline">修订</button>}
                  </p>
                </div>
              ))}
            </div>
            <RecallForm recall={recall} setRecall={setRecall} busy={busy || (!canRecall ? '请先重新打开当前版本' : '')} onSubmit={submitRecall} field={field} />
          </Section>
        ) : (
          recallOpen && (
            <Section label="回忆">
              <RecallForm recall={recall} setRecall={setRecall} busy={busy || (!canRecall ? '请先重新打开当前版本' : '')} onSubmit={submitRecall} field={field} autoFocus />
            </Section>
          )
        )}

        {busy && <p className="mt-3 text-[12px] text-[#a8862c]">{busy}</p>}
        {err && <p className="mt-3 text-[12px] text-[#c0392b]">{err}</p>}
        {(recallOpen || !!b.recollections?.length) && <label className="mt-2 flex items-center gap-2 text-xs text-[#7a5d14]"><input type="checkbox" checked={keepWide} disabled={!!busy} onChange={e => setKeepWide(e.target.checked)} />留下这条回忆时，也选择「留」</label>}
        {(b.keeps || []).filter(mark => !mark.revoked_at).map(mark => <p key={mark.mark_id} className="mt-2 text-xs text-[#7a5d14]">{(NAMES[mark.owner] || mark.owner).split('（')[0]}选择了「留」{mark.owner === session.principal && <button disabled={!!busy} onClick={() => revokeKeep(mark.mark_id)} className="ml-2 underline">撤销我的留标记</button>}</p>)}
        {(conflict || (!canRecall && !opening && !editing)) && <p className="mt-2 text-xs text-[#a8622c]">请重新打开当前版本后继续。草稿会保留。 <button disabled={!!busy} onClick={onReopen} className="underline">重新打开</button></p>}
        {editing && conflict && <p className="mt-2 text-xs text-[#8a7a52]">当前版本 {b.version}：{b.original_title}。重新打开会更新版本，保留你正在编辑的草稿；确认后可再次保存。</p>}

        {editing && (
          <button onClick={save} disabled={!!busy || opening} className="mt-4 w-full py-2.5 rounded-full bg-[#1f3a68] text-[#f6e3a8] serif italic tracking-wider active:scale-95 transition disabled:opacity-40">
            保存修订
          </button>
        )}

        {/* 删除申请（qiaosheng；审批权在char） */}
        {session.principal === 'qiaosheng' && (
          <div className="mt-3">
            {delDone
              ? <p className="text-[11px] text-[#a8622c] leading-relaxed">{delDone}</p>
              : delOpen ? (
                <div className="relative rounded-xl border border-[#e0c8b8] bg-[#faf3ec] px-3 py-3">
                  <button type="button" aria-label="关闭删除确认" disabled={!!busy}
                    onClick={() => { setDelOpen(false); setDelReason('') }}
                    className="absolute right-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded-full text-lg text-[#8a7a52] hover:bg-[#f0e3d7] disabled:opacity-40">×</button>
                  <p className="pr-7 text-[12px] text-[#a8622c] leading-relaxed">确定要删除这个记忆吗？</p>
                  <form className="mt-2 flex min-w-0 items-end gap-3" onSubmit={(e) => { e.preventDefault(); submitDeletion() }}>
                    <input value={delReason} onChange={(e) => setDelReason(e.target.value)} required disabled={!!busy}
                      aria-label="删除理由" placeholder="删除理由"
                      className="min-w-0 w-full flex-1 bg-transparent border-0 border-b border-dashed border-[#c98b5f]/70 py-1 text-[13px] text-[#5c3a24] outline-none focus:border-solid placeholder:text-[#d3b8a3] placeholder:italic" />
                    <button type="submit" disabled={!delReason.trim() || !!busy}
                      className="shrink-0 text-[12.5px] text-[#a83c1c] px-1 py-1 border-b border-[#a83c1c]/70 disabled:text-[#d3b8a3] disabled:border-transparent">确定</button>
                  </form>
                </div>
              ) : (
                <button onClick={() => setDelOpen(true)} className="text-[11px] text-[#c9a08a] hover:text-[#a83c1c] border-b border-transparent hover:border-[#c9a08a]">
                  申请删除这个记忆…
                </button>
              )}
          </div>
        )}
        <DeletionRequests key={delDone} memoryId={b.memory_id} onChanged={onReopen} onDeleted={onDelete} compact />

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
function RelationPage({ focus, setFocus, buckets, byId, onOpen, onSource, onRelations }: {
  focus: string | null; setFocus: (id: string | null) => void; buckets: Bucket[]
  byId: (id: string) => Bucket | undefined; onOpen: (id: string) => void; onSource: (jump: SourceJump) => void
  onRelations: (cards: Record<string, Partial<Bucket>>) => void
}) {
  const groups = useMemo(() => buildGroups(buckets), [buckets])
  const selected = groups.find(group => group.id === focus || (!!focus && group.ids.includes(focus)))
  const edges = useMemo(() => {
    const unique = new Map<string, { id: string; from: string; to: string; label: string; reverse?: string | null }>()
    for (const bucket of buckets) for (const edge of bucket.relations || []) {
      const from = edge.direction === 'out' ? bucket.memory_id : edge.other_memory
      const to = edge.direction === 'out' ? edge.other_memory : bucket.memory_id
      unique.set(edge.relation_id, { id: edge.relation_id, from, to,
        label: edge.custom_label || REL_TYPE_LABEL[edge.relation_type] || edge.relation_type, reverse: edge.reverse_label })
    }
    return [...unique.values()]
  }, [buckets])
  const visible = selected ? edges.filter(edge => selected.ids.includes(edge.from) && selected.ids.includes(edge.to)) : edges
  const [episodes, setEpisodes] = useState<{ episode_id: string; label: string; state: string; updated_at: string; source_selections: SourceSelection[] }[]>([])
  const [next, setNext] = useState<object | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [relationsError, setRelationsError] = useState('')
  const [relationsLoading, setRelationsLoading] = useState(false)
  const relationsRequest = useRef(0)
  const bucketKey = buckets.map(bucket => bucket.memory_id).sort().join('|')
  const readRelations = async () => {
    const current = ++relationsRequest.current
    const generation = session.generation
    setRelationsLoading(true); setRelationsError('')
    try {
      const cards: Record<string, Partial<Bucket>> = {}
      for (let offset = 0; offset < buckets.length; offset += 100) {
        const data = await api<{ cards: Record<string, Partial<Bucket>> }>('memory.cards.batch', { memory_ids: buckets.slice(offset, offset + 100).map(bucket => bucket.memory_id) })
        if (current !== relationsRequest.current || generation !== session.generation) return
        Object.assign(cards, data.cards)
      }
      onRelations(cards)
    } catch (e) { if (current === relationsRequest.current) setRelationsError(errorMessage(e)) }
    finally { if (current === relationsRequest.current) setRelationsLoading(false) }
  }
  useEffect(() => { readRelations(); return () => { relationsRequest.current++ } }, [bucketKey])
  const sequence = useRef(0)
  const read = async (append = false) => {
    const request = ++sequence.current
    setLoading(true); setError('')
    try {
      const data = await api<{ episodes: typeof episodes; next_cursor?: object }>('episode.list', {
        scope_id: 'primary', states: ['OPEN', 'QUIESCENT'], limit: 20, ...(append && next ? { cursor: next } : {}) })
      if (request !== sequence.current) return
      setEpisodes(previous => append ? [...previous, ...data.episodes] : data.episodes); setNext(data.next_cursor || null)
    } catch (e) { if (request === sequence.current) setError(errorMessage(e)) }
    finally { if (request === sequence.current) setLoading(false) }
  }
  useEffect(() => { read(); return () => { sequence.current++ } }, [])
  return <div className="space-y-6">
    {focus && <button onClick={() => setFocus(null)} className="text-sm text-[#1f3a68]">‹ 全部关系</button>}
    {!focus && <section>
      <h3 className="serif font-bold text-lg text-[#1f3a68] mb-2">正在发生的事</h3>
      {episodes.map(episode => <article key={episode.episode_id} className="py-2 border-b border-[#ece3cc]">
        <div className="flex gap-2 items-center"><span className="text-xs text-[#a8862c]">{episode.state === 'OPEN' ? '进行中' : '静置'}</span><span className="serif font-bold text-[#1f3a68]">{episode.label}</span></div>
        <div className="mt-2 flex flex-wrap gap-3">{episode.source_selections.map((selection, i) => <button key={i} onClick={() => onSource({ selection })} className="text-xs underline text-[#1f3a68]">原文片段 {i + 1}</button>)}</div>
      </article>)}
      {!loading && !episodes.length && !error && <p className="text-xs text-[#9aa3b8]">当前范围没有进行中的事</p>}
      {loading && <p role="status" className="text-xs text-[#a8862c]">读取中…</p>}
      {next && <button disabled={loading} onClick={() => read(true)} className="mt-2 text-xs underline text-[#1f3a68]">加载更多</button>}
      {error && <p role="alert" className="text-xs text-[#c0392b]">{error} <button onClick={() => read()} className="underline">重试</button></p>}
    </section>}
    <section>
      <h3 className="serif font-bold text-lg text-[#1f3a68] mb-2">{selected ? '这组记忆的关系' : '记忆之间的关系'}</h3>
      {relationsLoading && <p role="status" className="text-xs text-[#a8862c]">读取关联中…</p>}
      {relationsError && <p role="alert" className="text-xs text-[#c0392b]">{relationsError} <button onClick={readRelations} className="underline">重试</button></p>}
      {visible.map(edge => <article key={edge.id} className="border-b border-[#ece3cc] py-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <button onClick={() => onOpen(edge.from)} className="serif font-bold text-[#1f3a68] underline">{byId(edge.from)?.original_title || edge.from}</button>
          <span className="text-xs text-[#a8862c]">→ {edge.label} →</span>
          <button onClick={() => onOpen(edge.to)} className="serif font-bold text-[#1f3a68] underline">{byId(edge.to)?.original_title || edge.to}</button>
        </div>
        {edge.reverse && <p className="mt-1 text-xs text-[#8a7a52]">反向含义：{edge.reverse}</p>}
      </article>)}
      {!visible.length && !relationsLoading && !relationsError && <p className="text-sm text-[#9aa3b8]">还没有相连的记忆</p>}
    </section>
  </div>
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
  message_id: string
  provider_message_id: string; normalized_sender: string
  speaker_display?: string; created_at?: string; text?: string | null
  has_thinking?: boolean; thinking_text?: string | null
  thinking_summaries?: string[] | null
  has_tool_content?: boolean
  sequence: number
  memories_referencing?: ConvBinding[]
  attachments?: unknown[]
}
type ConvBinding = { binding_id: string; memory_id: string }
type SourceJump = EvidenceTarget | { conversation_id: string; around_seq?: number; around_message_id?: string }
type SourceHit = SrcMsg & { conversation_id: string; conversation_title?: string; excerpt?: string }

function SourcePage({ jump, clearJump, buckets, onOpen, refreshBuckets }: {
  jump: SourceJump | null
  clearJump: () => void
  buckets: Bucket[]
  onOpen: (id: string) => void
  refreshBuckets: () => void
}) {
  const [convs, setConvs] = useState<ConvItem[]>([])
  const [total, setTotal] = useState(0)
  const [listPage, setListPage] = useState(1)
  const [listLoading, setListLoading] = useState(false)
  const [viewLoading, setViewLoading] = useState(false)
  const [searchLoading, setSearchLoading] = useState(false)
  const loading = listLoading || viewLoading || searchLoading
  const [err, setErr] = useState('')
  // 搜索（她 2026-10-13：原文页加一个搜索——source.search 专项检索）
  const [sq, setSq] = useState('')
  const [sSender, setSSender] = useState('')
  const [sFrom, setSFrom] = useState('')
  const [sTo, setSTo] = useState('')
  const [sHits, setSHits] = useState<SourceHit[] | null>(null)
  const [searchMore, setSearchMore] = useState(false)
  const [searchArgs, setSearchArgs] = useState<Record<string, unknown> | null>(null)
  const [searchOffset, setSearchOffset] = useState(0)
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null)
  // 会话视图
  const [view, setView] = useState<{ conv: ConvItem; msgs: SrcMsg[]; page: number; total: number; totalPages: number; focusSeq?: number; focusMessage?: string } | null>(null)
  const [bindings, setBindings] = useState<ConvBinding[]>([])
  const [importing, setImporting] = useState('')
  const [importBusy, setImportBusy] = useState(false)
  const importLock = useRef(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const listRequest = useRef(0)
  const viewRequest = useRef(0)
  const searchRequest = useRef(0)
  const returnScroll = useRef(0)
  const toolbarRef = useRef<HTMLDivElement>(null)
  const listTotalPages = Math.max(1, Math.ceil(total / SOURCE_PAGE_SIZE))

  const loadConvs = async (page = 1, scroll = false) => {
    const request = ++listRequest.current
    setListLoading(true); setErr('')
    try {
      const read = (p: number) => api<{ conversations: ConvItem[]; total: number }>(
        'source.conversations.list', { limit: SOURCE_PAGE_SIZE, offset: (p - 1) * SOURCE_PAGE_SIZE, titled: true, order: 'first_desc' })
      let d = await read(page)
      const actualPage = Math.max(1, Math.min(page, Math.ceil(d.total / SOURCE_PAGE_SIZE)))
      if (actualPage !== page) d = await read(actualPage)
      if (request !== listRequest.current) return
      setConvs(d.conversations); setTotal(d.total); setListPage(actualPage)
      if (scroll) window.scrollTo({ top: 0 })
    } catch (e) {
      if (request === listRequest.current) setErr(errorMessage(e))
    } finally { if (request === listRequest.current) setListLoading(false) }
  }
  useEffect(() => {
    loadConvs()
    return () => { listRequest.current++; viewRequest.current++; searchRequest.current++ }
  }, [])

  const loadConversation = async (convId: string, page: number, aroundSeq?: number, opening = false, aroundMessage?: string) => {
    const request = ++viewRequest.current
    if (opening && !view) returnScroll.current = window.scrollY
    setErr(''); setViewLoading(true)
    try {
      const [d, bd] = await Promise.all([
        api<{ conversation: ConvItem; messages: SrcMsg[]; page: number; total: number; total_pages: number }>(
          'source.conversation.get', { conversation_id: convId, page, limit: MESSAGE_PAGE_SIZE, ...(aroundMessage ? { around_message_id: aroundMessage } : aroundSeq != null ? { around_seq: aroundSeq } : {}) }),
        opening ? api<{ bindings: ConvBinding[] }>('source.bindings.of_conversation', { conversation_id: convId }) : Promise.resolve(null),
      ])
      if (request !== viewRequest.current) return
      setEvidence(null)
      setView({ conv: d.conversation, msgs: d.messages, page: d.page, total: d.total, totalPages: d.total_pages, focusSeq: aroundSeq, focusMessage: aroundMessage })
      if (bd) setBindings(bd.bindings || [])
    } catch (e) {
      if (request === viewRequest.current) {
        if (e instanceof ApiError && e.code === 'SOURCE_MESSAGE_NOT_CURRENT' && aroundMessage) setEvidence({ message_id: aroundMessage })
        else setErr(errorMessage(e))
      }
    } finally { if (request === viewRequest.current) setViewLoading(false) }
  }
  const openConv = (convId: string, aroundSeq?: number, aroundMessage?: string) => loadConversation(convId, 1, aroundSeq, true, aroundMessage)
  const goMessagePage = (page: number) => { if (view && !viewLoading) loadConversation(view.conv.id, page) }
  const closeConversation = () => {
    viewRequest.current++; setViewLoading(false); setView(null); setBindings([]); setErr('')
    requestAnimationFrame(() => window.scrollTo({ top: returnScroll.current }))
  }
  useEffect(() => {
    if (jump) {
      if ('conversation_id' in jump) openConv(jump.conversation_id, jump.around_seq, jump.around_message_id)
      else { viewRequest.current++; setViewLoading(false); setView(null); setEvidence(jump) }
      clearJump()
    }
  }, [jump])
  useEffect(() => {
    if (!view) return
    const frame = requestAnimationFrame(() => {
      const messages = Array.from(document.querySelectorAll<HTMLElement>('[data-source-seq]'))
      const target = view.focusMessage ? messages.find(m => m.dataset.sourceId === view.focusMessage)
        : view.focusSeq == null ? null : messages.find((m) => Number(m.dataset.sourceSeq) >= view.focusSeq!) || messages[messages.length - 1]
      if (target) {
        const appHeight = parseFloat(getComputedStyle(target).getPropertyValue('--app-header-height')) || 0
        window.scrollTo({ top: Math.max(0, window.scrollY + target.getBoundingClientRect().top - appHeight - (toolbarRef.current?.getBoundingClientRect().height || 0) - 12) })
      } else window.scrollTo({ top: 0 })
    })
    return () => cancelAnimationFrame(frame)
  }, [view])

  const doSearch = async (append = false) => {
    if (loading || importBusy) return
    if (sFrom && sTo && sFrom > sTo) { setErr('开始日期不能晚于结束日期'); return }
    const request = ++searchRequest.current
    setErr(''); setSearchLoading(true)
    try {
      const args: Record<string, unknown> = append && searchArgs ? searchArgs : {
        limit: SOURCE_PAGE_SIZE, ...(sq.trim() ? { query: sq.trim() } : {}),
        ...(sSender ? { senders: [sSender] } : {}), ...(sFrom ? { date_from: sFrom } : {}), ...(sTo ? { date_to: sTo } : {}) }
      const offset = append ? searchOffset : 0
      const d = await api<{ hits: SourceHit[]; has_more: boolean }>('source.search', { ...args, offset })
      if (request === searchRequest.current) {
        setSHits(previous => append ? [...previous || [], ...d.hits] : d.hits)
        setSearchMore(d.has_more); setSearchOffset(offset + d.hits.length); setSearchArgs(args)
        if (!append) window.scrollTo({ top: 0 })
      }
    } catch (e) {
      if (request === searchRequest.current) setErr(errorMessage(e))
    } finally { if (request === searchRequest.current) setSearchLoading(false) }
  }

  const doImport = async (f: File) => {
    if (importLock.current) return
    importLock.current = true
    setImportBusy(true)
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
      loadConvs(1)
      refreshBuckets()
    } catch (e) { setImporting('导入失败：' + errorMessage(e)) } finally { importLock.current = false; setImportBusy(false) }
  }

  const bucketTitle = (mid: string) => buckets.find((b) => b.memory_id === mid)?.original_title || mid

  if (evidence) return <SourceEvidence target={evidence} onClose={() => setEvidence(null)}
    onConversation={(id, messageId) => { setEvidence(null); openConv(id, undefined, messageId) }} />

  /* --- 会话视图（消息流按她的聊天页组件样式：thinking 折叠在上方、
     时间戳在气泡下方、左char右我、75% 宽）--- */
  if (view) {
    // Only join adjacent messages that share an exact binding; never bridge unbound messages.
    const messageRanges: SrcMsg[][] = []
    for (const message of view.msgs) {
      const previous = messageRanges[messageRanges.length - 1]
      const last = previous?.[previous.length - 1]
      const continuesBinding = last?.memories_referencing?.some(reference =>
        message.memories_referencing?.some(next => next.binding_id === reference.binding_id))
      if (continuesBinding) previous.push(message)
      else messageRanges.push([message])
    }
    return (
      <div>
        <div ref={toolbarRef} className="sticky z-20 -mx-3 sm:-mx-4 px-3 sm:px-4 pt-2 pb-2 bg-[#fdfaf2] border-b border-[#e9dcb8]/60" style={{ top: 'var(--app-header-height, 0px)' }}>
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <button onClick={closeConversation} className="shrink-0 py-1 text-sm text-[#1f3a68]">‹ {sHits ? '搜索结果' : '全部会话'}</button>
            <span className="text-[10px] text-[#a8862c] tabular-nums">{view.total} 条 · {fmtTime(view.conv.first_message_at)} → {fmtTime(view.conv.last_message_at)}</span>
          </div>
          <h3 className="mt-1 serif font-bold text-lg text-[#1f3a68] break-words">{view.conv.title}</h3>
          {view.totalPages > 1 && <Pager compact page={view.page} total={view.totalPages} onChange={goMessagePage} disabled={viewLoading} />}
        </div>
        {bindings.length > 0 && (
          <p className="mt-2 text-[11.5px] text-[#7a5d14]">
            这段会话被 {[...new Set(bindings.map((b) => b.memory_id))].length} 个记忆桶绑定 · 金色边条的消息段可点击跳桶
          </p>
        )}
        <div className="mt-4">
          {messageRanges.map(messages => (
            <div key={messages[0].message_id}
              className={`mb-7 space-y-7 ${messages[0].memories_referencing?.length ? 'border-l-2 border-[#d9b44a] pl-3' : ''}`}>
              {messages.map((m) => {
                const references = m.memories_referencing || []
                const isHuman = m.normalized_sender === 'human'
                const isMain = isHuman || m.normalized_sender === 'assistant'
                const time = (fmtTime(m.created_at) || '').slice(5, 16)
                const steps: { kind: 'think' | 'tool'; text: string }[] = []
                if (m.thinking_text) steps.push({ kind: 'think', text: m.thinking_text })
                for (const s of m.thinking_summaries || []) steps.push({ kind: 'think', text: `官方思考摘要：${s}` })
                if (m.has_tool_content) steps.push({ kind: 'tool', text: '工具调用（证据面）' })
                return (
                  <div key={m.message_id} data-source-id={m.message_id} data-source-seq={m.sequence}>
                    {references.map(sb => (
                      <button
                        key={sb.binding_id}
                        onClick={() => onOpen(sb.memory_id)}
                        className="flex items-center gap-1.5 mb-2 px-2 py-1 rounded-full bg-[#f5ead0] text-[#1f3a68] text-[11.5px] active:scale-95 transition"
                      >
                        <span className="w-1.5 h-1.5 rounded-full bg-[#d9b44a]" />
                        已收入记忆《{bucketTitle(sb.memory_id)}》↗
                      </button>
                    ))}
                    {isMain ? (
                      <div className={isHuman
                        ? 'ml-auto flex w-[78%] flex-col items-end pr-[1em]'
                        : 'flex w-[78%] flex-col items-start pl-[1em]'}>
                        {/* thinking：上方折叠，点开才显示（她 2026-10-13 要求） */}
                        {!isHuman && steps.length > 0 && <Thinking steps={steps} />}
                        <div className={isHuman
                          ? 'min-w-0 max-w-full whitespace-pre-wrap [overflow-wrap:anywhere] text-[15px] leading-relaxed text-[#1f1e1d]'
                          : 'min-w-0 max-w-full serif whitespace-pre-wrap [overflow-wrap:anywhere] text-[15.5px] leading-relaxed text-[#1f1e1d]'}>
                          {m.text || '（无用户可见正文）'}
                          {!!m.attachments?.length && <p className="mt-2 text-xs text-[#a8862c]">此消息含 {m.attachments.length} 个附件</p>}
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
          ))}
        </div>
        {view.totalPages > 1 && <Pager page={view.page} total={view.totalPages} onChange={goMessagePage} disabled={viewLoading} />}
        {loading && <p className="mt-3 text-center text-[12px] text-[#a8862c]">加载中…</p>}
        {err && <p className="mt-3 text-[12px] text-[#c0392b]">{err}</p>}
      </div>
    )
  }

  /* --- 搜索结果 --- */
  if (sHits) {
    return (
      <div>
        <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
          <button onClick={() => { viewRequest.current++; searchRequest.current++; setViewLoading(false); setSearchLoading(false); setSHits(null); setErr('') }} className="text-sm text-[#1f3a68]">‹ 返回会话列表</button>
          <span className="text-[11px] text-[#a8862c]">已加载 {sHits.length} 条{searchMore ? '，还有更多' : ''} · 原文专项检索</span>
        </div>
        <div className="border-t border-[#e6dcc3]">
          {sHits.map((h, i) => (
            <button key={h.message_id || i} disabled={viewLoading} onClick={() => openConv(h.conversation_id, undefined, h.message_id)}
              className="block w-full text-left py-2.5 border-b border-[#e6dcc3] hover:bg-[#f5ead0]/50 transition-colors">
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
        {searchMore && <button disabled={loading} onClick={() => doSearch(true)} className="mx-auto my-4 block text-sm underline text-[#1f3a68]">加载更多</button>}
        {loading && <p role="status" className="mt-3 text-xs text-[#a8862c]">读取中…</p>}
        {err && <p role="alert" className="mt-3 text-xs text-[#c0392b]">{err}</p>}
      </div>
    )
  }

  /* --- 会话列表 --- */
  return (
    <div>
      <SourceSearchForm keyword={sq} onKeywordChange={setSq} sender={sSender} onSenderChange={setSSender}
        from={sFrom} onFromChange={setSFrom} to={sTo} onToChange={setSTo}
        disabled={loading || importBusy} onSearch={() => doSearch()} />

      <div className="flex items-baseline mt-3 mb-1">
        <h3 className="serif font-bold text-lg text-[#1f3a68] flex-1">原文会话</h3>
        <button onClick={() => fileRef.current?.click()} disabled={loading || importBusy} className="text-[12px] text-[#a8862c] border-b border-[#a8862c]/50">
          {importBusy ? '导入中…' : '导入'}
        </button>
        <input ref={fileRef} type="file" accept=".json,.zip,.md,.markdown" className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) doImport(f); e.target.value = '' }} />
      </div>
      {importing && <p className="text-[11.5px] text-[#7a5d14]">{importing}</p>}

      <div className="border-t border-[#e6dcc3]">
        {convs.map((c) => (
          <button key={c.id} disabled={loading || importBusy} onClick={() => openConv(c.id)}
            className="block w-full text-left py-2.5 border-b border-[#e6dcc3] hover:bg-[#f5ead0]/50 transition-colors">
            <div className="flex items-baseline gap-2">
              <span className="serif font-bold text-[14px] text-[#1f3a68] truncate flex-1">{c.title || '（未命名会话）'}</span>
              <span className="text-[11px] text-[#a8862c] tabular-nums shrink-0">{(fmtTime(c.first_message_at) || '').slice(0, 10)}</span>
              <span className="text-[11px] text-[#9aa3b8] shrink-0">{c.message_count} 条</span>
            </div>
          </button>
        ))}
        {!convs.length && !loading && (
          <div className="flex flex-col items-center py-16 text-[#8a7a52]">
            <Butterfly className="w-12 h-10 flutter opacity-70" />
            <p className="mt-3 serif italic text-sm">还没有原文</p>
            <p className="mt-1 text-[11px] text-[#b5ad98]">导入 Claude 官方导出的 conversations.json 开始</p>
          </div>
        )}
      </div>
      {listTotalPages > 1 && <Pager page={listPage} total={listTotalPages} onChange={(p) => loadConvs(p, true)} disabled={loading || importBusy} />}
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

type SearchResult = { hits: { memory_id: string; memory_version?: number }[]; next_cursor?: string[] | null; pool_truncated?: boolean }
async function runRealQuery(q: Query, cursor?: string[] | null): Promise<SearchResult> {
  const filters: Record<string, unknown> = {}
  if (q.cats.length) { filters.categories = q.cats; filters.category_match = q.catMode }
  if (q.moods.length) { filters.mood_tags = q.moods; filters.mood_match = q.moodMode }
  if (q.from || q.to) filters.event_date = { ...(q.from ? { from: q.from } : {}), ...(q.to ? { to: q.to } : {}) }
  return api<SearchResult>('memory.recall', { query: '', lexical_terms: terms(q.kw), keyword_match: q.kwMode,
    filters, limit: PAGE_SIZE, ...(cursor ? { cursor } : {}) })
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

function SearchPage({ q, setQ, buckets, byId, onOpen, lastId, moods, onLoaded }: {
  q: Query; setQ: (q: Query) => void
  buckets: Bucket[]; byId: (id: string) => Bucket | undefined
  onOpen: (id: string) => void; lastId: string | null; moods: string[]; onLoaded: (bucket: Bucket) => void
}) {
  const [result, setResult] = useState<{ ids: string[]; nextCursor?: string[] | null; partial: boolean } | null>(null)
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
  const runningRef = useRef(false)
  const run = async (append = false) => {
    if (append && runningRef.current) return
    const my = ++runRef.current
    runningRef.current = true; setRunning(true); setErr('')
    try {
      const r = await runRealQuery(q, append ? result?.nextCursor : null)
      const missing = r.hits.filter(h => !byId(h.memory_id) || (h.memory_version != null && byId(h.memory_id)?.version !== h.memory_version))
      const fresh = await loadBucketsByIds(missing.map(h => h.memory_id))
      if (my !== runRef.current) return
      fresh.forEach(onLoaded)
      setResult(previous => ({ ids: [...new Set([...(append ? previous?.ids || [] : []), ...r.hits.map(h => h.memory_id)])],
        nextCursor: r.next_cursor, partial: !!r.pool_truncated || (append && !!previous?.partial) }))
    } catch (e) {
      if (my === runRef.current) setErr(errorMessage(e))
    } finally { if (my === runRef.current) { runningRef.current = false; setRunning(false) } }
  }
  useEffect(() => {
    runRef.current++; runningRef.current = false
    setResult(null); setErr(''); setRunning(false)
    if (!active) return
    if (q.from && q.to && q.from > q.to) { setErr('开始日期不能晚于结束日期'); return }
    setRunning(true)
    const t = setTimeout(() => run(), 350)
    return () => { clearTimeout(t); runRef.current++; runningRef.current = false }
  }, [q])

  const res = useMemo(() => {
    if (!result) return []
    return result.ids.map((id) => byId(id)).filter(Boolean) as Bucket[]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, buckets])
  const shown = res

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
            {active && !running && <>已加载 <b className="text-lg">{res.length}</b> 段记忆</>}
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
            {!running && !err && result && res.length === 0 && <p className="py-16 text-center serif italic text-sm text-[#a8862c]">{result.partial ? "本次查询范围内未找到匹配，结果不完整" : "没有符合的记忆，换个条件试试"}</p>}
            {result?.nextCursor && <button disabled={running} onClick={() => run(true)} className="mx-auto my-4 block text-sm underline text-[#1f3a68]">{running ? '读取中…' : '加载更多'}</button>}
            {result?.partial && <p role="status" className="py-3 text-center text-xs text-[#a8622c]">检索达到候选范围上限，当前结果不完整；请缩小条件。</p>}
            {err && <button disabled={running} onClick={() => run(!!result?.nextCursor)} className="my-3 text-xs underline text-[#1f3a68]">重试检索</button>}
          </div>
        )}
        {err && <p className="mt-2 text-[12px] text-[#c0392b]">{err}</p>}
      </div>
    </div>
  )
}

/* ---------- I 抽屉（视觉原样；内容=i.items.list 真实条目） ---------- */
function SelfDrawer({ open, onClose, buckets, onOpen }: { open: boolean; onClose: () => void; buckets: Bucket[]; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<{ item_id: string; revision: number; content: string; updated_at?: string; ids: string[]; relationsLoaded?: boolean }[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [relationsBusy, setRelationsBusy] = useState('')
  const request = useRef(0)
  const load = async () => {
    const current = ++request.current
    setLoading(true); setError('')
    try {
      const data = await api<{ items: Omit<(typeof items)[number], 'ids'>[] }>('i.items.list', {})
      if (current === request.current) setItems(data.items.map(item => ({ ...item, ids: [] })))
    } catch (e) { if (current === request.current) setError(errorMessage(e)) }
    finally { if (current === request.current) setLoading(false) }
  }
  useEffect(() => {
    if (open) load()
    return () => { request.current++ }
  }, [open])
  const loadRelations = async (item: (typeof items)[number]) => {
    if (relationsBusy) return
    const current = request.current
    setRelationsBusy(item.item_id); setError('')
    try {
      const result = await api('relations.list', { item_id: item.item_id, revision: item.revision })
      const ids = new Set<string>(); collectMemoryIds(result, ids)
      if (current === request.current) setItems(previous => previous.map(it => it.item_id === item.item_id ? { ...it, ids: [...ids], relationsLoaded: true } : it))
    } catch (e) { if (current === request.current) setError(errorMessage(e)) }
    finally { setRelationsBusy('') }
  }

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
        <button disabled={loading} onClick={load} className="text-xs underline text-[#a8862c]">刷新条目</button>
        {error && <p role="alert" className="text-xs text-[#c0392b]">{error}</p>}
        {loading && <p className="text-center text-[12px] text-[#a8862c] py-10">读取中…</p>}
        {!loading && !error && !items.length && (
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
            {!it.relationsLoaded && <button disabled={!!relationsBusy} onClick={() => loadRelations(it)} className="mt-2 text-xs underline text-[#1f3a68]">{relationsBusy === it.item_id ? '读取关联中…' : '查看关联记忆'}</button>}
            {it.relationsLoaded && !it.ids.length && <p className="mt-2 text-xs text-[#8a7a52]">没有关联记忆</p>}
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
function AdminPage({ bucketsCount, onChanged }: { bucketsCount: number; onChanged: () => void | Promise<void> }) {
  const [policy, setPolicy] = useState<any>(null)
  const [policyRevision, setPolicyRevision] = useState(0)
  const [enabled, setEnabled] = useState(false)
  const [activity, setActivity] = useState<any[]>([])
  const [jobs, setJobs] = useState<any>(null)
  const [outbox, setOutbox] = useState<any>(null)
  const [settings, setSettings] = useState<any>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')

  const inflight = useRef(false)
  const request = useRef(0)
  const activityRequest = useRef(0)
  const [eventTypes, setEventTypes] = useState<string[]>([])
  const [eventType, setEventType] = useState('')
  const canWrite = session.principal === 'qiaosheng'
  const loadAll = async (clearError = true) => {
    const current = ++request.current
    activityRequest.current++
    if (clearError) setErr('')
    const results = await Promise.allSettled([
      api<any>('maintenance.recall_policy.get', {}),
      api<{ events: any[] }>('maintenance.activity.list', { limit: 100 }),
      api<any>('maintenance.jobs.status', {}), api<any>('maintenance.outbox.status', {}),
      api<any>('maintenance.settings.get', {}),
    ])
    if (current !== request.current) return
    const [p, a, j, o, s] = results
    if (p.status === 'fulfilled') { setPolicy(p.value); setPolicyRevision(p.value.revision || 0); setEnabled(p.value.enabled === true) }
    if (a.status === 'fulfilled') { setActivity(a.value.events || []); setEventTypes([...new Set<string>((a.value.events || []).map((event: any) => event.event_type))]); setEventType('') }
    if (j.status === 'fulfilled') setJobs(j.value)
    if (o.status === 'fulfilled') setOutbox(o.value)
    if (s.status === 'fulfilled') setSettings(s.value)
    const failures = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected')
    if (failures.length) setErr(failures.map(result => errorMessage(result.reason)).join('；'))
  }
  useEffect(() => { loadAll(); return () => { request.current++; activityRequest.current++ } }, [])

  const savePolicy = async () => {
    if (!canWrite || inflight.current || !policy) return
    if (enabled && !policy.provider) { setErr('判断服务尚未配置，请先完成服务配置'); return }
    if (!enabled && !window.confirm('确认关闭判断？关闭后检索候选将按页交给char自行判断。')) return
    inflight.current = true; setErr(''); setBusy('保存中…')
    const idem = operationId('ui-judge')
    try {
      await api('maintenance.recall_policy.update', { expected_revision: policyRevision, enabled,
        provider: policy.provider || null, model_id: policy.model_id || null, idempotency_key: idem }, idem)
      await loadAll()
    } catch (e) { await loadAll(false); setErr(errorMessage(e)) }
    finally { inflight.current = false; setBusy('') }
  }
  const warmup = async () => {
    if (!canWrite || inflight.current) return
    inflight.current = true; setErr(''); setNotice(''); setBusy('向量预热中…')
    try {
      await api('maintenance.semantic.warmup', {})
      setNotice('向量预热完成')
    } catch (e) { setBusy(''); setErr(errorMessage(e)) }
    finally { inflight.current = false; setBusy('') }
  }
  const filterActivity = async (type: string) => {
    const current = ++activityRequest.current
    setEventType(type); setErr('')
    try {
      const data = await api<{ events: any[] }>('maintenance.activity.list', { limit: 100, ...(type ? { event_type: type } : {}) })
      if (current === activityRequest.current) setActivity(data.events)
    } catch (e) { if (current === activityRequest.current) setErr(errorMessage(e)) }
  }

  const card = 'rounded-2xl bg-white/70 border border-[#e6dcc3] px-4 pt-3 pb-3.5'
  const modeText = policy ? ({ on: '判断开启（' + (policy.provider || '?') + '）', off: '判断关闭（候选全集分页交付）', unconfigured: '未配置（判断通道阻断：候选正文不直出）' } as any)[policy.mode] || policy.mode : '…'
  const readiness = policy?.provider_readiness || {}

  return (
    <div className="space-y-5">
      <p className="text-[11px] text-[#b5ad98] leading-relaxed">
        打开本页不调用任何模型；召回政策仅宿主用户可修改。共 {bucketsCount} 个记忆桶。
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
            <input type="checkbox" disabled={!canWrite || !!busy || !policy} checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            启用判断
          </label>
          <button disabled={!canWrite || !!busy || !policy} onClick={savePolicy} className="px-3 py-1.5 rounded-full bg-[#1f3a68] text-[#f6e3a8] text-[12.5px] active:scale-95 transition">保存</button>
          <button disabled={!!busy} onClick={() => loadAll()} className="px-3 py-1.5 rounded-full border border-[#d9b44a] text-[#7a5d14] text-[12.5px]">刷新</button>
        </div>
        {policy?.mode === 'unconfigured' && (
          <p className="mt-2 text-[11.5px] text-[#a8622c]">尚未选择判断模式：这不等于"关闭"。配置前候选正文不直出。</p>
        )}
        <p className="mt-2 text-[11px] text-[#b5ad98] leading-relaxed">
          本页显示服务配置与就绪状态。需要更换服务时由维护人员修改配置；密钥不会显示在页面。
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
        <button disabled={!canWrite || !!busy} onClick={warmup} className="mt-2 px-3 py-1.5 rounded-full border border-[#d9b44a] text-[#7a5d14] text-[12.5px]">
          预热语义向量
        </button>
      </section>

      {/* 任务 */}
      <section className={card}>
        <h3 className="serif font-bold text-[#1f3a68]">维护任务</h3>
        <div className="mt-1.5 flex gap-6 text-[13px] text-[#33405f] flex-wrap">
          <span>{outbox?.status === 'historical_readonly' ? '历史事件记录（只读）' : '事件记录'} <b className="serif">{outbox?.total ?? '—'}</b> 条</span>
          {jobs?.import_jobs && Object.entries(jobs.import_jobs).map(([k, v]) => (
            <span key={k}>导入 {k} <b className="serif">{String(v)}</b></span>
          ))}
        </div>
      </section>

      {/* 日志 */}
      <section className={card}>
        <div className="flex items-baseline gap-2">
          <h3 className="serif font-bold text-[#1f3a68] flex-1">审计日志（最近 {activity.length} 条）</h3>
          <select aria-label="审计事件类型" value={eventType} onChange={(e) => filterActivity(e.target.value)} className="text-[12px] text-[#7a5d14] bg-transparent border-b border-dashed border-[#d9b44a]/70 outline-none">
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

      <DeletionRequests onChanged={onChanged} />
      {notice && <p role="status" className="text-xs text-[#a8862c]">{notice}</p>}
      {busy && <p className="text-[12px] text-[#a8862c]">{busy}</p>}
      {err && <p className="text-[12px] text-[#c0392b]">{err}</p>}
    </div>
  )
}
