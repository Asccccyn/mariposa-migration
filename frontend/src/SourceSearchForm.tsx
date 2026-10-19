import { useEffect, useId, useRef, useState } from 'react'
import SourceDateField from './SourceDateField'

const SPEAKERS = [
  { value: '', label: '我们' },
  { value: 'assistant', label: 'char' },
  { value: 'human', label: '宿主用户' },
]

// A shared frame fixes the underline geometry independently of native input metrics.
const controlFrame = 'flex h-8 min-w-0 items-center border-b border-dashed border-[#cdbb88] focus-within:border-solid focus-within:border-[#1f3a68]'

function SpeakerField({ value, onChange, disabled }: {
  value: string; onChange: (value: string) => void; disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const items = useRef<(HTMLButtonElement | null)[]>([])
  const menuId = useId()
  const selected = Math.max(0, SPEAKERS.findIndex((item) => item.value === value))
  const close = (restoreFocus = true) => {
    setOpen(false)
    if (restoreFocus) trigger.current?.focus()
  }
  useEffect(() => {
    if (!open) return
    items.current[selected]?.focus()
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open, selected])
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])

  return (
    <div ref={root} className="relative min-w-0" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
    }}>
      <button ref={trigger} type="button" aria-label="说话人" aria-haspopup="listbox" aria-expanded={open}
        aria-controls={open ? menuId : undefined} disabled={disabled}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setOpen(true) }
          if (event.key === 'Escape') close()
        }}
        className={`${controlFrame} w-full justify-between gap-1 bg-transparent px-0 text-[13px] leading-none text-[#7a5d14] outline-none disabled:opacity-40`}>
        <span>{SPEAKERS[selected].label}</span>
        <svg aria-hidden="true" viewBox="0 0 16 16" className={`h-3 w-3 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" strokeWidth="1.4"><path d="m4 6 4 4 4-4" /></svg>
      </button>
      {open && (
        <div id={menuId} role="listbox" aria-label="选择说话人"
          className="absolute right-0 top-full z-40 mt-1 w-[112px] rounded-xl border border-[#e6dcc3] bg-[#fffdf7] p-1 shadow-[0_6px_20px_rgba(31,58,104,0.12)]">
          {SPEAKERS.map((item, index) => (
            <button key={item.value} ref={(node) => { items.current[index] = node }} type="button" role="option"
              aria-selected={item.value === value} tabIndex={item.value === value ? 0 : -1}
              onClick={() => { onChange(item.value); close() }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() }
                else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                  event.preventDefault()
                  const next = event.key === 'Home' ? 0 : event.key === 'End' ? SPEAKERS.length - 1
                    : (index + (event.key === 'ArrowDown' ? 1 : -1) + SPEAKERS.length) % SPEAKERS.length
                  items.current[next]?.focus()
                }
              }}
              className={`flex h-8 w-full items-center gap-2 rounded-lg px-2 text-left text-[12px] outline-none focus:bg-[#f5ead0] ${item.value === value ? 'text-[#1f3a68] bg-[#f5ead0]/60' : 'text-[#6d674f] hover:bg-[#f5ead0]/50'}`}>
              <span className="w-3 text-[11px]" aria-hidden="true">{item.value === value ? '✓' : ''}</span>
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

type Props = {
  keyword: string; onKeywordChange: (value: string) => void
  sender: string; onSenderChange: (value: string) => void
  from: string; onFromChange: (value: string) => void
  to: string; onToChange: (value: string) => void
  disabled: boolean; onSearch: () => void
}

export default function SourceSearchForm({ keyword, onKeywordChange, sender, onSenderChange,
  from, onFromChange, to, onToChange, disabled, onSearch }: Props) {
  return (
    <form aria-label="原文搜索条件" className="relative max-w-lg" onSubmit={(event) => { event.preventDefault(); if (!disabled) onSearch() }}>
      <div className="grid grid-cols-2 gap-3">
        <SourceDateField label="开始日期" value={from} onChange={onFromChange} max={to || undefined} disabled={disabled} />
        <SourceDateField label="结束日期" value={to} onChange={onToChange} min={from || undefined} align="right" disabled={disabled} />
      </div>
      <div className="mt-1 grid grid-cols-[28px_minmax(0,1fr)_84px_28px] items-end gap-2.5">
        <h4 aria-label="搜索原文" className="flex h-8 flex-col justify-end pb-px text-[10.5px] leading-[14px] tracking-[0.12em] text-[#a8862c]">
          <span>搜索</span><span>原文</span>
        </h4>
        <div className={`${controlFrame} overflow-hidden`}>
          {/* Keep the real input at 16px to avoid iOS focus zoom; its text fits the compact frame at 13px. */}
          <input type="search" aria-label="原文关键词" value={keyword} onChange={(event) => onKeywordChange(event.target.value)}
            placeholder="全文关键词…" disabled={disabled} enterKeyHint="search"
            className="h-8 shrink-0 appearance-none border-0 bg-transparent p-0 text-base leading-5 text-[#1f3a68] outline-none placeholder:text-[#b3a584] placeholder:italic disabled:opacity-40 [&::-webkit-search-cancel-button]:appearance-none"
            style={{ width: '123.076924%', transform: 'scale(0.8125)', transformOrigin: 'left center' }} />
        </div>
        <SpeakerField value={sender} onChange={onSenderChange} disabled={disabled} />
        <button type="submit" disabled={disabled} aria-label="搜索原文" title="搜索原文"
          className="flex h-8 w-7 items-center justify-center rounded-md text-[#1f3a68] outline-none hover:bg-[#f5ead0]/60 focus-visible:ring-1 focus-visible:ring-[#d9b44a] disabled:opacity-35">
          <svg aria-hidden="true" viewBox="0 0 24 24" className="h-[18px] w-[18px]" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 4.5 4.5" /></svg>
        </button>
      </div>
    </form>
  )
}
