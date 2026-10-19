import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './api'

export type SourceSelection = {
  conversation_id: string
  members: { source_message_id: string; content_hash?: string }[]
  start_char_offset?: number; end_char_offset?: number
}
export type EvidenceTarget = { memory_id: string } | { selection: SourceSelection } | { message_id: string }
type EvidenceMessage = {
  message_id: string; text: string; speaker_display?: string; normalized_sender: string
  created_at?: string; version_drift?: unknown
}
type EvidenceRange = {
  binding_id?: string; status?: string; reason?: string; version_drift?: unknown
  conversation?: { id: string; title: string }; messages?: EvidenceMessage[]; members?: EvidenceMessage[]
  drifted_members?: string[]
}

export default function SourceEvidence({ target, onClose, onConversation }: {
  target: EvidenceTarget; onClose: () => void; onConversation: (id: string, messageId?: string) => void
}) {
  const [ranges, setRanges] = useState<EvidenceRange[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const sequence = useRef(0)
  const read = async () => {
    const request = ++sequence.current
    setLoading(true); setError('')
    try {
      let next: EvidenceRange[]
      if ('memory_id' in target) next = (await api<{ ranges: EvidenceRange[] }>('source.memory.open', target)).ranges
      else if ('selection' in target) next = [await api<EvidenceRange>('source.selection.open', target)]
      else {
        const data = await api<{ conversation: EvidenceRange['conversation']; message: EvidenceMessage }>('source.message.get', { ...target, context: 0 })
        next = [{ conversation: data.conversation, messages: [data.message], version_drift: true }]
      }
      if (request === sequence.current) setRanges(next)
    } catch (e) { if (request === sequence.current) setError(errorMessage(e)) }
    finally { if (request === sequence.current) setLoading(false) }
  }
  useEffect(() => { read(); window.scrollTo({ top: 0 }); return () => { sequence.current++ } }, [target])
  return <div>
    <button onClick={onClose} className="text-sm text-[#1f3a68]">‹ 返回原文</button>
    <h3 className="mt-3 serif text-lg font-bold text-[#1f3a68]">绑定的原文片段</h3>
    <p className="mt-1 text-xs text-[#8a7a52]">按绑定读取全部片段；只展示选中的内容。</p>
    {loading && <p role="status" className="mt-4 text-xs text-[#a8862c]">读取中…</p>}
    {error && <p role="alert" className="mt-4 text-xs text-[#c0392b]">{error} <button onClick={read} className="underline">重试</button></p>}
    {!loading && !error && !ranges.length && <p className="mt-4 text-sm text-[#8a7a52]">这个记忆还没有绑定原文。</p>}
    {ranges.map((range, index) => <section key={range.binding_id || index} className="mt-5 border-t border-[#e6dcc3] pt-3">
      <h4 className="serif font-bold text-[#1f3a68]">片段 {index + 1} · {range.conversation?.title || '来源'}</h4>
      {range.status === 'unresolvable' && <p className="mt-2 text-sm text-[#a8622c]">这处绑定暂时无法解析：{range.reason || '来源不可用'}。其他片段仍可阅读。</p>}
      {(!!range.version_drift || !!range.drifted_members?.length) && <p className="mt-2 text-xs text-[#a8622c]">来源已有其他修订，当前展示绑定的证据版本。</p>}
      <div className={`mt-3 space-y-3 ${range.binding_id ? 'border-l-2 border-[#d9b44a] pl-3' : ''}`}>
        {(range.messages || range.members || []).map(message => <article key={message.message_id}>
          <p className="text-[11px] text-[#a8862c]">{message.speaker_display || message.normalized_sender} · {message.created_at || '时间未知'}</p>
          {!!message.version_drift && <p className="text-xs text-[#a8622c]">此消息已有新修订</p>}
          <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-[#33405f]">{message.text || '（无正文）'}</p>
        </article>)}
      </div>
      {range.conversation && <button onClick={() => onConversation(range.conversation!.id)} className="mt-3 text-xs underline text-[#1f3a68]">查看这段会话的当前完整内容</button>}
    </section>)}
  </div>
}
