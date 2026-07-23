from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, Iterable, Optional, Tuple


EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"
DEFAULT_ORGANIZATION_REF1C = "c78bcd0e-81f0-11ee-9ce5-9ee51454587f"
DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C = "c74ea54c-d1b2-11ef-9e01-9ee51454587f"
UNIT_TYPE_1C = "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"
ORIGIN_MARKER = "prodplan-origin="


def fmt_1c_datetime(value: Optional[date]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return datetime.combine(value, datetime.min.time()).isoformat()


def current_1c_datetime() -> str:
    try:
        return datetime.now(ZoneInfo("Europe/Moscow")).replace(microsecond=0).isoformat()
    except Exception:
        return fmt_1c_datetime(datetime.now().replace(microsecond=0)) or ""


def clean_ref1c(value: Any, *, empty_ref: str = EMPTY_REF1C) -> str:
    ref = str(value or "").strip()
    if not ref or ref == empty_ref:
        return ""
    return ref


def config_ref1c(config: Dict[str, Any], key: str, fallback: Optional[str] = None) -> str:
    return clean_ref1c(config.get(key) or fallback)


def add_unit_payload(row: Dict[str, Any], unit_value: Any) -> None:
    unit_ref = clean_ref1c(unit_value)
    if unit_ref:
        row["ЕдиницаИзмерения"] = unit_ref
        row["ЕдиницаИзмерения_Type"] = UNIT_TYPE_1C


def payload_hash(payload: Dict[str, Any]) -> str:
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(payload)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def origin_token(namespace: str, identity: Any) -> str:
    """Stable, short identity shared by parallel PRODPLAN instances.

    Unlike ``sync_link`` this marker lives in 1C, so a restored/cloned database
    can recover a document that another instance (or a previous retry) already
    created.  Callers must pass a durable business identity; volatile document
    dates and DB connection-specific state do not belong here.
    """
    normalized = json.dumps(
        {"namespace": str(namespace), "identity": identity},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def add_origin_marker(comment: Any, token: str) -> str:
    text = str(comment or "").strip()
    marker = f"{ORIGIN_MARKER}{token}"
    if marker in text:
        return text
    return f"{text}; {marker}" if text else marker


def find_document_by_origin(
    client: Any,
    *,
    entity: str,
    token: str,
    select_fields: Optional[list[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Find a 1C document previously created for ``token``.

    OData ``substringof`` is supported by the target 1C endpoint and avoids
    relying on a local ``sync_link``.  Older unit-test fakes without ``get_all``
    simply exercise the historical POST path.
    """
    get_all = getattr(client, "get_all", None)
    if get_all is None:
        return None
    marker = f"{ORIGIN_MARKER}{token}".replace("'", "''")
    rows = get_all(
        entity,
        filter_query=f"substringof('{marker}', Комментарий)",
        select_fields=select_fields or ["Ref_Key", "Number", "Комментарий", "Posted"],
        top=2,
        max_records=2,
        max_pages=1,
        order_by=None,
    )
    if len(rows) > 1:
        raise RuntimeError(
            f"В 1С найдено несколько документов с {ORIGIN_MARKER}{token}; "
            "автоматическое восстановление небезопасно"
        )
    return rows[0] if rows else None


def is_demo_base_url(base_url: str) -> bool:
    return "unf_demo" in (base_url or "").lower()


def create_odata_client(
    config: Dict[str, Any],
    client_factory: Any,
    *,
    allow_production: bool = False,
    require_demo_base: bool = False,
) -> Any:
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("OData config is not set. Save 1C connection settings first.")
    if require_demo_base and not is_demo_base_url(base_url) and not allow_production:
        raise PermissionError(
            f"Refusing to write to non-demo base_url '{base_url}'. "
            "Pass allow_production=true to override (use with caution)."
        )
    return client_factory(
        base_url=base_url,
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
    )


def find_sync_link(
    db: Any,
    sync_link_model: Any,
    *,
    source_doctype: str,
    source_id: int,
    target_entity: str,
) -> Any:
    return (
        db.query(sync_link_model)
        .filter(
            sync_link_model.source_system == "PRODPLAN",
            sync_link_model.source_doctype == source_doctype,
            sync_link_model.source_id == int(source_id),
            sync_link_model.target_entity == target_entity,
        )
        .one_or_none()
    )


def upsert_sync_link(
    db: Any,
    sync_link_model: Any,
    *,
    source_doctype: str,
    source_id: int,
    target_entity: str,
    target_number: str,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    existing = find_sync_link(
        db,
        sync_link_model,
        source_doctype=source_doctype,
        source_id=source_id,
        target_entity=target_entity,
    )
    synced_at = datetime.now(timezone.utc) if status == "success" else None
    if existing is None:
        db.add(
            sync_link_model(
                source_system="PRODPLAN",
                source_doctype=source_doctype,
                source_id=int(source_id),
                target_system="1C",
                target_entity=target_entity,
                target_ref_key=target_ref_key,
                target_number=target_number,
                payload_hash=payload_hash,
                status=status,
                last_error=last_error,
                last_synced_at=synced_at,
            )
        )
        return

    existing.target_number = target_number
    existing.payload_hash = payload_hash
    existing.status = status
    existing.last_error = last_error
    if target_ref_key:
        existing.target_ref_key = target_ref_key
    if synced_at is not None:
        existing.last_synced_at = synced_at


def post_document_operational(
    client: Any,
    *,
    entity: str,
    ref_key: str,
    unpost_first: bool = False,
) -> None:
    ref = clean_ref1c(ref_key)
    if not ref:
        raise ValueError("Ref_Key is required for 1C posting")
    base = f"{entity}(guid'{ref}')"
    post_operation = getattr(client, "post_operation", None)
    if post_operation is None:
        # Unit-test fakes created before operational posting usually expose
        # only `post`. The real OData1CClient has `post_operation`; skip here
        # instead of turning the action into a fake document POST.
        return
    if unpost_first:
        post_operation(f"{base}/Unpost")
    post_operation(f"{base}/Post?PostingModeOperational=true")


def post_export_entries(
    db: Any,
    *,
    entries: Iterable[Tuple[Any, Dict[str, Any]]],
    client: Any,
    target_entity: str,
    missing_ref_error: str,
    upsert_link: Callable[..., None],
    on_success: Callable[[Any, str], None],
    on_error: Optional[Callable[[Any, str], None]] = None,
    log_error: Optional[Callable[[Any], str]] = None,
) -> Tuple[int, int]:
    created = 0
    errored = 0
    for entry, payload_envelope in entries:
        payload = payload_envelope["payload"]
        try:
            phash = payload_hash(payload)
            existing_ref_key = clean_ref1c(getattr(entry, "target_ref_key", None))
            upsert_link(
                entry=entry,
                payload_hash=phash,
                target_ref_key=existing_ref_key or None,
                status="planned",
                last_error=None,
            )
            db.flush()

            if existing_ref_key:
                if getattr(entry, "unpost_before_patch", False):
                    post_operation = getattr(client, "post_operation", None)
                    if post_operation is not None:
                        post_operation(f"{target_entity}(guid'{existing_ref_key}')/Unpost")
                patch = getattr(client, "patch", None)
                if patch is None:
                    raise RuntimeError("1C document already exists, but OData client cannot patch it")
                patch(f"{target_entity}(guid'{existing_ref_key}')", payload)
                ref_key = existing_ref_key
            else:
                created_header = client.post(target_entity, payload)
                ref_key = clean_ref1c(created_header.get("Ref_Key"))
                if not ref_key:
                    raise RuntimeError(missing_ref_error)

            entry.target_ref_key = ref_key
            entry.status = "created"

            upsert_link(
                entry=entry,
                payload_hash=phash,
                target_ref_key=ref_key,
                status="success",
                last_error=None,
            )
            on_success(entry, ref_key)
            created += 1
            # Persist each successful export immediately. A single commit at the
            # end of the batch meant a crash (or a later DB failure) after a 1C
            # POST but before that commit lost the stored Ref_Key, so a re-run
            # re-POSTed and created a duplicate document in 1C. Committing per
            # entry bounds that window to the single in-flight document.
            db.commit()
        except Exception as exc:
            # Read the ref_key before any rollback: if the POST succeeded and a
            # later step (on_success/link) failed, we still want to record the
            # Ref_Key so a re-run PATCHes the existing doc instead of duplicating.
            existing_ref_key = clean_ref1c(getattr(entry, "target_ref_key", None))
            entry.status = "error"
            entry.error = str(exc)
            errored += 1
            try:
                upsert_link(
                    entry=entry,
                    payload_hash=payload_hash(payload),
                    target_ref_key=existing_ref_key or None,
                    status="error",
                    last_error=str(exc),
                )
                if on_error is not None:
                    on_error(entry, str(exc))
                db.commit()
            except Exception:
                # Keep the session usable for the remaining entries.
                db.rollback()
            if log_error is not None:
                try:
                    print(log_error(entry))
                except Exception:
                    pass
    return created, errored
