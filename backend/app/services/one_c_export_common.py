from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, Optional, Tuple


EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


def fmt_1c_datetime(value: Optional[date]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return datetime.combine(value, datetime.min.time()).isoformat()


def clean_ref1c(value: Any, *, empty_ref: str = EMPTY_REF1C) -> str:
    ref = str(value or "").strip()
    if not ref or ref == empty_ref:
        return ""
    return ref


def payload_hash(payload: Dict[str, Any]) -> str:
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(payload)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
    synced_at = datetime.utcnow() if status == "success" else None
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
            upsert_link(
                entry=entry,
                payload_hash=phash,
                target_ref_key=None,
                status="planned",
                last_error=None,
            )
            db.flush()

            created_header = client.post(target_entity, payload)
            ref_key = clean_ref1c(created_header.get("Ref_Key"))
            if not ref_key:
                raise RuntimeError(missing_ref_error)

            entry.target_ref_key = ref_key
            entry.status = "created"
            created += 1

            upsert_link(
                entry=entry,
                payload_hash=phash,
                target_ref_key=ref_key,
                status="success",
                last_error=None,
            )
            on_success(entry, ref_key)
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)
            errored += 1
            try:
                upsert_link(
                    entry=entry,
                    payload_hash=payload_hash(payload),
                    target_ref_key=None,
                    status="error",
                    last_error=str(exc),
                )
                if on_error is not None:
                    on_error(entry, str(exc))
            except Exception:
                pass
            if log_error is not None:
                try:
                    print(log_error(entry))
                except Exception:
                    pass
    db.commit()
    return created, errored

