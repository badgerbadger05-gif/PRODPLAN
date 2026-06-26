"""HTTP API ремонтного модуля (операция A): restage / move / add."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Item, ProductionStage, SpecComponent, Specification
from app.routers.specification_repair import router
from app.services import spec_writeback_1c

API = "/api/v1/specification-repair"


class _Fake1C:
    """Фейк OData-клиента: отдаёт Состав по ref и записывает PATCH-и."""

    def __init__(self, specs=None, fail=False):
        self.specs = specs or {}
        self.patches = []
        self.fail = fail

    def get_all(self, entity, filter_query=None, select_fields=None):
        import re
        m = re.search(r"guid'([^']+)'", filter_query or "")
        ref = m.group(1) if m else None
        if ref in self.specs:
            return [{"Ref_Key": ref, "Состав": self.specs[ref]}]
        return []

    def patch(self, endpoint, payload):
        if self.fail:
            raise RuntimeError("1С недоступна")
        self.patches.append((endpoint, payload))
        return {"status": "ok"}


@pytest.fixture()
def fake_1c(monkeypatch):
    holder = {}

    def install(specs=None, fail=False):
        fc = _Fake1C(specs=specs, fail=fail)
        monkeypatch.setattr(spec_writeback_1c, "build_client_from_config", lambda: fc)
        holder["client"] = fc
        return fc

    install.get = lambda: holder.get("client")
    return install


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_move(db):
    a = Specification(spec_code="A", spec_name="A", spec_ref1c="a")
    b = Specification(spec_code="B", spec_name="B", spec_ref1c="b")
    part = Item(item_code="P", item_name="Деталь", item_ref1c="p")
    db.add_all([a, b, part])
    db.commit()
    comp = SpecComponent(spec_id=a.spec_id, item_id=part.item_id, quantity=1, component_type="Сборка")
    db.add(comp)
    db.commit()
    return a, b, part, comp


def test_move_dry_run_previews_without_change(client, db_session):
    a, b, part, comp = _seed_move(db_session)

    resp = client.post(f"{API}/move", json={"component_id": comp.component_id, "target_spec_id": b.spec_id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True and body["to_spec_id"] == b.spec_id
    assert "pending_1c" in body and set(body["pending_1c"]["specs"]) == {a.spec_id, b.spec_id}
    # dry-run не изменил БД
    assert db_session.query(SpecComponent).filter_by(spec_id=a.spec_id).count() == 1
    assert db_session.query(SpecComponent).filter_by(spec_id=b.spec_id).count() == 0


def test_move_apply_relocates(client, db_session, fake_1c):
    a, b, part, comp = _seed_move(db_session)
    fc = fake_1c(specs={
        "a": [{"Номенклатура_Key": "p", "Спецификация_Key": "00000000-0000-0000-0000-000000000000",
               "Этап_Key": "st", "Количество": 1}],
        "b": [],
    })

    resp = client.post(
        f"{API}/move",
        json={"component_id": comp.component_id, "target_spec_id": b.spec_id, "dry_run": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    # 1С-запись произошла: сначала target, потом source
    assert [p[0] for p in fc.patches] == [
        "Catalog_Спецификации(guid'b')", "Catalog_Спецификации(guid'a')",
    ]
    assert body["writeback_1c"]["op"] == "move"
    # локальная БД-зеркало обновилась
    assert db_session.query(SpecComponent).filter_by(spec_id=a.spec_id).count() == 0
    assert db_session.query(SpecComponent).filter_by(spec_id=b.spec_id).count() == 1


def test_restage_apply_writes_to_1c(client, db_session, fake_1c):
    sp = Specification(spec_code="S", spec_name="S", spec_ref1c="s")
    it = Item(item_code="X", item_name="X", item_ref1c="x")
    st_old = ProductionStage(stage_name="Сборка", stage_ref1c="st-old")
    st_new = ProductionStage(stage_name="Испытания", stage_ref1c="st-new")
    db_session.add_all([sp, it, st_old, st_new])
    db_session.commit()
    comp = SpecComponent(spec_id=sp.spec_id, item_id=it.item_id, quantity=1,
                         component_type="Материал", stage_id=st_old.stage_id)
    db_session.add(comp)
    db_session.commit()

    fc = fake_1c(specs={"s": [{"Номенклатура_Key": "x",
                               "Спецификация_Key": "00000000-0000-0000-0000-000000000000",
                               "Этап_Key": "st-old", "Количество": 1}]})

    resp = client.post(
        f"{API}/restage",
        json={"component_id": comp.component_id, "new_stage_id": st_new.stage_id, "dry_run": False},
    )

    assert resp.status_code == 200
    assert resp.json()["writeback_1c"]["changed"] == 1
    _, payload = fc.patches[0]
    assert payload["Состав"][0]["Этап_Key"] == "st-new"
    assert db_session.query(SpecComponent).filter_by(component_id=comp.component_id).first().stage_id == st_new.stage_id


def test_apply_returns_502_and_keeps_db_when_1c_fails(client, db_session, fake_1c):
    a, b, part, comp = _seed_move(db_session)
    fake_1c(specs={"a": [{"Номенклатура_Key": "p",
                          "Спецификация_Key": "00000000-0000-0000-0000-000000000000",
                          "Этап_Key": "st", "Количество": 1}], "b": []}, fail=True)

    resp = client.post(
        f"{API}/move",
        json={"component_id": comp.component_id, "target_spec_id": b.spec_id, "dry_run": False},
    )

    assert resp.status_code == 502
    # 1С упала ДО локальной мутации — БД не тронута
    assert db_session.query(SpecComponent).filter_by(spec_id=a.spec_id).count() == 1
    assert db_session.query(SpecComponent).filter_by(spec_id=b.spec_id).count() == 0


def test_dry_run_does_not_touch_1c(client, db_session, fake_1c):
    a, b, part, comp = _seed_move(db_session)
    fc = fake_1c(specs={"a": [], "b": []})

    resp = client.post(f"{API}/move", json={"component_id": comp.component_id, "target_spec_id": b.spec_id})

    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert fc.patches == []  # dry-run в 1С не ходит
    assert "writeback_1c" not in resp.json()


def test_move_same_spec_returns_400(client, db_session):
    a, b, part, comp = _seed_move(db_session)
    resp = client.post(f"{API}/move", json={"component_id": comp.component_id, "target_spec_id": a.spec_id})
    assert resp.status_code == 400


def test_add_apply_inserts(client, db_session, fake_1c):
    sp = Specification(spec_code="S", spec_name="S", spec_ref1c="s")
    it = Item(item_code="X", item_name="X", item_ref1c="x", unit="PCE")
    db_session.add_all([sp, it])
    db_session.commit()
    fc = fake_1c(specs={"s": [{"Номенклатура_Key": "z",
                              "Спецификация_Key": "00000000-0000-0000-0000-000000000000",
                              "Этап_Key": "st", "Количество": 1, "СпособПополнения": "Закупка"}]})

    resp = client.post(
        f"{API}/add",
        json={"spec_id": sp.spec_id, "item_id": it.item_id, "quantity": 3, "dry_run": False},
    )

    assert resp.status_code == 200
    assert resp.json()["writeback_1c"]["op"] == "add"
    _, payload = fc.patches[0]
    added = next(r for r in payload["Состав"] if r["Номенклатура_Key"] == "x")
    assert added["Количество"] == 3 and added["ЕдиницаИзмерения"] == "PCE"
    assert db_session.query(SpecComponent).filter_by(spec_id=sp.spec_id, item_id=it.item_id).count() == 1
