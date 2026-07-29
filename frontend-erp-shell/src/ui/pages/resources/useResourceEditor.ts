import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ProductionKind,
  ProductionResource,
  ProductionResourcePayload,
  ResourceProductionKind,
  ResourceStage,
} from '../../../domain/resources'
import {
  addResourceProductionKind,
  createResource,
  listProductionKinds,
  listResourceProductionKinds,
  listResources,
  listResourceStages,
  removeResourceProductionKind,
  updateResource,
} from '../../../services/resources'
import {
  emptyResourceForm,
  normalizeResourcePayload,
  resourceToForm,
} from './resourceForm'

export function useResourceEditor() {
  const [rows, setRows] = useState<ProductionResource[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [stages, setStages] = useState<ResourceStage[]>([])
  const [kinds, setKinds] = useState<ResourceProductionKind[]>([])
  const [allKinds, setAllKinds] = useState<ProductionKind[]>([])
  const [selectedKind, setSelectedKind] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState<ProductionResourcePayload>(emptyResourceForm)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const detailSequence = useRef(0)

  const active = useMemo(
    () => creating ? null : rows.find((row) => row.resource_id === activeId) ?? rows[0] ?? null,
    [rows, activeId, creating],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    setMessage('')
    try {
      const data = await listResources()
      setRows(data)
      setActiveId((current) => current && data.some((row) => row.resource_id === current) ? current : data[0]?.resource_id ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadProductionKindsCatalog = useCallback(async () => {
    try {
      setAllKinds(await listProductionKinds())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const loadDetails = useCallback(async (resource: ProductionResource) => {
    if (creating) return
    const sequence = ++detailSequence.current
    setActiveId(resource.resource_id)
    setForm(resourceToForm(resource))
    setStages([])
    setKinds([])
    try {
      const [nextStages, nextKinds] = await Promise.all([
        listResourceStages(resource.resource_id),
        listResourceProductionKinds(resource.resource_id),
      ])
      if (sequence !== detailSequence.current) return
      setStages(nextStages)
      setKinds(nextKinds)
      setSelectedKind('')
    } catch (e) {
      if (sequence !== detailSequence.current) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [creating])

  const beginCreate = useCallback(() => {
    detailSequence.current += 1
    setCreating(true)
    setActiveId(null)
    setStages([])
    setKinds([])
    setForm(emptyResourceForm())
    setError('')
    setMessage('')
  }, [])

  const selectResource = useCallback((resource: ProductionResource) => {
    detailSequence.current += 1
    setCreating(false)
    setActiveId(resource.resource_id)
    setForm(resourceToForm(resource))
  }, [])

  const saveResource = useCallback(async () => {
    const payload = normalizeResourcePayload(form)
    if (!payload.resource_name) {
      setError('Введите название участка')
      return
    }
    setSaving(true)
    setError('')
    setMessage('')
    try {
      const saved = creating
        ? await createResource(payload)
        : active
          ? await updateResource(active.resource_id, payload)
          : null
      await load()
      if (saved) {
        setCreating(false)
        setActiveId(saved.resource_id)
        setForm(resourceToForm(saved))
        setMessage(creating ? 'Участок создан' : 'Участок сохранен')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [active, creating, form, load])

  const addKind = useCallback(async () => {
    if (!active || !selectedKind) return
    const selectionSequence = detailSequence.current
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await addResourceProductionKind(active.resource_id, Number(selectedKind))
      if (selectionSequence !== detailSequence.current) return
      await loadDetails(active)
      setMessage('Вид производства привязан к участку')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [active, loadDetails, selectedKind])

  const removeKind = useCallback(async (kind: ResourceProductionKind) => {
    if (!active) return
    const selectionSequence = detailSequence.current
    setSaving(true)
    setError('')
    setMessage('')
    try {
      await removeResourceProductionKind(active.resource_id, kind.production_kind_id)
      if (selectionSequence !== detailSequence.current) return
      await loadDetails(active)
      setMessage('Привязка вида производства снята')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [active, loadDetails])

  useEffect(() => {
    void load()
    void loadProductionKindsCatalog()
  }, [load, loadProductionKindsCatalog])

  useEffect(() => {
    if (active) void loadDetails(active)
  }, [active, loadDetails])

  return {
    active,
    addKind,
    allKinds,
    beginCreate,
    creating,
    error,
    form,
    kinds,
    load,
    loadDetails,
    loading,
    message,
    removeKind,
    rows,
    saveResource,
    saving,
    selectedKind,
    selectResource,
    setForm,
    setSelectedKind,
    stages,
  }
}
