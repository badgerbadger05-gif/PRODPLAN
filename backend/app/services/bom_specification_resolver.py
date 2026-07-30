"""Canonical specification selection for BOM edges.

``SpecComponent.component_spec_ref1c`` is an explicit 1C pin.  When present it
always wins over the child's default specification and must resolve to exactly
one local ``Specification``.  Silently falling back on a missing/corrupt pin
would expand and export a different BOM, so pinned resolution fails closed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models import DefaultSpecification, SpecComponent, Specification


class BomSpecificationResolutionError(ValueError):
    """A BOM specification selector cannot be resolved without guessing."""


def _norm_ref(value: object) -> str:
    return str(value or "").strip()


class BomSpecificationResolver:
    """One session-scoped owner for default and pinned BOM specification lookup."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._specs_by_ref: dict[str, list[Specification]] = defaultdict(list)
        self._specs_loaded = False
        self._defaults_by_item: dict[int, list[DefaultSpecification]] = defaultdict(list)
        self._defaults_loaded = False
        self._components_by_spec: dict[int, list[SpecComponent]] = defaultdict(list)
        self._components_loaded = False

    def _load_specs(self) -> None:
        if self._specs_loaded:
            return
        for spec in (
            self._db.query(Specification)
            .order_by(Specification.spec_id.asc())
            .all()
        ):
            ref = _norm_ref(spec.spec_ref1c)
            if ref:
                self._specs_by_ref[ref].append(spec)
        self._specs_loaded = True

    def _load_defaults(self) -> None:
        if self._defaults_loaded:
            return
        for row in (
            self._db.query(DefaultSpecification)
            .order_by(
                DefaultSpecification.item_id.asc(),
                DefaultSpecification.id.asc(),
            )
            .all()
        ):
            self._defaults_by_item[int(row.item_id)].append(row)
        self._defaults_loaded = True

    def _load_components(self) -> None:
        if self._components_loaded:
            return
        for component in (
            self._db.query(SpecComponent)
            .order_by(
                SpecComponent.spec_id.asc(),
                SpecComponent.component_id.asc(),
            )
            .all()
        ):
            self._components_by_spec[int(component.spec_id)].append(component)
        self._components_loaded = True

    def default_spec_id(self, item_id: int) -> int | None:
        """Resolve the unpinned item default deterministically.

        MRP currently has no characteristic axis.  Multiple distinct defaults
        therefore cannot be selected safely and fail closed instead of taking
        an arbitrary first/last row.
        """
        self._load_defaults()
        rows = self._defaults_by_item.get(int(item_id), ())
        spec_ids = sorted({int(row.spec_id) for row in rows})
        if not spec_ids:
            return None
        if len(spec_ids) != 1:
            raise BomSpecificationResolutionError(
                f"item_id={int(item_id)} has ambiguous default specifications: "
                f"{spec_ids}"
            )
        return spec_ids[0]

    def pinned_spec(self, component: SpecComponent) -> Specification | None:
        """Return the exact pinned child specification, or None for no pin."""
        ref = _norm_ref(component.component_spec_ref1c)
        if not ref:
            return None
        self._load_specs()
        matches = self._specs_by_ref.get(ref, ())
        if len(matches) != 1:
            state = "missing" if not matches else "ambiguous"
            raise BomSpecificationResolutionError(
                f"component_id={int(component.component_id)} has {state} pinned "
                f"child specification ref={ref!r}"
            )
        return matches[0]

    def child_spec_id(self, component: SpecComponent) -> int | None:
        """Resolve the specification used to recursively expand this edge."""
        pinned = self.pinned_spec(component)
        if pinned is not None:
            return int(pinned.spec_id)
        return self.default_spec_id(int(component.item_id))

    def child_spec_ref1c(self, component: SpecComponent) -> str | None:
        """Return an explicit pinned ref for 1C payloads, validating it first."""
        pinned = self.pinned_spec(component)
        return _norm_ref(pinned.spec_ref1c) if pinned is not None else None

    def validate_components(self, components: Iterable[SpecComponent]) -> None:
        """Validate every explicit pin without resolving optional defaults."""
        for component in components:
            if _norm_ref(component.component_spec_ref1c):
                self.pinned_spec(component)

    def components_for_spec(self, spec_id: int | None) -> tuple[SpecComponent, ...]:
        if spec_id is None:
            return ()
        self._load_components()
        return tuple(self._components_by_spec.get(int(spec_id), ()))

    def descendant_ids_by_root(
        self,
        root_item_ids: Iterable[int],
    ) -> dict[int, set[int]]:
        """Walk descendants using the selected specification on every edge."""
        roots = sorted({int(item_id) for item_id in root_item_ids})
        result = {root: {root} for root in roots}

        def visit(
            root_id: int,
            item_id: int,
            spec_id: int | None,
            seen: frozenset[tuple[int, int]],
        ) -> None:
            if spec_id is None:
                return
            scope = (int(item_id), int(spec_id))
            if scope in seen:
                return
            next_seen = seen | {scope}
            for component in self.components_for_spec(spec_id):
                child_id = int(component.item_id)
                result[root_id].add(child_id)
                visit(
                    root_id,
                    child_id,
                    self.child_spec_id(component),
                    next_seen,
                )

        for root in roots:
            visit(root, root, self.default_spec_id(root), frozenset())
        return result
