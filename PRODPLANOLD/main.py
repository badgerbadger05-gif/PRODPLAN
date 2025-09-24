from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Обеспечить импорт пакета src при запуске из корня
PROJECT_ROOT = Path(__file__).parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import init_database  # noqa: E402
from src.planner import generate_production_plan  # noqa: E402
from src.stock_history import sync_stock_with_history  # noqa: E402
from src.order_calculator import cmd_calculate_orders # noqa: E402
from src.odata_stock_sync import sync_stock_from_odata  # noqa: E402


def cmd_init_db(args: argparse.Namespace) -> None:
    """
    Инициализация схемы БД (идемпотентно).
    """
    db_path = Path(args.db) if args.db else None
    init_database(db_path=db_path)
    print(f"OK: init-db at {db_path or 'data/specifications.db'}")


def cmd_sync_stock_odata(args: argparse.Namespace) -> None:
   """
   Синхронизация остатков из 1С через OData.
   """
   db_path = Path(args.db) if args.db else None
   
   # Гарантируем наличие столбца items.stock_qty через идемпотентную инициализацию/миграцию
   init_database(db_path=db_path)
   
   stats = sync_stock_from_odata(
       base_url=args.url,
       entity_name=args.entity,
       username=getattr(args, 'username', None),
       password=getattr(args, 'password', None),
       token=getattr(args, 'token', None),
       filter_query=getattr(args, 'filter', None),
       select_fields=getattr(args, 'select', None),
       db_path=db_path,
       dry_run=args.dry_run
   )
   
   # sync_stock_from_odata печатает JSON; дополнительно выведем человекочитаемую строку
   print(f"OK: sync-stock-odata url={args.url} entity={args.entity} dry_run={args.dry_run}")

def _parse_date(value: str | None):
    if not value:
        return None
    # Поддержка ISO (YYYY-MM-DD) и DD.MM.YYYY
    try:
        from datetime import date
        if "-" in value:
            y, m, d = value.split("-")
            return date(int(y), int(m), int(d))
        if "." in value:
            d, m, y = value.split(".")
            return date(int(y), int(m), int(d))
    except Exception:
        return None
    return None


def cmd_sync_stock_history(args: argparse.Namespace) -> None:
    """
    Синхронизация остатков с сохранением истории: Excel → БД (items.stock_qty) + история.
    """
    db_path = Path(args.db) if args.db else None
    # Приоритет: --dir (каталог). Для обратной совместимости поддерживаем --path (файл/каталог).
    stock_path: Path
    if getattr(args, "dir", None):
        stock_path = Path(args.dir)
    elif getattr(args, "path", None):
        stock_path = Path(args.path)
    else:
        stock_path = Path("ostatki")
    # Гарантируем наличие столбца items.stock_qty через идемпотентную инициализацию/миграцию
    init_database(db_path=db_path)
    stats = sync_stock_with_history(stock_path, db_path=db_path, dry_run=args.dry_run)
    # sync_stock печатает JSON; дополнительно выведем человекочитаемую строку
    print(f"OK: sync-stock-history src={stock_path} dry_run={args.dry_run}")


def cmd_generate_plan(args: argparse.Namespace) -> None:
    """
    Генерация файла production_plan.xlsx на основе корневых изделий из БД.
    """
    db_path = Path(args.db) if args.db else None
    out_path = Path(args.out) if args.out else Path("output/production_plan.xlsx")
    start_date = _parse_date(args.start_date)
    result = generate_production_plan(
        db_path=db_path,
        output_path=out_path,
        horizon_days=args.days,
        start_date=start_date,
    )
    print(str(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prodplan",
        description="Система планирования производства: управление схемой БД и синхронизацией спецификаций.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init-db
    p_init = sub.add_parser("init-db", help="Инициализация схемы SQLite БД")
    p_init.add_argument("--db", type=str, default=None, help="Путь к SQLite БД (по умолчанию data/specifications.db)")
    p_init.set_defaults(func=cmd_init_db)

    # sync-stock-history
    p_stock_hist = sub.add_parser("sync-stock-history", help="Синхронизация остатков с сохранением истории: Excel → БД (items.stock_qty) + история")
    p_stock_hist.add_argument("--db", type=str, default=None, help="Путь к SQLite БД (по умолчанию data/specifications.db)")
    p_stock_hist.add_argument("--dir", type=str, default="ostatki", help="Каталог с Excel-файлами остатков (default: ostatki)")
    p_stock_hist.add_argument("--path", type=str, default=None, help="Путь к Excel-файлу остатков (для обратной совместимости)")
    p_stock_hist.add_argument("--dry-run", action="store_true", help="Режим без записи (оценка покрытия и изменений)")
    p_stock_hist.set_defaults(func=cmd_sync_stock_history)

    # generate-plan
    p_plan = sub.add_parser("generate-plan", help="Сформировать production_plan.xlsx")
    p_plan.add_argument("--db", type=str, default=None, help="Путь к SQLite БД (по умолчанию data/specifications.db)")
    p_plan.add_argument("--out", type=str, default="output/production_plan.xlsx", help="Путь к выходному Excel")
    p_plan.add_argument("--days", type=int, default=30, help="Горизонт планирования в днях (по умолчанию 30)")
    p_plan.add_argument("--start-date", type=str, default=None, help="Дата начала (YYYY-MM-DD или DD.MM.YYYY)")
    p_plan.set_defaults(func=cmd_generate_plan)

    # calculate-orders
    p_orders = sub.add_parser("calculate-orders", help="Рассчитать заказы на производство и закупку")
    p_orders.add_argument("--db", type=str, default=None, help="Путь к SQLite БД (по умолчанию data/specifications.db)")
    p_orders.add_argument("--output", type=str, default="output", help="Каталог для выходных файлов (по умолчанию output)")
    p_orders.set_defaults(func=cmd_calculate_orders)
    
    # sync-stock-odata
    p_stock_odata = sub.add_parser("sync-stock-odata", help="Синхронизация остатков: 1С OData → БД (items.stock_qty)")
    p_stock_odata.add_argument("--db", type=str, default=None, help="Путь к SQLite БД (по умолчанию data/specifications.db)")
    p_stock_odata.add_argument("--url", type=str, required=True, help="URL OData сервиса 1С")
    p_stock_odata.add_argument("--entity", type=str, required=True, help="Имя сущности OData (например, ОстаткиНоменклатуры)")
    p_stock_odata.add_argument("--username", type=str, default=None, help="Имя пользователя для Basic аутентификации")
    p_stock_odata.add_argument("--password", type=str, default=None, help="Пароль для Basic аутентификации")
    p_stock_odata.add_argument("--token", type=str, default=None, help="Токен для Bearer аутентификации")
    p_stock_odata.add_argument("--filter", type=str, default=None, help="Фильтр OData (например, ДатаОстатка eq datetime'2023-01-01')")
    p_stock_odata.add_argument("--select", type=str, default=None, help="Поля для выборки (через запятую)")
    p_stock_odata.add_argument("--dry-run", action="store_true", help="Режим без записи (оценка покрытия и изменений)")
    p_stock_odata.set_defaults(func=cmd_sync_stock_odata)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())