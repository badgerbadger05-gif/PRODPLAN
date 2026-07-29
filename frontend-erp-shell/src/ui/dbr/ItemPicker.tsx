import { useEffect, useId, useRef, useState } from 'react'
import type { BomItem } from '../../domain/specification'
import { searchSpecificationItems } from '../../services/specification'

export type PickedItem = {
  item_id: number
  item_code: string
  item_name: string
  item_article?: string | null
}

type Props = {
  value: PickedItem | null
  onChange: (item: PickedItem | null) => void
  placeholder?: string
  disabled?: boolean
}

function itemLabel(item: PickedItem) {
  return [item.item_article, item.item_name].filter(Boolean).join(' · ') || item.item_code
}

// Autocomplete over the shared item catalog (v1/specification/search).
// Shows article + name; keeps the picked item once chosen until cleared.
export function ItemPicker({ value, onChange, placeholder, disabled }: Props) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<BomItem[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const boxRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listboxId = useId()

  useEffect(() => {
    const text = query.trim()
    if (!open || text.length < 2) {
      setResults([])
      return
    }
    let cancelled = false
    setLoading(true)
    const handle = window.setTimeout(async () => {
      try {
        const res = await searchSpecificationItems({ q: text, limit: 20 })
        if (!cancelled) {
          setResults(res.items)
          setActiveIndex(-1)
        }
      } catch {
        if (!cancelled) setResults([])
      } finally {
        if (!cancelled) setLoading(false)
      }
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [query, open])

  useEffect(() => {
    function onDocClick(event: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  function pick(item: BomItem) {
    onChange({
      item_id: item.item_id,
      item_code: item.item_code,
      item_name: item.item_name,
      item_article: item.item_article ?? null,
    })
    setQuery('')
    setResults([])
    setOpen(false)
  }

  if (value) {
    return (
      <div className="dbrPicker" ref={boxRef}>
        <div className="dbrPickerChosen">
          <div className="dbrPickerChosenMeta">
            <strong>{value.item_name}</strong>
            <span>{value.item_article || value.item_code}</span>
          </div>
          {!disabled && (
            <button type="button" className="dbrPickerClear" onClick={() => onChange(null)} title="Очистить">
              ✕
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="dbrPicker" ref={boxRef}>
      <input
        ref={inputRef}
        role="combobox"
        aria-label={placeholder ?? 'Номенклатура'}
        aria-expanded={open && query.trim().length >= 2}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-activedescendant={activeIndex >= 0 ? `${listboxId}-${activeIndex}` : undefined}
        value={query}
        disabled={disabled}
        placeholder={placeholder ?? 'Артикул, код или название'}
        onChange={(e) => {
          setQuery(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault()
            setOpen(false)
            setActiveIndex(-1)
            inputRef.current?.focus()
          } else if (e.key === 'ArrowDown' && results.length) {
            e.preventDefault()
            setActiveIndex((prev) => Math.min(prev + 1, results.length - 1))
          } else if (e.key === 'ArrowUp' && results.length) {
            e.preventDefault()
            setActiveIndex((prev) => Math.max(prev - 1, 0))
          } else if (e.key === 'Enter' && activeIndex >= 0) {
            e.preventDefault()
            pick(results[activeIndex])
          }
        }}
      />
      {open && query.trim().length >= 2 && (
        <div id={listboxId} className="dbrPickerDrop" role="listbox" aria-label="Результаты поиска номенклатуры">
          {loading && <div className="dbrPickerEmpty">Поиск…</div>}
          {!loading && !results.length && <div className="dbrPickerEmpty">Ничего не найдено</div>}
          {results.map((item) => (
            <button
              type="button"
              id={`${listboxId}-${results.indexOf(item)}`}
              role="option"
              aria-selected={activeIndex === results.indexOf(item)}
              key={item.item_id}
              className="dbrPickerOption"
              onClick={() => pick(item)}
            >
              <strong>{item.item_name}</strong>
              <span>{itemLabel({ item_id: item.item_id, item_code: item.item_code, item_name: item.item_name, item_article: item.item_article })}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
