import { useEffect, useRef, useState } from 'react'
import { api, errorMessage, operationId, session } from './api'

type Request = {
  request_id: string; memory_id: string; human_reason: string; status: string
  rejection_reason?: string | null; created_at: string; submitted_by: string
}
const labels: Record<string, string> = { pending: '待确认', approved: '已批准', rejected: '已拒绝', withdrawn: '已撤回', superseded: '已由其他操作处理' }

export default function DeletionRequests({ memoryId, onChanged, onDeleted, compact = false }: {
  memoryId?: string; onChanged: () => void | Promise<void>; onDeleted?: (id: string) => void; compact?: boolean
}) {
  const [requests, setRequests] = useState<Request[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const sequence = useRef(0)
  const inflight = useRef(false)
  const load = async () => {
    const request = ++sequence.current
    setLoading(true)
    try {
      const data = await api<{ requests: Request[] }>('memory.deletion.list', memoryId ? { memory_id: memoryId } : {})
      if (request === sequence.current) setRequests(data.requests)
    } catch (e) { if (request === sequence.current) setError(errorMessage(e)) }
    finally { if (request === sequence.current) setLoading(false) }
  }
  useEffect(() => { load(); return () => { sequence.current++ } }, [memoryId])
  const act = async (request: Request, decision: 'withdraw' | 'approve' | 'reject') => {
    if (inflight.current) return
    if (decision === 'approve' && !window.confirm('批准后将永久删除这个记忆，无法恢复。确认批准？')) return
    const reason = (reasons[request.request_id] || '').trim()
    if (decision === 'reject' && !reason) { setError('拒绝申请时请填写理由'); return }
    inflight.current = true; setBusy(request.request_id); setError('')
    try {
      if (decision === 'withdraw') await api('memory.deletion.withdraw', { request_id: request.request_id })
      else await api('memory.deletion.decide', { request_id: request.request_id, decision,
        ...(decision === 'reject' ? { rejection_reason: reason } : {}), operation_id: operationId('ui-decision') })
      await load()
      if (decision === 'approve' && onDeleted) onDeleted(request.memory_id)
      else await onChanged()
    } catch (e) { setError(errorMessage(e)) }
    finally { inflight.current = false; setBusy('') }
  }
  return <section className={compact ? 'mt-4 border-t border-[#ece3cc] pt-3' : 'rounded-2xl bg-white/70 border border-[#e6dcc3] px-4 py-3'}>
    <div className="flex items-center justify-between gap-2">
      <h3 className={`${compact ? 'text-xs' : 'serif font-bold'} text-[#1f3a68]`}>删除申请</h3>
      <button disabled={loading || !!busy} onClick={() => { setError(''); load() }} className="text-xs underline text-[#a8862c]">刷新</button>
    </div>
    {loading && <p className="mt-2 text-xs text-[#8a7a52]" role="status">读取中…</p>}
    {requests.map(request => <div key={request.request_id} className="mt-2 border-t border-[#ece3cc] pt-2 text-xs text-[#4a5675]">
      <p>{labels[request.status] || request.status} · {request.created_at.slice(0, 10)}</p>
      {!memoryId && <p className="break-all text-[11px] text-[#8a7a52]">{request.memory_id}</p>}
      <p className="mt-1 whitespace-pre-wrap">{request.human_reason}</p>
      {request.rejection_reason && <p className="mt-1">拒绝理由：{request.rejection_reason}</p>}
      {request.status === 'pending' && <div className="mt-2 flex flex-wrap items-center gap-3">
        {request.submitted_by === session.principal && <button disabled={!!busy} onClick={() => act(request, 'withdraw')} className="underline text-[#8a7a52]">撤回申请</button>}
        {session.principal === 'jiaming' && <>
          <button disabled={!!busy} onClick={() => act(request, 'approve')} className="underline text-[#b3423a]">批准删除</button>
          <input aria-label="拒绝理由" disabled={!!busy} value={reasons[request.request_id] || ''} onChange={e => setReasons({ ...reasons, [request.request_id]: e.target.value })} placeholder="拒绝理由" className="min-w-0 flex-1 border-b border-dashed border-[#d9b44a] bg-transparent p-1" />
          <button disabled={!!busy || !(reasons[request.request_id] || '').trim()} onClick={() => act(request, 'reject')} className="underline text-[#1f3a68]">拒绝</button>
        </>}
      </div>}
    </div>)}
    {!loading && !requests.length && !error && <p className="mt-2 text-xs text-[#9aa3b8]">暂无申请</p>}
    {requests.length >= 200 && <p className="mt-2 text-xs text-[#8a7a52]">显示最近 200 条申请</p>}
    {error && <p role="alert" className="mt-2 text-xs text-[#c0392b]">{error}</p>}
  </section>
}
