"""Read-only диагностика масштаба каскадов ремонтного модуля спецификаций.

Считает, сколько строк состава несут закреплённую спецификацию компонента
(SpecComponent.component_spec_ref1c). Именно эти строки требуют каскадного PATCH
при смене основной спеки детали (родители, явно закрепившие старую спеку).

ВАЖНО: колонка заполняется только при выгрузке спецификаций ПОСЛЕ Этапа 0.
На «старой» БД до повторного sync значения будут NULL — сначала прогнать
sync спецификаций, потом эту диагностику.

Запуск (read-only, ничего не пишет):
    cd backend && python -m app.scripts.diagnose_pinned_component_specs
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import func

from app.database import SessionLocal
from app.models import SpecComponent


def main() -> None:
    db = SessionLocal()
    try:
        total = db.query(func.count(SpecComponent.component_id)).scalar() or 0
        pinned = (
            db.query(func.count(SpecComponent.component_id))
            .filter(SpecComponent.component_spec_ref1c.isnot(None))
            .scalar()
            or 0
        )

        print("=== Диагностика закреплённых спецификаций компонентов ===")
        print(f"Всего строк состава:                 {total}")
        print(f"С закреплённой спекой (не NULL):      {pinned}")
        print(f"Доля:                                 {(pinned / total * 100 if total else 0):.1f}%")

        # Сколько РАЗНЫХ дочерних спек закреплено (= потенциальные точки смены вида).
        distinct_child_specs = (
            db.query(func.count(func.distinct(SpecComponent.component_spec_ref1c)))
            .filter(SpecComponent.component_spec_ref1c.isnot(None))
            .scalar()
            or 0
        )
        print(f"Уникальных закреплённых спек:          {distinct_child_specs}")

        # Топ дочерних спек по числу родительских строк — оценка «тяжести» каскада
        # при смене основной спеки соответствующей детали.
        rows = (
            db.query(SpecComponent.component_spec_ref1c)
            .filter(SpecComponent.component_spec_ref1c.isnot(None))
            .all()
        )
        counter = Counter(r[0] for r in rows)
        if counter:
            print("\nТоп закреплённых спек по числу родительских строк (каскад при смене):")
            for ref, cnt in counter.most_common(15):
                print(f"  {ref}  ->  {cnt} строк состава у родителей")
    finally:
        db.close()


if __name__ == "__main__":
    main()
