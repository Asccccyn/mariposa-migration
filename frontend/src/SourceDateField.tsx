import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'

type Props = {
  label: string
  value: string
  onChange: (value: string) => void
  align?: 'left' | 'right'
  min?: string
  max?: string
  disabled?: boolean
}
type Day = { year: number; month: number; day: number }
type Mode = 'days' | 'months' | 'years'

const daysInMonth = (year: number, month: number) => [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
const dateKey = ({ year, month, day }: Day) => `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
function parseDate(value?: string): Day | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, month, day] = value.split('-').map(Number)
  return year >= 1 && year <= 9999 && month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth(year, month) ? { year, month, day } : null
}
function localDate(day: Day) {
  const date = new Date(0)
  date.setHours(12, 0, 0, 0)
  date.setFullYear(day.year, day.month - 1, day.day)
  return date
}
const fromLocalDate = (date: Date): Day => ({ year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate() })
const today = () => fromLocalDate(new Date())
const yearBlock = (year: number) => Math.floor((year - 1) / 12) * 12 + 1
function addMonths(day: Day, amount: number): Day {
  const index = (day.year - 1) * 12 + day.month - 1 + amount
  const year = Math.floor(index / 12) + 1
  const month = ((index % 12) + 12) % 12 + 1
  return { year, month, day: Math.min(day.day, daysInMonth(year, month)) }
}

export default function SourceDateField({ label, value, onChange, align = 'left', min, max, disabled = false }: Props) {
  const id = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const focusGrid = useRef(false)
  const selected = parseDate(value)
  const lower = parseDate(min) || { year: 1, month: 1, day: 1 }
  const upper = parseDate(max) || { year: 9999, month: 12, day: 31 }
  const lowerKey = dateKey(lower)
  const upperKey = dateKey(upper)
  const validRange = lowerKey <= upperKey
  const clamp = (day: Day): Day => {
    if (day.year < 1 || dateKey(day) < lowerKey) return lower
    if (day.year > 9999 || dateKey(day) > upperKey) return upper
    return day
  }
  const allowed = (day: Day) => validRange && day.year >= 1 && day.year <= 9999 && dateKey(day) >= lowerKey && dateKey(day) <= upperKey
  const monthAllowed = (year: number, month: number) => validRange && year >= 1 && year <= 9999
    && dateKey({ year, month, day: daysInMonth(year, month) }) >= lowerKey && dateKey({ year, month, day: 1 }) <= upperKey
  const yearAllowed = (year: number) => validRange && year >= lower.year && year <= upper.year
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<Mode>('days')
  const [active, setActive] = useState<Day>(() => clamp(selected || today()))
  const [yearStart, setYearStart] = useState(() => yearBlock(active.year))
  const [shift, setShift] = useState(0)
  const activeKey = dateKey(active)
  const currentToday = today()
  const todayKey = dateKey(currentToday)
  const selectedKey = selected ? dateKey(selected) : ''

  const close = (restoreFocus = true) => {
    setOpen(false)
    if (restoreFocus) triggerRef.current?.focus({ preventScroll: true })
  }
  const show = () => {
    if (disabled) return
    const next = clamp(selected || today())
    setActive(next); setYearStart(yearBlock(next.year)); setMode('days'); setShift(0)
    focusGrid.current = true
    setOpen(true)
  }
  const choose = (day: Day) => {
    if (!allowed(day)) return
    onChange(dateKey(day)); close()
  }
  const moveTo = (day: Day, focus = false) => {
    focusGrid.current = focus
    setActive(clamp(day))
  }

  useEffect(() => {
    if (!open) return
    const outside = (event: Event) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('focusin', outside)
    }
  }, [open])
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  useLayoutEffect(() => {
    if (!open || !focusGrid.current) return
    focusGrid.current = false
    const target = popupRef.current?.querySelector<HTMLButtonElement>('[data-calendar-focus="true"]:not(:disabled)')
      || popupRef.current?.querySelector<HTMLButtonElement>('[data-calendar-choice]:not(:disabled)')
    target?.focus({ preventScroll: true })
  }, [open, mode, activeKey, yearStart])
  useLayoutEffect(() => {
    if (!open) return
    const position = () => {
      const rect = popupRef.current?.getBoundingClientRect()
      if (!rect) return
      const viewport = window.visualViewport
      const left = (viewport?.offsetLeft || 0) + 12
      const right = left + (viewport?.width || window.innerWidth) - 24
      const correction = rect.left < left ? left - rect.left : rect.right > right ? right - rect.right : 0
      if (Math.abs(correction) > 0.5) setShift((previous) => previous + correction)
    }
    position()
    window.addEventListener('resize', position)
    window.visualViewport?.addEventListener('resize', position)
    return () => {
      window.removeEventListener('resize', position)
      window.visualViewport?.removeEventListener('resize', position)
    }
  }, [open])

  const onDayKey = (event: KeyboardEvent<HTMLButtonElement>, day: Day) => {
    const offsets: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }
    let next: Day
    if (event.key === 'PageUp' || event.key === 'PageDown') {
      next = addMonths(day, (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1))
    } else {
      const weekday = localDate(day).getDay()
      const offset = event.key === 'Home' ? -weekday : event.key === 'End' ? 6 - weekday : offsets[event.key]
      if (offset === undefined) return
      const date = localDate(day)
      date.setDate(date.getDate() + offset)
      next = fromLocalDate(date)
    }
    event.preventDefault()
    moveTo(next, true)
  }
  const changeMode = (next: Mode) => {
    if (next === 'years') setYearStart(yearBlock(active.year))
    focusGrid.current = true
    setMode(next)
  }
  const canNavigate = (direction: -1 | 1) => {
    if (mode === 'years') return validRange && yearStart + direction * 12 <= upper.year && yearStart + direction * 12 + 11 >= lower.year
    if (mode === 'months') return yearAllowed(active.year + direction)
    const next = addMonths(active, direction)
    return monthAllowed(next.year, next.month)
  }
  const navigate = (direction: -1 | 1) => {
    if (!canNavigate(direction)) return
    if (mode === 'years') setYearStart((start) => start + direction * 12)
    else moveTo(addMonths(active, direction * (mode === 'months' ? 12 : 1)))
  }
  const firstWeekday = localDate({ ...active, day: 1 }).getDay()
  const cellCount = Math.ceil((firstWeekday + daysInMonth(active.year, active.month)) / 7) * 7
  const smallButton = 'flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[#1f3a68] hover:bg-[#f3e6c2]/70 disabled:opacity-25 focus-visible:outline focus-visible:outline-1 focus-visible:outline-[#a8862c]'
  const choiceStyle = 'rounded-md text-[12px] tabular-nums transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-[-2px] focus-visible:outline-[#a8862c] disabled:text-[#c9c3b5] disabled:cursor-default'

  return (
    <div ref={rootRef} className={`relative min-w-0 ${open ? 'z-40' : ''}`} onKeyDown={(event) => {
      if (open && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() }
    }}>
      <button ref={triggerRef} type="button" aria-label={label} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined}
        disabled={disabled} onClick={() => open ? close() : show()}
        onKeyDown={(event) => { if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); show() } }}
        className="block w-full min-w-0 bg-transparent p-0 text-left disabled:opacity-40 focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-4 focus-visible:outline-[#a8862c]">
        <span className="block text-[10px] leading-4 text-[#a8862c]">{label}</span>
        <span className="flex min-w-0 items-center justify-between gap-1 py-1 text-[12px] leading-5 tabular-nums text-[#7a5d14]">
          <span className={`truncate ${selected ? '' : 'text-[#b9ac8d]'}`}>{selected ? selectedKey : '选择日期'}</span>
          <svg aria-hidden="true" viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0 text-[#a8862c]" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="5" width="18" height="16" rx="2" /><path d="M16 3v4M8 3v4M3 11h18M8 15h1M15 15h1" />
          </svg>
        </span>
      </button>
      {open && (
        <div ref={popupRef} id={id} role="dialog" aria-label={`${label}日历`}
          className={`absolute top-full mt-1 w-64 max-w-[calc(100vw-24px)] rounded-xl border border-[#e6dcc3] bg-[#fffdf7] p-3 shadow-[0_8px_28px_rgba(31,58,104,0.16)] ${align === 'right' ? 'right-0' : 'left-0'}`}
          style={{ transform: `translateX(${shift}px)` }}>
          <div className="mb-2 flex items-center justify-between gap-1">
            <button type="button" aria-label={mode === 'years' ? '前12年' : mode === 'months' ? '上一年' : '上一月'} disabled={!canNavigate(-1)} onClick={() => navigate(-1)} className={smallButton}>
              <svg aria-hidden="true" viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="m14 6-6 6 6 6" /></svg>
            </button>
            <div className="flex min-w-0 items-center justify-center gap-1 text-[12px] font-medium text-[#1f3a68]" aria-live="polite">
              <button type="button" aria-label="选择年份" aria-expanded={mode === 'years'} onClick={() => changeMode(mode === 'years' ? 'days' : 'years')} className="rounded px-1 py-1 hover:bg-[#f3e6c2]/70">{mode === 'years' ? `${yearStart}–${Math.min(9999, yearStart + 11)}` : `${active.year}年`}</button>
              {mode !== 'years' && <button type="button" aria-label="选择月份" aria-expanded={mode === 'months'} onClick={() => changeMode(mode === 'months' ? 'days' : 'months')} className="rounded px-1 py-1 hover:bg-[#f3e6c2]/70">{active.month}月</button>}
            </div>
            <button type="button" aria-label={mode === 'years' ? '后12年' : mode === 'months' ? '下一年' : '下一月'} disabled={!canNavigate(1)} onClick={() => navigate(1)} className={smallButton}>
              <svg aria-hidden="true" viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="m10 6 6 6-6 6" /></svg>
            </button>
          </div>
          {mode === 'days' ? (
            <div role="grid" aria-label={`${active.year}年${active.month}月`} className="mx-auto max-w-56">
              <div role="row" className="grid grid-cols-7">
                {['日', '一', '二', '三', '四', '五', '六'].map((day) => <span key={day} role="columnheader" className="h-6 text-center text-[10px] leading-6 text-[#a8862c]">{day}</span>)}
              </div>
              {Array.from({ length: cellCount / 7 }, (_, row) => (
                <div key={row} role="row" className="grid grid-cols-7">
                  {Array.from({ length: 7 }, (_, column) => {
                    const number = row * 7 + column - firstWeekday + 1
                    if (number < 1 || number > daysInMonth(active.year, active.month)) return <span key={column} role="gridcell" className="h-8" />
                    const day = { year: active.year, month: active.month, day: number }
                    const key = dateKey(day)
                    const isSelected = key === selectedKey
                    return <button key={column} type="button" role="gridcell" aria-label={`${day.year}年${day.month}月${day.day}日`} aria-selected={isSelected} aria-current={key === todayKey ? 'date' : undefined}
                      data-calendar-choice data-calendar-focus={key === activeKey} tabIndex={key === activeKey ? 0 : -1} disabled={!allowed(day)}
                      onClick={() => choose(day)} onKeyDown={(event) => onDayKey(event, day)}
                      className={`h-8 ${choiceStyle} ${isSelected ? 'bg-[#1f3a68] text-[#f6e3a8]' : key === todayKey ? 'font-bold text-[#8a6819] enabled:hover:bg-[#f3e6c2]/70' : 'text-[#33405f] enabled:hover:bg-[#f3e6c2]/70'}`}>{number}</button>
                  })}
                </div>
              ))}
            </div>
          ) : mode === 'months' ? (
            <div className="grid grid-cols-3 gap-1" aria-label="月份">
              {Array.from({ length: 12 }, (_, index) => index + 1).map((month) => <button key={month} type="button" data-calendar-choice data-calendar-focus={month === active.month} disabled={!monthAllowed(active.year, month)}
                onClick={() => { moveTo({ ...active, month, day: Math.min(active.day, daysInMonth(active.year, month)) }, true); setMode('days') }}
                className={`h-8 ${choiceStyle} ${month === active.month ? 'bg-[#f3e6c2]/70 text-[#7a5d14]' : 'text-[#33405f] enabled:hover:bg-[#f3e6c2]/70'}`}>{month}月</button>)}
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-1" aria-label="年份">
              {Array.from({ length: Math.min(12, 10000 - yearStart) }, (_, index) => yearStart + index).map((year) => <button key={year} type="button" data-calendar-choice data-calendar-focus={year === active.year} disabled={!yearAllowed(year)}
                onClick={() => { moveTo({ ...active, year, day: Math.min(active.day, daysInMonth(year, active.month)) }, true); setMode('months') }}
                className={`h-8 ${choiceStyle} ${year === active.year ? 'bg-[#f3e6c2]/70 text-[#7a5d14]' : 'text-[#33405f] enabled:hover:bg-[#f3e6c2]/70'}`}>{year}</button>)}
            </div>
          )}
          <div className="mt-2 flex items-center justify-between border-t border-[#ece3cc] pt-1.5">
            <button type="button" disabled={!allowed(currentToday)} onClick={() => choose(currentToday)} className="rounded px-1 py-1 text-[11px] text-[#1f3a68] hover:bg-[#f3e6c2]/70 disabled:text-[#c9c3b5]">今天</button>
            <button type="button" onClick={() => { onChange(''); close() }} className="rounded px-1 py-1 text-[11px] text-[#8a7a52] hover:bg-[#f3e6c2]/70">清除</button>
          </div>
        </div>
      )}
    </div>
  )
}
