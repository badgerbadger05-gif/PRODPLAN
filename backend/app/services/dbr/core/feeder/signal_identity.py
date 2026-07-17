"""Stable identity and lifecycle constants for feeder signals.

The identity is deliberately independent from Frappe so it can be used by
DocType validation, generators, migration patches and pure tests.  A unique
``dedup_key`` in MariaDB is the final guard against two workers creating the
same live signal concurrently.


Портировано из prodflow prodflow/services/feeder/signal_identity.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

import hashlib
import json

OPEN = "Open"
ORDER_CREATED = "Order Created"
IN_WORK = "In Work"
DONE = "Done"
CANCELLED = "Cancelled"

LIVE_STATUSES = (OPEN, ORDER_CREATED, IN_WORK)


def is_live(status: str | None) -> bool:
	return (status or "") in LIVE_STATUSES


def build_dedup_key(
	signal_type: str | None,
	*,
	supermarket_position: str | None = None,
	item: str | None = None,
	warehouse: str | None = None,
	drum_slot: str | None = None,
	parent_signal: str | None = None,
) -> str:
	"""Return a short, stable key for one *live* business signal.

	The readable prefix helps diagnostics; the SHA-256 body keeps the indexed
	value short even when Warehouse/Item names are long.
	"""
	type_name = (signal_type or "").strip()
	if type_name == "Пополнение":
		prefix = "R"
		identity = [supermarket_position or "", item or "", warehouse or ""]
	elif type_name == "Под график":
		prefix = "S"
		identity = [drum_slot or "", item or "", warehouse or ""]
	elif type_name == "Цепочка":
		prefix = "C"
		identity = [parent_signal or "", item or "", warehouse or ""]
	else:
		prefix = "X"
		identity = [type_name, supermarket_position or "", drum_slot or "", parent_signal or "", item or "", warehouse or ""]
	payload = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
	return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def key_from_mapping(values: dict) -> str:
	return build_dedup_key(
		values.get("signal_type"),
		supermarket_position=values.get("supermarket_position"),
		item=values.get("item"),
		warehouse=values.get("warehouse"),
		drum_slot=values.get("drum_slot"),
		parent_signal=values.get("parent_signal"),
	)
