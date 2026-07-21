import { dateRu, qty } from '../../lib/format'
import { DocumentWindow } from '../layout/DocumentWindow'
import { StatusBar } from '../layout/StatusBar'
import { DbrNav } from '../dbr/DbrNav'
import { ItemPicker } from '../dbr/ItemPicker'
import { programStatusLabel } from './dbr-programs/model'
import { useDbrProgramsController } from './dbr-programs/useDbrProgramsController'

export function DbrProgramsPage() {
  const controller = useDbrProgramsController()
  const {
    programs, selected, editRows, loading, saving, error, message,
    fromDate, toDate, title, company, rows,
    setToDate, setTitle, setCompany, changeFromDate,
    load, openProgram, patchRow, addRow, removeRow, submit, approve,
    patchEditRow, addEditRow, removeEditRow, saveDraftItems,
  } = controller

  return (
    <main className="workArea">
      <div className="topLine">
        <div className="breadcrumbs">Планирование DBR / Производственные программы</div>
        <div className="runBadge">Программ: {programs.length}</div>
      </div>

      <DocumentWindow
        title="Производственные программы"
        subtitle="План выпуска по датам — источник для построения барабана"
        hotkeys="F5 Обновить"
        footer={(
          <StatusBar
            loading={loading}
            visibleFrom={programs.length ? 1 : 0}
            visibleTo={programs.length}
            total={programs.length}
            selectedCount={selected ? 1 : 0}
            canPrev={false}
            canNext={false}
            onPrev={() => undefined}
            onNext={() => undefined}
          />
        )}
      >
        <DbrNav />

        <div className="commandBar">
          <button onClick={() => void load()} disabled={loading}>Обновить</button>
        </div>

        {error && <div className="errorLine">{error}</div>}
        {message && <div className="successLine">{message}</div>}

        <div className="dbrScroll">
          {/* ── Create program ─────────────────────────────────────────── */}
          <section className="dbrSection">
            <h2>Новая программа</h2>
            <div className="dbrProgramHead">
              <label className="dbrField">
                <span>Период с</span>
                <input
                  type="date"
                  value={fromDate}
                  onChange={(e) => changeFromDate(e.target.value)}
                />
              </label>
              <label className="dbrField">
                <span>Период по</span>
                <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} />
              </label>
              <label className="dbrField">
                <span>Название</span>
                <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="напр. Июль 2026" />
              </label>
              <label className="dbrField">
                <span>Компания</span>
                <input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="необязательно" />
              </label>
            </div>

            <table className="journalTable dbrTable dbrProgramTable">
              <thead>
                <tr>
                  <th className="itemCell">Номенклатура</th>
                  <th className="dateCol">Дата</th>
                  <th className="numCell">Кол-во</th>
                  <th className="itemCell">Комментарий</th>
                  <th className="dbrActionCol"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.key}>
                    <td className="itemCell">
                      <ItemPicker value={row.item} onChange={(item) => patchRow(row.key, { item })} />
                    </td>
                    <td className="dateCol">
                      <input
                        type="date"
                        value={row.program_date}
                        onChange={(e) => patchRow(row.key, { program_date: e.target.value, dateEdited: true })}
                      />
                    </td>
                    <td className="numCell">
                      <input
                        type="number"
                        step="0.001"
                        min="0.001"
                        value={row.qty}
                        placeholder="шт"
                        onChange={(e) => patchRow(row.key, { qty: e.target.value })}
                      />
                    </td>
                    <td className="itemCell">
                      <input
                        value={row.comment}
                        onChange={(e) => patchRow(row.key, { comment: e.target.value })}
                        placeholder="необязательно"
                      />
                    </td>
                    <td className="dbrActionCol">
                      <button onClick={() => removeRow(row.key)} disabled={rows.length <= 1}>Удалить</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="commandBar">
              <button onClick={addRow} disabled={saving}>Добавить строку</button>
              <div className="commandBarSpacer" />
              <button className="primary" onClick={() => void submit()} disabled={saving}>Создать программу</button>
            </div>
          </section>

          {/* ── Program list ───────────────────────────────────────────── */}
          <section className="dbrSection">
            <h2>Список программ</h2>
            <table className="journalTable dbrTable">
              <thead>
                <tr>
                  <th className="numCell">№</th>
                  <th className="itemCell">Название</th>
                  <th className="dateCol">Период</th>
                  <th className="numCell">Строк</th>
                  <th className="itemCell">Статус</th>
                  <th className="dbrActionCol"></th>
                </tr>
              </thead>
              <tbody>
                {programs.map((program) => (
                  <tr
                    key={program.id}
                    className={selected?.id === program.id ? 'selectedRow' : ''}
                    onClick={() => void openProgram(program.id)}
                  >
                    <td className="numCell"><strong>{program.id}</strong></td>
                    <td className="itemCell">
                      <strong>{program.title || '(без названия)'}</strong>
                      <span>{program.company || ''}</span>
                    </td>
                    <td className="dateCol">{dateRu(program.from_date)} — {dateRu(program.to_date)}</td>
                    <td className="numCell">{program.items.length}</td>
                    <td className="itemCell">
                      <span className={`miniPill ${program.status === 'approved' ? 'ready' : ''}`}>
                        {programStatusLabel(program.status)}
                      </span>
                    </td>
                    <td className="dbrActionCol">
                      {program.status === 'draft' && (
                        <button
                          className="primary"
                          onClick={(e) => { e.stopPropagation(); void approve(program.id) }}
                          disabled={saving}
                        >
                          Утвердить
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {!programs.length && (
                  <tr><td colSpan={6} className="emptyDetail">{loading ? 'Загрузка…' : 'Программы не созданы'}</td></tr>
                )}
              </tbody>
            </table>
          </section>

          {/* ── Selected program detail ────────────────────────────────── */}
          {selected && (
            <section className="dbrSection">
              <div className="dbrSectionHead">
                <h2>Программа №{selected.id}: {selected.title || '(без названия)'}</h2>
                <div className="commandBar">
                  {selected.status === 'draft' && (
                    <>
                      <button
                        onClick={addEditRow}
                        disabled={saving}
                      >
                        Добавить строку
                      </button>
                      <button className="primary" onClick={() => void saveDraftItems()} disabled={saving}>Сохранить строки</button>
                      <button onClick={() => void approve(selected.id)} disabled={saving}>Утвердить</button>
                    </>
                  )}
                </div>
              </div>
              <table className="journalTable dbrTable">
                <thead>
                  <tr>
                    <th className="dateCol">Дата</th>
                    <th className="itemCell">Номенклатура</th>
                    <th className="numCell">Кол-во</th>
                    <th className="itemCell">Комментарий</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.status === 'draft' ? editRows.map((row) => (
                    <tr key={row.key}>
                      <td className="dateCol">
                        <input type="date" value={row.program_date} onChange={(e) => patchEditRow(row.key, { program_date: e.target.value })} />
                      </td>
                      <td className="itemCell">
                        <ItemPicker value={row.item} onChange={(item) => patchEditRow(row.key, { item })} />
                      </td>
                      <td className="numCell">
                        <input type="number" min="0.001" step="0.001" value={row.qty} onChange={(e) => patchEditRow(row.key, { qty: e.target.value })} />
                      </td>
                      <td className="itemCell">
                        <input value={row.comment} onChange={(e) => patchEditRow(row.key, { comment: e.target.value })} />
                        <button onClick={() => removeEditRow(row.key)} disabled={saving}>Удалить</button>
                      </td>
                    </tr>
                  )) : selected.items.map((item) => (
                    <tr key={item.id}>
                      <td className="dateCol">{dateRu(item.program_date)}</td>
                      <td className="itemCell">
                        <strong>{item.item_name || item.item_code || `ID ${item.item_id}`}</strong>
                        <span>{item.item_code || `ID ${item.item_id}`}</span>
                      </td>
                      <td className="numCell"><strong>{qty(item.qty)}</strong></td>
                      <td className="itemCell">{item.comment || ''}</td>
                    </tr>
                  ))}
                  {!(selected.status === 'draft' ? editRows : selected.items).length && (
                    <tr><td colSpan={4} className="emptyDetail">В программе нет строк</td></tr>
                  )}
                </tbody>
              </table>
            </section>
          )}
        </div>
      </DocumentWindow>
    </main>
  )
}
