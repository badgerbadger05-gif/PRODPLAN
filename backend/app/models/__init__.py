"""ORM models package.

Historically all models lived in a single ``app/models.py`` module. The file
was split into themed submodules, but this package MUST preserve the exact
public surface of that module: every name that used to be importable via
``from app.models import X`` remains importable here unchanged.

That includes the SQLAlchemy symbols the original module imported at top level
(``Column``, ``Integer``, ``relationship``, ``func``, ``text`` …), the shared
``Base`` from ``app.database``, the ``CrossPlatformJSON`` type + its
``compile_jsonb`` hook, and all ORM model classes. Importing every submodule
below also registers every table on ``Base.metadata`` (so ``create_all`` and
Alembic autogenerate still see all tables).
"""

# --- Re-export the SQLAlchemy symbols the original models.py exposed ---
from sqlalchemy import (
    Column,
    Integer,
    String,
    DECIMAL,
    TIMESTAMP,
    ForeignKey,
    TEXT,
    Boolean,
    DateTime,
    Date,
    CheckConstraint,
    JSON,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import TypeDecorator

# --- Shared declarative Base (single mapper registry) ---
from ..database import Base

# --- Shared cross-platform JSON type + its compile hook ---
from .types import CrossPlatformJSON, compile_jsonb

# --- ORM model classes (importing each submodule registers its tables) ---
from .catalog import (
    ProductionStage,
    Item,
    ItemCategory,
    StockWarehouse,
    ItemWarehouseStock,
    Unit,
    Employee,
    ProductionKind,
    ItemEmbedding,
)
from .specification import (
    Specification,
    SpecComponent,
    Operation,
    SpecOperation,
    DefaultSpecification,
    RootProduct,
)
from .production import (
    ProductionOrder,
    ProductionProduct,
    ProductionOrderLineState,
    WorkshopWarehouseBinding,
    IgnoredWarehouse,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionManufacture,
    ProductionManufactureOperation,
    ProductionComponent,
    ProductionOperation,
    ProductionDayClose,
    ProductionDayCloseItem,
)
from .suppliers import (
    SupplierOrder,
    Supplier,
    SupplierOrderItem,
)
from .resources import (
    ProductionResource,
    ResourceStage,
    ResourceProductionKind,
    WorkCalendarDay,
)
from .planning import (
    ProductionPlanEntry,
    PlanningConfigVersion,
    ProductionPlanHeader,
    ProductionPlanLine,
    PlanningRun,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    MrpRequirement,
    MrpRequirementBucket,
    CapacityLoad,
    PeggingLink,
    ForcedOrderRequest,
    ForcedOrderResult,
)
from .sync import SyncLink
from .dbr import (
    DbrSettings,
    DbrAssemblyRate,
    DbrCategorySupplyRisk,
    DbrSupermarketPosition,
    DbrFeederSignal,
    DbrProductionProgram,
    DbrProductionProgramItem,
    DbrDrumSchedule,
    DbrDrumScheduleProgram,
    DbrDrumSlot,
    DbrDrumCapacityGap,
)
from .paint_weld import (
    PaintWeldPair,
    PaintWeldChainLink,
)

__all__ = [
    # SQLAlchemy re-exports (parity with the original module namespace)
    "Base",
    "Boolean",
    "CheckConstraint",
    "Column",
    "DECIMAL",
    "Date",
    "DateTime",
    "ForeignKey",
    "Index",
    "Integer",
    "JSON",
    "JSONB",
    "String",
    "TEXT",
    "TIMESTAMP",
    "TypeDecorator",
    "UniqueConstraint",
    "compiles",
    "func",
    "relationship",
    "text",
    # Shared JSON type + hook
    "CrossPlatformJSON",
    "compile_jsonb",
    # ORM model classes
    "CapacityLoad",
    "DbrAssemblyRate",
    "DbrCategorySupplyRisk",
    "DbrDrumCapacityGap",
    "DbrDrumSchedule",
    "DbrDrumScheduleProgram",
    "DbrDrumSlot",
    "DbrFeederSignal",
    "DbrProductionProgram",
    "DbrProductionProgramItem",
    "DbrSettings",
    "DbrSupermarketPosition",
    "DefaultSpecification",
    "Employee",
    "ForcedOrderRequest",
    "ForcedOrderResult",
    "IgnoredWarehouse",
    "Item",
    "ItemCategory",
    "ItemEmbedding",
    "ItemWarehouseStock",
    "MrpRequirement",
    "MrpRequirementBucket",
    "Operation",
    "PaintWeldChainLink",
    "PaintWeldPair",
    "PeggingLink",
    "PlannedOrder",
    "PlannedOrderStage",
    "PlannedPurchase",
    "PlannedRework",
    "PlanningConfigVersion",
    "PlanningRun",
    "ProductionComponent",
    "ProductionDayClose",
    "ProductionDayCloseItem",
    "ProductionKind",
    "ProductionManufacture",
    "ProductionManufactureOperation",
    "ProductionMaterialIssue",
    "ProductionMaterialIssueLine",
    "ProductionOperation",
    "ProductionOrder",
    "ProductionOrderLineState",
    "ProductionPlanEntry",
    "ProductionPlanHeader",
    "ProductionPlanLine",
    "ProductionProduct",
    "ProductionResource",
    "ProductionStage",
    "ResourceProductionKind",
    "ResourceStage",
    "RootProduct",
    "SpecComponent",
    "SpecOperation",
    "Specification",
    "StockWarehouse",
    "Supplier",
    "SupplierOrder",
    "SupplierOrderItem",
    "SyncLink",
    "Unit",
    "WorkCalendarDay",
    "WorkshopWarehouseBinding",
]
