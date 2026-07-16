import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import type {
  DbrAssemblyRate,
  DbrCategoryRisk,
  DbrSettings,
  DbrSettingsUpdate,
} from '../../domain/dbr'
import type { ProductionResource } from '../../domain/resources'
import { qty } from '../../lib/format'
import {
  deleteDbrAssemblyRate,
  getDbrSettings,
  listDbrAssemblyRates,
  listDbrCategoryRisks,
  replaceDbrCategoryRisks,
  updateDbrSettings,
  upsertDbrAssemblyRate,
} from '../../services/dbr'
import { listResources } from '../../services/resources'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { DbrNav } from '../dbr/DbrNav'
import { ItemPicker } from '../dbr/ItemPicker'
import type { PickedItem } from '../dbr/ItemPicker'

// Local editable copy of the settings singleton. Kept as strings-or-numbers is
// avoided — everything numeric is normalized to a number on save.
type SettingsForm = Omit<DbrSettings, 'id'>

function toForm(s: DbrSettings): SettingsForm {
  return {
    frozen_days: s.frozen_days,
    gate_horizon_workdays: s.gate_horizon_workdays,
    shelf_threshold_qty: Number(s.shelf_threshold_qty ?? 0),
    rt_machining_days: s.rt_machining_days,
    rt_welding_days: s.rt_welding_days,
    rt_painting_days: s.rt_painting_days,
    batch_days_turning: s.batch_days_turning,
    batch_days_bending: s.batch_days_bending,
    batch_days_welding: s.batch_days_welding,
    batch_days_paint_black: s.batch_days_paint_black,
    batch_days_paint_color: s.batch_days_paint_color,
    feeder_chain_enabled: s.feeder_chain_enabled,
    feeder_load_horizon_weeks: s.feeder_load_horizon_weeks,
    w2_warehouse_ref1c: s.w2_warehouse_ref1c ?? '',
    w3_warehouse_ref1c: s.w3_warehouse_ref1c ?? '',
    w4_warehouse_ref1c: s.w4_warehouse_ref1c ?? '',
    fastener_categories: s.fastener_categories ?? [],
  }
}

const emptyRateDraft = { resource_id: '', qty_per_capacity: '' }

export function DbrSettingsPage() {
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [rates, setRates] = useState<DbrAssemblyRate[]>([])
  const [risks, setRisks] = useState<DbrCategoryRisk[]>([])
  const [resources, setResources] = useState<ProductionResource[]>([])

  const [rateDraft, setRateDraft] = useState(emptyRateDraft)
  const [rateItem, setRateItem] = useState<PickedItem | null>(null)

  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const [settings, rateRows, riskRows, resourceRows] = await Promise.all([
        getDbrSettings(),
        listDbrAssemblyRates(),
        listDbrCategoryRisks(),
        listResources(),
      ])
      setForm(toForm(settings))
      setRates(rateRows)
      setRisks(riskRows)
      setResources(resourceRows)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  function patch(next: Partial<SettingsForm>) {
    setForm((prev) => (prev ? { ...prev, ...next } : prev))
  }

  async function saveSettings() {
    if (!form) return
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const payload: DbrSettingsUpdate = {
        ...form,
        shelf_threshold_qty: Number(form.shelf_threshold_qty ?? 0),
        w2_warehouse_ref1c: form.w2_warehouse_ref1c?.trim() || null,
        w3_warehouse_ref1c: form.w3_warehouse_ref1c?.trim() || null,
        w4_warehouse_ref1c: form.w4_warehouse_ref1c?.trim() || null,
        fastener_categories: Array.from(
          new Set(form.fastener_categories.map((name) => name.trim()).filter(Boolean)),
        ),
      }
      const saved = await updateDbrSettings(payload)
      setForm(toForm(saved))
      setMessage('Настройки сохранены')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function addRate() {
    const resourceId = Number(rateDraft.resource_id)
    const itemId = rateItem?.item_id
    const qtyPer = Number(rateDraft.qty_per_capacity)
    if (!resourceId || !itemId) {
      setError('Укажите участок и номенклатуру')
      return
    }
    if (!Number.isFinite(qtyPer) || qtyPer <= 0) {
      setError('Такт сборки должен быть больше нуля')
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await upsertDbrAssemblyRate({ resource_id: resourceId, item_id: itemId, qty_per_capacity: qtyPer })
      setRates(await listDbrAssemblyRates())
      setRateDraft(emptyRateDraft)
      setRateItem(null)
      setMessage('Такт сборки сохранён')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function removeRate(id: number) {
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await deleteDbrAssemblyRate(id)
      setRates((prev) => prev.filter((r) => r.id !== id))
      setMessage('Такт сборки удалён')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  function patchRisk(index: number, next: Partial<DbrCategoryRisk>) {
    setRisks((prev) => prev.map((row, i) => (i === index ? { ...row, ...next } : row)))
  }

  function addRiskRow() {
    setRisks((prev) => [...prev, { id: 0, item_group: '', receipt_warehouse_ref1c: '', supply_risk_pct: '' }])
  }

  function removeRiskRow(index: number) {
    setRisks((prev) => prev.filter((_, i) => i !== index))
  }

  async function saveRisks() {
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const rows = risks
        .map((r) => ({
          item_group: String(r.item_group ?? '').trim(),
          receipt_warehouse_ref1c: (r.receipt_warehouse_ref1c ?? '').toString().trim() || null,
          supply_risk_pct:
            r.supply_risk_pct === '' || r.supply_risk_pct === null || r.supply_risk_pct === undefined
              ? null
              : Number(r.supply_risk_pct),
        }))
        .filter((r) => r.item_group)
      const saved = await replaceDbrCategoryRisks(rows)
      setRisks(saved)
      setMessage('Категорийные риски сохранены')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование DBR / Настройки модуля</div>
        <div className="runBadge">Тактов: {rates.length} · Рисков: {risks.length}</div>
      </div>

      <DocumentWindow
        title="Настройки DBR"
        subtitle="Параметры барабан-буфер-канат, такты сборки и категорийные риски"
        hotkeys="F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={form ? 1 : 0}
            visibleTo={form ? 1 : 0}
            total={form ? 1 : 0}
            selectedCount={form ? 1 : 0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <DbrNav />
        <div className="commandBar">
          <button className="primary" onClick={() => void saveSettings()} disabled={saving || loading || !form}>Сохранить настройки</button>
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="dbrScroll">
          {/* ── Settings card ─────────────────────────────────────────── */}
          <section className="dbrSection">
            <h2>Параметры модуля</h2>
            {form ? (
              <div className="dbrFormGrid">
                <NumField label="Заморозка, дней" value={form.frozen_days} onChange={(v) => patch({ frozen_days: v })} />
                <NumField label="Горизонт гейта, раб. дней" value={form.gate_horizon_workdays} onChange={(v) => patch({ gate_horizon_workdays: v })} />
                <NumField label="Порог полки, шт" value={Number(form.shelf_threshold_qty)} step="0.001" onChange={(v) => patch({ shelf_threshold_qty: v })} />

                <FieldGroupLabel>RT-классы (время реакции), дней</FieldGroupLabel>
                <NumField label="Мехобработка" value={form.rt_machining_days} onChange={(v) => patch({ rt_machining_days: v })} />
                <NumField label="Сварка" value={form.rt_welding_days} onChange={(v) => patch({ rt_welding_days: v })} />
                <NumField label="Покраска" value={form.rt_painting_days} onChange={(v) => patch({ rt_painting_days: v })} />

                <FieldGroupLabel>Партии (batch), дней</FieldGroupLabel>
                <NumField label="Токарка" value={form.batch_days_turning} onChange={(v) => patch({ batch_days_turning: v })} />
                <NumField label="Гибка" value={form.batch_days_bending} onChange={(v) => patch({ batch_days_bending: v })} />
                <NumField label="Сварка" value={form.batch_days_welding} onChange={(v) => patch({ batch_days_welding: v })} />
                <NumField label="Покраска (чёрная)" value={form.batch_days_paint_black} onChange={(v) => patch({ batch_days_paint_black: v })} />
                <NumField label="Покраска (цветная)" value={form.batch_days_paint_color} onChange={(v) => patch({ batch_days_paint_color: v })} />

                <FieldGroupLabel>Цепочка питателей</FieldGroupLabel>
                <label className="dbrCheckField">
                  <input
                    type="checkbox"
                    checked={form.feeder_chain_enabled}
                    onChange={(e) => patch({ feeder_chain_enabled: e.target.checked })}
                  />
                  <span>Цепочка питателей включена</span>
                </label>
                <NumField label="Горизонт загрузки, недель" value={form.feeder_load_horizon_weeks} onChange={(v) => patch({ feeder_load_horizon_weeks: v })} />

                <FieldGroupLabel>Склады-роли (ref 1С)</FieldGroupLabel>
                <TextField label="Склад №2" value={form.w2_warehouse_ref1c ?? ''} onChange={(v) => patch({ w2_warehouse_ref1c: v })} />
                <TextField label="Склад №3" value={form.w3_warehouse_ref1c ?? ''} onChange={(v) => patch({ w3_warehouse_ref1c: v })} />
                <TextField label="Склад №4" value={form.w4_warehouse_ref1c ?? ''} onChange={(v) => patch({ w4_warehouse_ref1c: v })} />

                <FieldGroupLabel>Классификация комплекта</FieldGroupLabel>
                <label className="dbrField">
                  <span>Категории метизов (по одной в строке)</span>
                  <textarea
                    rows={4}
                    value={form.fastener_categories.join('\n')}
                    onChange={(e) => patch({ fastener_categories: e.target.value.split('\n') })}
                    placeholder={'Болты\nГайки\nШайбы'}
                  />
                </label>
              </div>
            ) : (
              <div className="emptyDetail">{loading ? 'Загрузка...' : 'Настройки недоступны'}</div>
            )}
          </section>

          {/* ── Assembly rates ────────────────────────────────────────── */}
          <section className="dbrSection">
            <h2>Такты сборки</h2>
            <table className="journalTable dbrTable">
              <thead>
                <tr>
                  <th className="itemCell">Участок</th>
                  <th className="itemCell">Номенклатура</th>
                  <th className="numCell">Шт/сутки</th>
                  <th className="dbrActionCol"></th>
                </tr>
              </thead>
              <tbody>
                {rates.map((row) => (
                  <tr key={row.id}>
                    <td className="itemCell">
                      <strong>{row.resource_name}</strong>
                      <span>ID {row.resource_id}</span>
                    </td>
                    <td className="itemCell">
                      <strong>{row.item_name}</strong>
                      <span>{row.item_code} · ID {row.item_id}</span>
                    </td>
                    <td className="numCell"><strong>{qty(row.qty_per_capacity)}</strong></td>
                    <td className="dbrActionCol">
                      <button onClick={() => void removeRate(row.id)} disabled={saving}>Удалить</button>
                    </td>
                  </tr>
                ))}
                {!rates.length && (
                  <tr><td colSpan={4} className="emptyDetail">Такты сборки не заданы</td></tr>
                )}
                <tr className="dbrAddRow">
                  <td className="itemCell">
                    <select
                      value={rateDraft.resource_id}
                      onChange={(e) => setRateDraft((d) => ({ ...d, resource_id: e.target.value }))}
                    >
                      <option value="">Участок…</option>
                      {resources.map((r) => (
                        <option key={r.resource_id} value={r.resource_id}>{r.resource_name}</option>
                      ))}
                    </select>
                  </td>
                  <td className="itemCell">
                    <ItemPicker value={rateItem} onChange={setRateItem} />
                  </td>
                  <td className="numCell">
                    <input
                      type="number"
                      min="0.001"
                      step="0.001"
                      placeholder="шт/сутки"
                      value={rateDraft.qty_per_capacity}
                      onChange={(e) => setRateDraft((d) => ({ ...d, qty_per_capacity: e.target.value }))}
                    />
                  </td>
                  <td className="dbrActionCol">
                    <button className="primary" onClick={() => void addRate()} disabled={saving}>Добавить</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          {/* ── Category risks ────────────────────────────────────────── */}
          <section className="dbrSection">
            <div className="dbrSectionHead">
              <h2>Категорийные риски снабжения</h2>
              <div className="commandBar">
                <button onClick={addRiskRow} disabled={saving}>Добавить строку</button>
                <button className="primary" onClick={() => void saveRisks()} disabled={saving || loading}>Сохранить риски</button>
              </div>
            </div>
            <table className="journalTable dbrTable">
              <thead>
                <tr>
                  <th className="itemCell">Категория (item_group)</th>
                  <th className="itemCell">Склад прихода (ref 1С)</th>
                  <th className="numCell">Риск, %</th>
                  <th className="dbrActionCol"></th>
                </tr>
              </thead>
              <tbody>
                {risks.map((row, index) => (
                  <tr key={row.id || `new-${index}`}>
                    <td className="itemCell">
                      <input
                        value={row.item_group}
                        onChange={(e) => patchRisk(index, { item_group: e.target.value })}
                        placeholder="категория"
                      />
                    </td>
                    <td className="itemCell">
                      <input
                        value={row.receipt_warehouse_ref1c ?? ''}
                        onChange={(e) => patchRisk(index, { receipt_warehouse_ref1c: e.target.value })}
                        placeholder="ref 1С"
                      />
                    </td>
                    <td className="numCell">
                      <input
                        type="number"
                        step="0.01"
                        value={row.supply_risk_pct ?? ''}
                        onChange={(e) => patchRisk(index, { supply_risk_pct: e.target.value })}
                      />
                    </td>
                    <td className="dbrActionCol">
                      <button onClick={() => removeRiskRow(index)} disabled={saving}>Удалить</button>
                    </td>
                  </tr>
                ))}
                {!risks.length && (
                  <tr><td colSpan={4} className="emptyDetail">Категорийные риски не заданы</td></tr>
                )}
              </tbody>
            </table>
          </section>
        </div>
      </DocumentWindow>
    </main>
  )
}

function NumField({ label, value, onChange, step }: {
  label: string
  value: number
  onChange: (value: number) => void
  step?: string
}) {
  return (
    <label className="dbrField">
      <span>{label}</span>
      <input
        type="number"
        step={step ?? '1'}
        value={Number.isFinite(value) ? value : 0}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  )
}

function TextField({ label, value, onChange }: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="dbrField">
      <span>{label}</span>
      <input value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

function FieldGroupLabel({ children }: { children: ReactNode }) {
  return <div className="dbrGroupLabel">{children}</div>
}
