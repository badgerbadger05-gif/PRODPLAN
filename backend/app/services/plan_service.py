from datetime import date, timedelta, datetime
from typing import Dict, List, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, text
from ..models import Item, RootProduct, ProductionPlanEntry, ProductionStage
from ..database import get_db


def fetch_stages(db: Session) -> List[Dict[str, Any]]:
    """
    Возвращает список этапов производства: [{'value': stage_id, 'label': stage_name}, ...]
    """
    stages = db.query(ProductionStage).order_by(
        ProductionStage.stage_order.asc().nulls_last(),
        ProductionStage.stage_name
    ).all()

    return [{"value": int(stage.stage_id), "label": str(stage.stage_name)} for stage in stages]


def query_plan_matrix_paginated(
    start_date_str: str,
    days: int = 30,
    stage_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 30,
    sort_by: str = 'item_name',
    sort_dir: str = 'asc',
    db: Session = None,
) -> Dict[str, Any]:
    """
    Возвращает страницу данных плана в виде матрицы по дням для заданного горизонта.
    """
    if db is None:
        db = next(get_db())

    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()

    horizon_days = max(1, int(days or 1))
    end = start + timedelta(days=horizon_days)

    # Безопасная сортировка
    sort_by = (sort_by or 'item_name').lower()
    allowed_sort = {'item_name', 'item_code', 'item_article', 'month_plan'}
    if sort_by not in allowed_sort:
        sort_by = 'item_name'
    sort_dir = (sort_dir or 'asc').lower()
    if sort_dir not in {'asc', 'desc'}:
        sort_dir = 'asc'

    # Пагинация
    p = max(1, int(page or 1))
    ps = max(1, int(page_size or 30))
    offset = (p - 1) * ps

    # Базовый набор строк — корневые изделия
    base_query = db.query(
        Item.item_id,
        Item.item_code,
        Item.item_name,
        Item.item_article
    ).join(RootProduct, Item.item_id == RootProduct.item_id)

    # Общее количество
    total = base_query.count()

    # Применяем сортировку и пагинацию
    if sort_dir == 'desc':
        base_query = base_query.order_by(text(f"{sort_by} DESC"))
    else:
        base_query = base_query.order_by(text(f"{sort_by} ASC"))

    items = base_query.offset(offset).limit(ps).all()

    # Список дат окна (ISO)
    date_list = [(start + timedelta(days=k)).isoformat() for k in range(horizon_days)]

    if not items:
        return {
            "rows": [],
            "dates": date_list,
            "total": total,
            "page": p,
            "page_size": ps,
        }

    # Собираем item_ids страницы
    item_ids = [item.item_id for item in items]

    # Загружаем план по дням только для item_ids страницы
    stage_filter = ""
    params = {"start": start.isoformat(), "end": end.isoformat()}

    if stage_id is not None:
        stage_filter = "AND stage_id = :stage_id"
        params["stage_id"] = stage_id

    sql_days = text(f"""
    SELECT item_id, date, COALESCE(SUM(planned_qty), 0) AS qty
      FROM production_plan_entries
     WHERE item_id IN ({','.join([':id' + str(i) for i in range(len(item_ids))])})
       AND date >= :start
       AND date <  :end
       {stage_filter}
     GROUP BY item_id, date
    """)

    # Добавляем параметры для item_ids
    for i, item_id in enumerate(item_ids):
        params[f"id{i}"] = item_id

    days_map: Dict[int, Dict[str, int]] = {iid: {} for iid in item_ids}
    result = db.execute(sql_days, params)

    for row in result:
        iid = int(row.item_id)
        dval = row.date
        try:
            # нормализуем к ключу формата YYYY-MM-DD
            if hasattr(dval, "strftime"):
                ds = dval.strftime('%Y-%m-%d')
            else:
                ds = str(dval).split('T')[0].split(' ')[0]
        except Exception:
            ds = str(dval).split('T')[0].split(' ')[0]
        q = int(round(float(row.qty or 0.0)))
        if iid in days_map:
            days_map[iid][ds] = q

    # Собираем результатные строки
    result_rows: List[Dict[str, Any]] = []
    for item in items:
        row_days = {d: int(days_map.get(item.item_id, {}).get(d, 0)) for d in date_list}
        month_plan = sum(row_days.values())

        result_rows.append({
            "item_id": item.item_id,
            "item_code": str(item.item_code),
            "item_name": str(item.item_name),
            "item_article": str(item.item_article) if item.item_article else None,
            "month_plan": float(month_plan),
            "days": row_days,
        })

    return {
        "rows": result_rows,
        "dates": date_list,
        "total": total,
        "page": p,
        "page_size": ps,
    }


def upsert_plan_entry(
    item_id: int,
    date_str: str,
    planned_qty: float,
    stage_id: Optional[int] = None,
    db: Session = None,
) -> None:
    """
    Идемпотентно вставляет/обновляет запись плана на указанную дату.
    """
    if db is None:
        db = next(get_db())

    try:
        d = date.fromisoformat(date_str)
    except Exception:
        d = date.today()

    qty = float(planned_qty or 0.0)

    # UPDATE сначала
    update_stmt = db.query(ProductionPlanEntry).filter(
        and_(
            ProductionPlanEntry.item_id == item_id,
            ProductionPlanEntry.date == d,
            ProductionPlanEntry.stage_id == stage_id
        )
    ).update({
        "planned_qty": qty,
        "updated_at": func.now()
    })

    if update_stmt == 0:
        # INSERT если UPDATE не затронул ни одной строки
        new_entry = ProductionPlanEntry(
            item_id=item_id,
            stage_id=stage_id,
            date=d,
            planned_qty=qty,
            completed_qty=0.0,
            status='GREEN',
            notes=None
        )
        db.add(new_entry)

    db.commit()


def bulk_upsert_plan_entries(
    entries: List[Dict[str, Any]],
    db: Session = None,
) -> int:
    """
    Пакетное сохранение записей плана в одной транзакции.
    entries: [{item_id: int, date: 'YYYY-MM-DD', qty: int, stage_id: Optional[int]}]
    Возвращает количество успешно обработанных записей.
    """
    if db is None:
        db = next(get_db())

    if not entries:
        return 0

    # Предвалидация и нормализация
    normalized: List[Dict[str, Any]] = []
    for e in entries:
        try:
            iid = int(e.get('item_id'))
            d = str(e.get('date') or '').strip()
            _ = date.fromisoformat(d)  # валидация даты
            qty = int(e.get('qty') or 0)
            stg = e.get('stage_id', None)
            stg = int(stg) if (stg is not None and str(stg).strip() != '') else None
        except Exception:
            continue
        normalized.append({'item_id': iid, 'date': d, 'qty': qty, 'stage_id': stg})

    if not normalized:
        return 0

    try:
        saved = 0
        for e in normalized:
            # UPDATE сначала
            update_stmt = db.query(ProductionPlanEntry).filter(
                and_(
                    ProductionPlanEntry.item_id == e['item_id'],
                    ProductionPlanEntry.date == date.fromisoformat(e['date']),
                    ProductionPlanEntry.stage_id == e['stage_id']
                )
            ).update({
                "planned_qty": float(e['qty'] or 0),
                "updated_at": func.now()
            })

            if update_stmt == 0:
                # INSERT если UPDATE не затронул ни одной строки
                new_entry = ProductionPlanEntry(
                    item_id=e['item_id'],
                    stage_id=e['stage_id'],
                    date=date.fromisoformat(e['date']),
                    planned_qty=float(e['qty'] or 0),
                    completed_qty=0.0,
                    status='GREEN',
                    notes=None
                )
                db.add(new_entry)

            saved += 1

        db.commit()
        return saved
    except Exception:
        db.rollback()
        return 0


def delete_plan_rows_for_item(
    start_date_str: str,
    days: int,
    item_id: int,
    stage_id: Optional[int] = None,
    db: Session = None,
) -> int:
    """
    Удаляет записи плана для изделия item_id в интервале [start; start+days).
    Если указан stage_id — удаляет только в рамках этого этапа.
    Возвращает количество удалённых строк.
    """
    if db is None:
        db = next(get_db())

    try:
        start = date.fromisoformat(start_date_str)
    except Exception:
        start = date.today()

    end = start + timedelta(days=max(1, int(days or 1)))

    query = db.query(ProductionPlanEntry).filter(
        and_(
            ProductionPlanEntry.item_id == item_id,
            ProductionPlanEntry.date >= start,
            ProductionPlanEntry.date < end
        )
    )

    if stage_id is not None:
        query = query.filter(ProductionPlanEntry.stage_id == stage_id)

    deleted_count = query.count()
    query.delete()
    db.commit()

    return deleted_count


def delete_root_product_for_item(
    item_id: int,
    db: Session = None,
) -> int:
    """
    Удаляет запись из root_products для указанного item_id.
    Возвращает количество удалённых строк (0 или 1).
    """
    if db is None:
        db = next(get_db())

    deleted = db.query(RootProduct).filter(RootProduct.item_id == item_id).delete()
    db.commit()
    return int(deleted)


def ensure_root_product_by_code(
    item_code: str,
    item_name: Optional[str] = None,
    item_article: Optional[str] = None,
    db: Session = None,
) -> int:
    """
    Гарантирует наличие строки плана (root_products) для указанного item_code.
    1) Обеспечивает наличие записи в items (мягкий upsert полей name/article)
    2) Вставляет строку в root_products (INSERT OR IGNORE)
    Возвращает item_id.
    """
    if db is None:
        db = next(get_db())

    # Ищем или создаем item
    item = db.query(Item).filter(Item.item_code == item_code).first()

    if item:
        item_id = item.item_id
        # Мягкое обновление name/article при наличии новых значений
        if item_name and item_name.strip():
            item.item_name = item_name.strip()
        if item_article and item_article.strip():
            item.item_article = item_article.strip()
        item.updated_at = func.now()
    else:
        # Создаем новый item
        new_item = Item(
            item_code=item_code,
            item_name=item_name or item_code,
            item_article=item_article,
            stock_qty=0.0,
            status='active'
        )
        db.add(new_item)
        db.flush()  # Получаем item_id
        item_id = new_item.item_id

    # Добавляем в root_products если еще нет
    existing_root = db.query(RootProduct).filter(RootProduct.item_id == item_id).first()
    if not existing_root:
        root_product = RootProduct(item_id=item_id)
        db.add(root_product)

    db.commit()
    return item_id