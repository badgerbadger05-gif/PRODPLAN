import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DbrAssemblyRate, DbrCategoryRisk } from '../../../domain/dbr'
import type { ProductionResource } from '../../../domain/resources'
import {
  deleteDbrAssemblyRate,
  getDbrSettings,
  listDbrAssemblyRates,
  listDbrCategoryRisks,
  replaceDbrCategoryRisks,
  updateDbrSettings,
  upsertDbrAssemblyRate,
} from '../../../services/dbr'
import { listResources } from '../../../services/resources'
import type { PickedItem } from '../../dbr/ItemPicker'
import type { KeyboardShortcut } from '../../platform'
import {
  normalizeCategoryRiskRows,
  toSettingsForm,
  toSettingsUpdate,
  type SettingsForm,
} from './model'

const emptyRateDraft = { resource_id: '', qty_per_capacity: '' }
const loadLabels = ['Параметры модуля', 'Такты сборки', 'Категорийные риски', 'Участки'] as const
type LoadError = { label: typeof loadLabels[number]; message: string }

export function useDbrSettingsController() {
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [rates, setRates] = useState<DbrAssemblyRate[]>([])
  const [risks, setRisks] = useState<DbrCategoryRisk[]>([])
  const [resources, setResources] = useState<ProductionResource[]>([])
  const [rateDraft, setRateDraft] = useState(emptyRateDraft)
  const [rateItem, setRateItem] = useState<PickedItem | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [loadErrors, setLoadErrors] = useState<LoadError[]>([])
  const [message, setMessage] = useState('')
  const loadSequence = useRef(0)
  const mutationInFlight = useRef(false)

  const load = useCallback(async () => {
    const sequence = ++loadSequence.current
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const results = await Promise.allSettled([
        getDbrSettings(),
        listDbrAssemblyRates(),
        listDbrCategoryRisks(),
        listResources(),
      ])
      if (sequence !== loadSequence.current) return
      const failures: LoadError[] = []
      results.forEach((result, index) => {
        if (result.status === 'rejected') {
          failures.push({
            label: loadLabels[index],
            message: result.reason instanceof Error ? result.reason.message : String(result.reason),
          })
        }
      })
      const [settingsResult, ratesResult, risksResult, resourcesResult] = results
      if (settingsResult.status === 'fulfilled') setForm(toSettingsForm(settingsResult.value))
      if (ratesResult.status === 'fulfilled') setRates(ratesResult.value)
      if (risksResult.status === 'fulfilled') setRisks(risksResult.value)
      if (resourcesResult.status === 'fulfilled') setResources(resourcesResult.value)
      setLoadErrors(failures)
      setError(failures[0]?.message ?? '')
    } finally {
      if (sequence === loadSequence.current) setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const shortcuts = useMemo<KeyboardShortcut[]>(() => [{
    id: 'dbr-settings-refresh',
    keys: 'F5',
    run: () => void load(),
    allowInEditable: true,
    scope: 'resource',
  }], [load])

  function patch(next: Partial<SettingsForm>) {
    setForm((current) => (current ? { ...current, ...next } : current))
  }

  function beginMutation() {
    if (mutationInFlight.current) return false
    mutationInFlight.current = true
    setSaving(true)
    setError('')
    setMessage('')
    return true
  }

  function endMutation() {
    mutationInFlight.current = false
    setSaving(false)
  }

  async function saveSettings() {
    if (!form || !beginMutation()) return
    try {
      const saved = await updateDbrSettings(toSettingsUpdate(form))
      setForm(toSettingsForm(saved))
      setMessage('Настройки сохранены')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      endMutation()
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
    if (!beginMutation()) return
    try {
      await upsertDbrAssemblyRate({ resource_id: resourceId, item_id: itemId, qty_per_capacity: qtyPer })
      setRates(await listDbrAssemblyRates())
      setRateDraft(emptyRateDraft)
      setRateItem(null)
      setMessage('Такт сборки сохранён')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      endMutation()
    }
  }

  async function removeRate(id: number) {
    if (!beginMutation()) return
    try {
      await deleteDbrAssemblyRate(id)
      setRates((current) => current.filter((rate) => rate.id !== id))
      setMessage('Такт сборки удалён')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      endMutation()
    }
  }

  function patchRisk(index: number, next: Partial<DbrCategoryRisk>) {
    setRisks((current) => current.map((row, rowIndex) => (
      rowIndex === index ? { ...row, ...next } : row
    )))
  }

  function addRiskRow() {
    setRisks((current) => [
      ...current,
      { id: 0, item_group: '', receipt_warehouse_ref1c: '', supply_risk_pct: '' },
    ])
  }

  function removeRiskRow(index: number) {
    setRisks((current) => current.filter((_, rowIndex) => rowIndex !== index))
  }

  async function saveRisks() {
    if (!beginMutation()) return
    try {
      const saved = await replaceDbrCategoryRisks(normalizeCategoryRiskRows(risks))
      setRisks(saved)
      setMessage('Категорийные риски сохранены')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      endMutation()
    }
  }

  return {
    form,
    rates,
    risks,
    resources,
    rateDraft,
    setRateDraft,
    rateItem,
    setRateItem,
    loading,
    saving,
    error,
    loadErrors,
    message,
    shortcuts,
    load,
    patch,
    saveSettings,
    addRate,
    removeRate,
    patchRisk,
    addRiskRow,
    removeRiskRow,
    saveRisks,
  }
}
