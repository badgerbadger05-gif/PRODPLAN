"""Список участков больше не режется на 100, поиск номенклатуры — только текстовый."""

from inspect import signature

from app.models import Item, ItemEmbedding, ProductionResource
from app.routers.resources import get_resources
from app.services.nomenclature_search import search_nomenclature_service


def _make_items(db_session, count: int) -> None:
    for i in range(count):
        db_session.add(
            Item(
                item_code=f"CODE-{i:04d}",
                item_name=f"Кронштейн опорный {i}",
                item_article=f"ART-{i:04d}",
                status="active",
            )
        )
    db_session.commit()


def test_get_resources_returns_more_than_legacy_hardcoded_limit(db_session):
    for i in range(150):
        db_session.add(ProductionResource(resource_name=f"Участок {i:03d}"))
    db_session.commit()

    # Дефолт лимита берём из сигнатуры — он должен быть заметно больше 100.
    default_limit = signature(get_resources).parameters["limit"].default.default
    assert default_limit >= 500

    resources = get_resources(skip=0, limit=default_limit, db=db_session)

    assert len(resources) == 150


def test_get_resources_honours_skip_and_limit(db_session):
    for i in range(10):
        db_session.add(ProductionResource(resource_name=f"Участок {i:03d}"))
    db_session.commit()

    page = get_resources(skip=3, limit=4, db=db_session)

    assert [r.resource_name for r in page] == [
        "Участок 003",
        "Участок 004",
        "Участок 005",
        "Участок 006",
    ]


def test_search_is_text_based(db_session):
    _make_items(db_session, 3)

    results = search_nomenclature_service(db_session, "Кронштейн", limit=10)

    assert {r["item_code"] for r in results} == {"CODE-0000", "CODE-0001", "CODE-0002"}
    assert all(r["similarity"] == 1.0 for r in results)


def test_stored_embeddings_no_longer_shadow_text_search(db_session):
    """Раньше непустой item_embeddings подменял текстовый поиск псевдовекторами."""
    _make_items(db_session, 2)
    for item in db_session.query(Item).all():
        db_session.add(
            ItemEmbedding(
                item_id=item.item_id,
                embedding_vector="[0.1, 0.2, 0.3, 0.4]",
                model_name="legacy-md5-stub",
            )
        )
    db_session.commit()

    results = search_nomenclature_service(db_session, "Кронштейн", limit=10)

    assert {r["item_code"] for r in results} == {"CODE-0000", "CODE-0001"}


def test_short_query_returns_nothing(db_session):
    _make_items(db_session, 1)

    assert search_nomenclature_service(db_session, "К", limit=10) == []


def test_generate_embeddings_endpoint_is_gone():
    from app.routers import nomenclature

    paths = {route.path for route in nomenclature.router.routes}
    assert "/v1/nomenclature/generate-embeddings" not in paths
