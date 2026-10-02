# Прослеживаемость контуров и веток — снимок 2026-09-29

Датированное свидетельство, а не источник настройки. Собрано только чтением:
git-история локального репозитория, worktree/клоны на рабочей станции,
docker/psql на сервере mtzdock. Ни один контур в ходе сбора не изменялся.
Каталоги указаны именами рядом с основным checkout (`../<имя>`).

## Якоря

| Величина | Значение |
|---|---|
| Локальный `main` | `836e5fb0` (27.09) — **равен развёрнутому кандидату `current-20260925`** |
| Серверный checkout `prodplan-shadow` | `f087bfe61` — не содержит ни §58, ни линии исправления |
| Живой прод :9020 (emergency) | frontend `ae0f462a`, backend `204653fe` |
| Backend `204653fe` (11.09) | **не содержится ни в одной локальной ветке** — осиротевший коммит |
| Рабочий checkout `PRODPLAN` | ветка `codex/fix-replacement-realization-replay` @ `ae0f462a` (= код фронта живого прода), 1 коммит сверх main, расхождение с main 452 |

## Контуры на сервере

| Контур | Порты | Код | Состояние 29.09 |
|---|---|---|---|
| `emergency-20260918` (фактический прод) | фронт :9020, backend :18018 | frontend `ae0f462a`, backend `204653fe` | Жив: все ключевые экраны 200, Ledger свежий (gen 1577+, cutoff утро 29.09), 0 rejected за 24 ч; схема БД `20260909_02` |
| `current-20260925` (кандидат) | фронт :19023, backend :18021 | `836e5fb0` = main | Жив: экраны 200, Ledger свежий (gen 1545+); за сутки 12 rejected-поколений (последнее 07:34, самовосстановилось); схема `20260925_01`; **дефект бинарного known_at_freeze присутствует** |
| `shadow` (канонический по репо) | backend-green :18020 | `dc8b753687e6` (тег `b38f4dc5`) | Полуразобран: без frontend и sync-worker, truth с cutoff 23.09, production-экраны 503; `shadow-stack.sh verify` падает — pinned-образ не найден |

## Worktree основного репозитория (общий .git)

Статус «в main» означает: у ветки 0 коммитов, которых нет в main.

| Каталог | Ветка | Tip | Статус |
|---|---|---|---|
| `../PRODPLAN-current-execution-fixes` | codex/current-execution-followup-fixes | `424d118e` (18.09) | в main |
| `../PRODPLAN-current-execution-metrics` | codex/current-execution-refresh-metrics | `7fe15969` (18.09) | в main |
| `../PRODPLAN-current-execution` | codex/current-execution-ledger | `8685f6bd` (25.09) | в main |
| `../PRODPLAN-integration` | integration/current-execution-main | `e8a1dd15` (25.09) | в main (ранее кандидат `e8a1dd15` на сервере) |
| `../PRODPLAN-make-bootstrap` | fix/make-replenishment-bootstrap | `50112942` (28.09) | **+8 сверх main — линия §58** |
| (worktree codex warehouse-catalog-discovery) | codex/prod-readiness-20260929 | `b7c44328` (29.09, активна) | +15 сверх main, main содержит целиком; аудит расхождений контуров |
| (worktree codex) | codex/warehouse-catalog-discovery | `12a8283e` (29.09) | +1 сверх main; discovery справочников складов (закрывает 404 Catalog_Склады) |
| `../PRODPLAN-piecework-hotfix` | codex/fix-prod-piecework-identity | `688c18b8` (21.09) | +10 сверх main, отстаёт на 61 |
| `../PRODPLAN-current-execution-deploy-integration` | codex/current-execution-deploy-integration | `0d0b7e77` (18.09) | +2 сверх main, отстаёт на 61 |
| `../PRODPLAN-rehearsal-7fe15969`, `../PRODPLAN-rehearsal-b` | detached | `9dde4e22`, `590d68b4` (22.09) | репетиции развёртывания |

## Отдельные клоны 21–22.09 (свой .git, не worktree)

`../PRODPLAN-custody-publication-fix` (`9caa935a3`),
`../PRODPLAN-retain-deploy-integration` (`85b91be01`),
`../PRODPLAN-retain-reservation-wi-fix` (`058e53ee2`),
`../PRODPLAN-truth-freshness-20260921` (`5810ac6dc` — объект есть в основном репо, в main НЕ входит),
`../PRODPLAN-truth-freshness-ui-fix` (`8ce0324f5`),
`../PRODPLAN-truth-freshness-ui-reload-fix` (`2ef1cee96`),
`../PRODPLAN-ui-uuid-fallback` (`c8291be8a`).

Их объектов в основном репо нет; по сообщениям коммитов в main найден аналог
только для custody (`0e3b14d7` «Retain custody rewind baselines and publish
local tails»). Остальные либо перенесены иначе, либо брошены — достоверно
выясняется fetch их объектов и diff, на момент снимка не выполнялось.

## Линия §58 (решение + исправление, до прода не дошла)

| Артефакт | Коммит | Дата | Где живёт |
|---|---|---|---|
| Решение §58 (журнал решений, стр. ~995) | `19237207` | 28.09 | fix/make-replenishment-bootstrap, codex/prod-readiness-20260929 |
| Исправление «absorbed coverage leaves the fact for every owner» | `e3cab541` | 28.09 | те же ветки |
| Сопутствующие (bbf0acce, c98cdee0, 930e387c, 9c9fff20) | — | 27–28.09 | те же ветки |

`19237207` не является предком `836e5fb0`: развёрнутый current собран до
решения. Подтверждённый дефект развёрнутого кода — бинарное исключение
приходов по `known_at_freeze` без учёта `covered_from_stock_at_freeze`
(`backend/app/services/item_ledger/historical_replay_core.py:276-286` в
исходниках развёрнутого `836e5fb0`).

## Следствия

1. Три линии кода не пересекаются: серверный `f087bfe61`, локальный `main`
   (= прод-кандидат), ветки §58/аудита. Серверный checkout не знает ни о
   решении §58, ни об исправлении.
2. Живой прод частично собран из осиротевшего кода (`204653fe`).
3. Четыре worktree (current-execution*, integration) слиты в main и как
   источники кода избыточны; семь клонов 21–22.09 требуют решения
   (долить/закрыть), их статус недоказан.
4. Перед переключением прода на `current` линия §58 должна войти в образ;
   иначе зафиксируется известный дефект known_at_freeze (кейс план 5 /
   деталь 679: 360 вместо 1055).

## Как проверялось

```
git rev-list --left-right --count main...<ветка>   # слитость в main
git merge-base --is-ancestor 19237207 836e5fb0     # §58 не в кандидате
git branch -a --contains <коммит>                  # прослеживаемость
docker ps / curl /health / psql SELECT             # состояние контуров
```

## Актуализация 2026-09-30: очистка веток и каталогов

Кандидат `9d2f0069` (codex/prod-readiness-20260929) объединил линию §58,
warehouse-catalog и исправления аудита — 23 коммита сверх main. После этого
выполнена уборка; удалённое доказуемо сохранено в main или кандидате.

Удалённые worktree (содержимое полностью в main): `../PRODPLAN-current-execution`,
`../PRODPLAN-current-execution-fixes`, `../PRODPLAN-current-execution-metrics`,
`../PRODPLAN-integration`, `../PRODPLAN-rehearsal-7fe15969`, `../PRODPLAN-rehearsal-b`,
два temp-review worktree.

Удалённые ветки: codex/current-execution-followup-fixes, codex/current-execution-ledger,
codex/current-execution-refresh-metrics, integration/current-execution-main (все в main);
codex/warehouse-catalog-discovery (изменение идентично коммиту `fa0f26f9` в кандидате).

Семь клонов 21–22.09 удалены; их вершины заякорены архивными тегами
`archive/20260930-*` в основном репозитории (конвенция `archive/` уже
существовала). Незакоммиченный incident-evidence клона retain-deploy-integration
сохранён в `tmp/cleanup-2026-09-30/`.

Оставлены осознанно: `../PRODPLAN-make-bootstrap` (незакоммиченная правка
physical_refresh_orchestrator.py, 132 строки — уникальна), `../PRODPLAN-piecework-hotfix`
(+10 не в main), `../PRODPLAN-current-execution-deploy-integration` (+2 не в main),
worktree codex-сессий, ветка codex/fix-replacement-realization-replay (код фронта
живого emergency-прода), codex/prod-readiness-20260929 (кандидат).

## Актуализация 2026-09-30 (вторая волна): сведение к main + кандидат

Проверка содержимого (git cherry / сличение тем коммитов) показала:

- codex/fix-replacement-realization-replay — патч-эквивалент уже в main
  (merge `8aa024ee`); ветка удалена, основной worktree переведён на `main`.
  Коммит `ae0f462a` (код фронта emergency-прода) заякорен тегом
  `archive/20260930-fix-replacement-realization-replay`.
- codex/fix-prod-piecework-identity и codex/current-execution-deploy-integration —
  общие коммиты «Count net assembly output…» и «Keep weld and paint…» в main;
  остальные 8 тем не найдены ни в main, ни в кандидате (брошенные или
  переписанные ветки работ). Ветки и worktree удалены, вершины заякорены
  `archive/20260930-piecework-identity`, `archive/20260930-deploy-integration`.

Итоговые ветки: `main` (836e5fb0), `codex/prod-readiness-20260929` (кандидат
9d2f0069, +23). Единственная линия вне main — кандидат.

Позднее в тот же день удалены и `fix/make-replenishment-bootstrap` с её
worktree: ветка была прямым предком кандидата, а незакоммиченный WIP
(`settle_at`-урегулирование cutoff snap retirement, 28.09 15:22) заархивирован
патчем `tmp/cleanup-2026-09-30/make-bootstrap-settle-at-wip.patch` — подход
отклонён, в кандидате той семантики нет (решено через repair missing
recorder / receipt lineage / incoming edges).

Побочное: тест канона ловил копию самого себя во вложенных scratch-worktree
расширения Kilo Code (`.kilo/worktrees/*`, пересоздаются автоматически).
В `tests/test_canon_invariants.py` `.kilo` добавлен в `_SCRATCH_DIRS` по
изначальному замыслу механизма исключений (коммит на усмотрение владельца).
