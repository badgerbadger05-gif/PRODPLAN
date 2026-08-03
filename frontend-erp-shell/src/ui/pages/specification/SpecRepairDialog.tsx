import { useEffect, useMemo, useState } from 'react'
import type { BomItem, BomItemIdentity, SpecFlatRow, SpecNode } from '../../../domain/specification'
import type {
  AddResult,
  KindChangePreviewResult,
  MoveResult,
  ProductionKind,
  RemoveResult,
  RestageResult,
  SetQuantityResult,
  StageOption,
} from '../../../domain/specificationRepair'
import {
  listProductionKinds,
  listStages,
  repairAdd,
  repairKindChangePreview,
  repairMove,
  repairRemove,
  repairRestage,
  repairSetQuantity,
} from '../../../services/specificationRepair'
import { searchSpecificationItems } from '../../../services/specification'

export type RepairAction = 'restage' | 'move' | 'add' | 'quantity' | 'remove' | 'kind'

const TITLES: Record<RepairAction, string> = {
  restage: 'Сменить этап компонента',
  move: 'Перенести компонент в другую спецификацию',
  add: 'Добавить компонент в спецификацию',
  quantity: 'Изменить количество компонента',
  remove: 'Убрать компонент из спецификации',
  kind: 'Смена вида производства — превью каскада',
}

const COMPONENT_TYPES = ['Сборка', 'Закупка', 'Материал']

function nodeItemId(node: SpecNode | null): number | null {
  const payload = (node as (SpecNode & { item?: { id?: number | string } }) | null)?.item
  if (payload?.id == null) return null
  const parsed = Number(payload.id)
  return Number.isFinite(parsed) ? parsed : null
}

function nodeTitle(node: SpecNode | null) {
  if (!node) return ''
  return node.type === 'operation' ? node.operation?.name || 'Операция' : node.name || 'Номенклатура'
}

type RepairResult = RestageResult | MoveResult | AddResult | SetQuantityResult | RemoveResult | KindChangePreviewResult

type Props = {
  action: RepairAction
  node: SpecFlatRow | null
  rootItem: BomItemIdentity
  treeRows: SpecFlatRow[]
  onClose: () => void
  onApplied: (message: string) => void
}

export function SpecRepairDialog({ action, node, rootItem, treeRows, onClose, onApplied }: Props) {
  const [stages, setStages] = useState<StageOption[]>([])
  const [kinds, setKinds] = useState<ProductionKind[]>([])
  const [refError, setRefError] = useState('')

  // Общие поля
  const [newStageId, setNewStageId] = useState<string>('') // '' = не задан
  const [dryRun, setDryRun] = useState(true)
  const [busy, setBusy] = useState(false)
  const [errorText, setErrorText] = useState('')
  const [result, setResult] = useState<RepairResult | null>(null)

  // move / remove
  const [targetSpecId, setTargetSpecId] = useState<string>('')
  const [force, setForce] = useState(false)

  // remove
  const [confirmRemove, setConfirmRemove] = useState(false)

  // quantity — преднабор текущим количеством выбранной строки
  const [qtyValue, setQtyValue] = useState<number>(node?.qtyPerParent ?? 1)

  // add
  const [addItemQuery, setAddItemQuery] = useState('')
  const [addSearchItems, setAddSearchItems] = useState<BomItem[]>([])
  const [addItem, setAddItem] = useState<BomItem | null>(null)
  const [addQty, setAddQty] = useState<number>(1)
  const [addType, setAddType] = useState<string>('Сборка')
  const [searching, setSearching] = useState(false)

  // kind
  const [newKindId, setNewKindId] = useState<string>('')

  const componentId = node?.componentId ?? null
  const itemId = nodeItemId(node)
  // Спека для добавления: собственная спека выбранного узла, иначе спека корня.
  const addSpecId = node?.specId ?? rootItem.spec_id ?? null

  // Кандидаты целевых спек для переноса — из дерева (узлы со своей спекой).
  const targetSpecOptions = useMemo(() => {
    const seen = new Map<number, string>()
    for (const row of treeRows) {
      if (row.type !== 'item' || row.specId == null) continue
      if (!seen.has(row.specId)) seen.set(row.specId, `${nodeTitle(row)} · #${row.specId}`)
    }
    return Array.from(seen.entries()).map(([value, label]) => ({ value, label }))
  }, [treeRows])

  useEffect(() => {
    let alive = true
    async function loadRefs() {
      try {
        const needStages = action === 'restage' || action === 'move' || action === 'add'
        const [st, kd] = await Promise.all([
          needStages ? listStages() : Promise.resolve<StageOption[]>([]),
          action === 'kind' ? listProductionKinds() : Promise.resolve<ProductionKind[]>([]),
        ])
        if (!alive) return
        setStages(st)
        setKinds(kd)
      } catch (e) {
        if (alive) setRefError(e instanceof Error ? e.message : String(e))
      }
    }
    void loadRefs()
    return () => {
      alive = false
    }
  }, [action])

  async function searchAddItem() {
    if (!addItemQuery.trim()) return
    setSearching(true)
    setErrorText('')
    try {
      const data = await searchSpecificationItems({ q: addItemQuery.trim(), limit: 40 })
      setAddSearchItems(data.items ?? [])
    } catch (e) {
      setErrorText(e instanceof Error ? e.message : String(e))
    } finally {
      setSearching(false)
    }
  }

  // Текстовая блокировка: чего не хватает, чтобы выполнить операцию.
  const blocker = useMemo(() => {
    if (refError) return refError
    if (action === 'restage') {
      if (componentId == null) return 'Выберите строку состава (компонент, не корень).'
    }
    if (action === 'move') {
      if (componentId == null) return 'Выберите строку состава (компонент, не корень).'
      if (!targetSpecId.trim()) return 'Укажите целевую спецификацию.'
    }
    if (action === 'add') {
      if (addSpecId == null) return 'У выбранного узла нет спецификации, куда добавлять.'
      if (!addItem) return 'Выберите номенклатуру для добавления.'
      if (!(addQty > 0)) return 'Количество должно быть больше нуля.'
    }
    if (action === 'quantity') {
      if (componentId == null) return 'Выберите строку состава (компонент, не корень).'
      if (!(qtyValue > 0)) return 'Количество должно быть больше нуля.'
    }
    if (action === 'remove') {
      if (componentId == null) return 'Выберите строку состава (компонент, не корень).'
      if (!confirmRemove) return 'Поставьте галку подтверждения удаления.'
    }
    if (action === 'kind') {
      if (itemId == null) return 'Не удалось определить номенклатуру узла.'
      if (!newKindId.trim()) return 'Выберите новый вид производства.'
    }
    return ''
  }, [action, refError, componentId, targetSpecId, addSpecId, addItem, addQty, qtyValue, confirmRemove, itemId, newKindId])

  async function submit() {
    if (blocker) return
    setBusy(true)
    setErrorText('')
    setResult(null)
    try {
      const stageNum = newStageId.trim() ? Number(newStageId) : null
      if (action === 'restage') {
        const res = await repairRestage({ component_id: componentId!, new_stage_id: stageNum, dry_run: dryRun })
        setResult(res)
      } else if (action === 'move') {
        const res = await repairMove({
          component_id: componentId!,
          target_spec_id: Number(targetSpecId),
          new_stage_id: stageNum,
          force,
          dry_run: dryRun,
        })
        setResult(res)
      } else if (action === 'add') {
        const res = await repairAdd({
          spec_id: addSpecId!,
          item_id: addItem!.item_id,
          quantity: addQty,
          component_type: addType,
          stage_id: stageNum,
          dry_run: dryRun,
        })
        setResult(res)
      } else if (action === 'quantity') {
        const res = await repairSetQuantity({ component_id: componentId!, quantity: qtyValue, dry_run: dryRun })
        setResult(res)
      } else if (action === 'remove') {
        const res = await repairRemove({ component_id: componentId!, force, dry_run: dryRun })
        setResult(res)
      } else {
        // kind — всегда read-only превью
        const res = await repairKindChangePreview({ item_id: itemId!, new_production_kind_id: Number(newKindId) })
        setResult(res)
        return
      }
      // Реальная запись в 1С прошла — сообщаем наверх и перезагружаем.
      if (!dryRun) onApplied(`Записано в 1С: ${TITLES[action]}`)
    } catch (e) {
      setErrorText(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const isWriteResult = result != null && action !== 'kind'
  const writeResult = isWriteResult ? (result as RestageResult | MoveResult | AddResult | SetQuantityResult | RemoveResult) : null
  const kindResult = action === 'kind' && result ? (result as KindChangePreviewResult) : null

  const stageLabel =
    action === 'restage' ? 'Новый этап (пусто — снять этап)' : 'Этап (пусто — как у соседей)'

  const submitLabel = action === 'kind' ? 'Показать каскад' : dryRun ? 'Проверить (dry-run)' : 'Записать в 1С'

  return (
    <div className="dialogOverlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialogBox">
        <div className="dialogHeader">{TITLES[action]}</div>
        <div className="dialogBody">
          {node && (
            <div className="detailMeta">
              <span>{nodeTitle(node)}</span>
              <span>{node.article || ''}</span>
            </div>
          )}
          {errorText && <div className="dialogError">{errorText}</div>}

          {action === 'restage' && (
            <div className="dialogField">
              <label>{stageLabel}</label>
              <select value={newStageId} onChange={(e) => setNewStageId(e.target.value)}>
                <option value="">— без этапа —</option>
                {stages.map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </select>
            </div>
          )}

          {action === 'move' && (
            <>
              <div className="dialogField">
                <label>Целевая спецификация</label>
                <select value={targetSpecId} onChange={(e) => setTargetSpecId(e.target.value)}>
                  <option value="">— выберите —</option>
                  {targetSpecOptions.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                <input
                  type="number"
                  placeholder="или введите spec_id вручную"
                  value={targetSpecId}
                  onChange={(e) => setTargetSpecId(e.target.value)}
                />
              </div>
              <div className="dialogField">
                <label>{stageLabel}</label>
                <select value={newStageId} onChange={(e) => setNewStageId(e.target.value)}>
                  <option value="">— как у соседей —</option>
                  {stages.map((s) => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
              </div>
              <div className="dialogCheckRow">
                <input id="repairForce" type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                <label htmlFor="repairForce">force — разрешить, даже если деталь останется вне всех спек</label>
              </div>
            </>
          )}

          {action === 'add' && (
            <>
              <div className="detailMeta">
                <span>Добавляем в спецификацию #{addSpecId ?? '—'}</span>
              </div>
              <div className="dialogField">
                <label>Номенклатура</label>
                {addItem ? (
                  <div className="detailMeta">
                    <span>{addItem.item_name} ({addItem.item_article || addItem.item_code})</span>
                    <button onClick={() => setAddItem(null)}>Сменить</button>
                  </div>
                ) : (
                  <>
                    <div className="dialogCheckRow">
                      <input
                        type="text"
                        placeholder="Артикул, код, название"
                        value={addItemQuery}
                        onChange={(e) => setAddItemQuery(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && void searchAddItem()}
                      />
                      <button onClick={() => void searchAddItem()} disabled={searching || !addItemQuery.trim()}>Найти</button>
                    </div>
                    {addSearchItems.length > 0 && (
                      <div className="dialogPreview repairItemPick">
                        {addSearchItems.map((it) => (
                          <button key={it.item_id} className="repairItemRow" onClick={() => { setAddItem(it); setAddSearchItems([]) }}>
                            {it.item_name} · {it.item_article || it.item_code}
                          </button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
              <div className="dialogField">
                <label>Количество</label>
                <input type="number" min="0.0001" step="1" value={addQty} onChange={(e) => setAddQty(Number(e.target.value || 0))} />
              </div>
              <div className="dialogField">
                <label>Тип компонента</label>
                <select value={addType} onChange={(e) => setAddType(e.target.value)}>
                  {COMPONENT_TYPES.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>
              <div className="dialogField">
                <label>{stageLabel}</label>
                <select value={newStageId} onChange={(e) => setNewStageId(e.target.value)}>
                  <option value="">— как у соседей —</option>
                  {stages.map((s) => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
              </div>
            </>
          )}

          {action === 'quantity' && (
            <div className="dialogField">
              <label>Новое количество (норма расхода){node?.unit ? `, ${node.unit}` : ''}</label>
              <input
                type="number"
                min="0.0001"
                step="any"
                value={qtyValue}
                onChange={(e) => setQtyValue(Number(e.target.value || 0))}
              />
            </div>
          )}

          {action === 'remove' && (
            <>
              <div className="dialogHint">
                Компонент будет удалён из спецификации. Если деталь больше нигде не
                используется — отметьте «force», иначе операция не пройдёт.
              </div>
              <div className="dialogCheckRow">
                <input id="repairConfirmRemove" type="checkbox" checked={confirmRemove} onChange={(e) => setConfirmRemove(e.target.checked)} />
                <label htmlFor="repairConfirmRemove">подтверждаю удаление компонента</label>
              </div>
              <div className="dialogCheckRow">
                <input id="repairRemoveForce" type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                <label htmlFor="repairRemoveForce">force — удалить, даже если деталь останется вне всех спецификаций</label>
              </div>
            </>
          )}

          {action === 'kind' && (
            <div className="dialogField">
              <label>Новый вид производства</label>
              <select value={newKindId} onChange={(e) => setNewKindId(e.target.value)}>
                <option value="">— выберите —</option>
                {kinds.map((k) => (
                  <option key={k.id} value={k.id}>{k.name}</option>
                ))}
              </select>
            </div>
          )}

          {action !== 'kind' && (
            <div className="dialogCheckRow">
              <input id="repairDryRun" type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
              <label htmlFor="repairDryRun">dry-run — только превью, в 1С не писать</label>
            </div>
          )}

          {blocker && !errorText && <div className="dialogHint">{blocker}</div>}

          {writeResult && (
            <div className="dialogPreview">
              {writeResult.dry_run ? '✓ dry-run: изменений в 1С нет\n' : '✓ записано в 1С\n'}
              {`Затронутые спецификации: ${writeResult.pending_1c.specs.join(', ') || '—'}\n`}
              {(writeResult.warnings ?? []).length > 0 && `⚠ ${JSON.stringify(writeResult.warnings)}\n`}
              {'\n'}
              {JSON.stringify(writeResult, null, 2)}
            </div>
          )}

          {kindResult && (
            <div className="dialogPreview">
              {`Текущий вид: ${kindResult.current_kind?.name ?? '—'} → новый: ${kindResult.new_kind?.name ?? '—'}\n`}
              {`Затронуто родительских строк состава: ${kindResult.cascade.affected_parent_rows}\n\n`}
              {kindResult.cascade.parents
                .map((p) => `• спека #${p.parent_spec_id} (${p.parent_spec_code ?? '—'}), строка #${p.component_id}, тип ${p.component_type ?? '—'}`)
                .join('\n') || 'Родителей с закреплённой спекой нет — каскад не требуется.'}
              {`\n\n${kindResult.cascade.note}`}
            </div>
          )}
        </div>
        <div className="dialogFooter">
          <button onClick={onClose}>{isWriteResult && !writeResult?.dry_run ? 'Закрыть' : 'Отмена'}</button>
          <button className="primary" onClick={() => void submit()} disabled={busy || !!blocker}>
            {busy ? '…' : submitLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
