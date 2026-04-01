from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime


class ItemBase(BaseModel):
    item_code: str
    item_name: str
    item_article: Optional[str] = None
    item_ref1c: Optional[str] = None
    replenishment_time: Optional[int] = None
    unit: Optional[str] = None
    category_id: Optional[int] = None
    stock_qty: float = 0.0
    # Опциональная оптимальная партия (лот‑сайзинг) для номенклатуры
    optimal_batch: Optional[float] = None
    status: str = 'active'


class ItemCreate(ItemBase):
    pass


class ItemUpdate(ItemBase):
    pass


class Item(ItemBase):
    item_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Paginated DTO for items list
class ItemsPage(BaseModel):
    rows: List[Item]
    total: int
    limit: int
    offset: int


class ItemCategoryBase(BaseModel):
    category_code: Optional[str] = None
    category_name: str
    category_ref1c: str
    parent_id: Optional[int] = None
    is_folder: bool = False
    predefined: bool = False
    predefined_name: Optional[str] = None
    data_version: Optional[str] = None
    deletion_mark: bool = False


class ItemCategoryCreate(ItemCategoryBase):
    pass


class ItemCategoryUpdate(ItemCategoryBase):
    pass


class ItemCategory(ItemCategoryBase):
    category_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SpecificationBase(BaseModel):
    spec_code: Optional[str] = None
    spec_name: str
    spec_ref1c: str


class SpecificationCreate(SpecificationBase):
    pass


class SpecificationUpdate(SpecificationBase):
    pass


class Specification(SpecificationBase):
    spec_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SpecComponentBase(BaseModel):
    spec_id: int
    item_id: int
    quantity: float
    stage_id: Optional[int] = None
    component_type: str = 'Материал'


class SpecComponentCreate(SpecComponentBase):
    pass


class SpecComponentUpdate(SpecComponentBase):
    pass


class SpecComponent(SpecComponentBase):
    component_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OperationBase(BaseModel):
    operation_ref1c: str
    operation_name: Optional[str] = None
    time_norm: float = 0.0


class OperationCreate(OperationBase):
    pass


class OperationUpdate(OperationBase):
    pass


class Operation(OperationBase):
    operation_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SpecOperationBase(BaseModel):
    spec_id: int
    operation_id: int
    stage_id: Optional[int] = None
    time_norm: float = 0.0


class SpecOperationCreate(SpecOperationBase):
    pass


class SpecOperationUpdate(SpecOperationBase):
    pass


class SpecOperation(SpecOperationBase):
    spec_operation_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductionOrderBase(BaseModel):
    order_number: str
    order_date: datetime
    order_ref1c: str
    is_posted: bool = False
    order_state_key: Optional[str] = None
    order_state_name: Optional[str] = None
    deletion_mark: bool = False


class ProductionOrderCreate(ProductionOrderBase):
    pass


class ProductionOrderUpdate(ProductionOrderBase):
    pass


class ProductionOrder(ProductionOrderBase):
    order_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductionProductBase(BaseModel):
    order_id: int
    item_id: int
    line_number: Optional[int] = None
    characteristic_ref1c: Optional[str] = None
    quantity: float
    spec_id: Optional[int] = None
    stage_id: Optional[int] = None


class ProductionProductCreate(ProductionProductBase):
    pass


class ProductionProductUpdate(ProductionProductBase):
    pass


class ProductionProduct(ProductionProductBase):
    product_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductionComponentBase(BaseModel):
    order_id: int
    item_id: int
    quantity: float
    spec_id: Optional[int] = None
    stage_id: Optional[int] = None


class ProductionComponentCreate(ProductionComponentBase):
    pass


class ProductionComponentUpdate(ProductionComponentBase):
    pass


class ProductionComponent(ProductionComponentBase):
    component_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductionOperationBase(BaseModel):
    order_id: int
    operation_id: int
    planned_quantity: float = 0.0
    time_norm: float = 0.0
    standard_hours: float = 0.0
    stage_id: Optional[int] = None


class ProductionOperationCreate(ProductionOperationBase):
    pass


class ProductionOperationUpdate(ProductionOperationBase):
    pass


class ProductionOperation(ProductionOperationBase):
    operation_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SupplierBase(BaseModel):
    supplier_ref1c: str
    supplier_name: Optional[str] = None


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(SupplierBase):
    pass


class Supplier(SupplierBase):
    supplier_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SupplierOrderBase(BaseModel):
    order_number: str
    order_date: datetime
    order_ref1c: str
    supplier_id: Optional[int] = None
    document_amount: float = 0.0
    is_posted: bool = False


class SupplierOrderCreate(SupplierOrderBase):
    pass


class SupplierOrderUpdate(SupplierOrderBase):
    pass


class SupplierOrder(SupplierOrderBase):
    order_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SupplierOrderItemBase(BaseModel):
    order_id: int
    item_id: int
    quantity: float
    price: float = 0.0
    amount: float = 0.0
    delivery_date: Optional[datetime] = None


class SupplierOrderItemCreate(SupplierOrderItemBase):
    pass


class SupplierOrderItemUpdate(SupplierOrderItemBase):
    pass


class SupplierOrderItem(SupplierOrderItemBase):
    item_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DefaultSpecificationBase(BaseModel):
    item_id: int
    characteristic_id: Optional[str] = None
    spec_id: int


class DefaultSpecificationCreate(DefaultSpecificationBase):
    pass


class DefaultSpecificationUpdate(DefaultSpecificationBase):
    pass


class DefaultSpecification(DefaultSpecificationBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductionResourceBase(BaseModel):
    resource_name: str
    shift_offset: Optional[int] = 0
    planning_range: Optional[int] = 30
    capacity: float = 0.0
    work_schedule: Optional[str] = '5/2'
    daily_work_hours: float = 8.0
    buffer_days: int = 0


class ProductionResourceCreate(ProductionResourceBase):
    pass


class ProductionResourceUpdate(ProductionResourceBase):
    pass


class ProductionResource(ProductionResourceBase):
    resource_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ResourceStageBase(BaseModel):
    resource_id: int
    stage_id: int


class ResourceStageCreate(ResourceStageBase):
    pass


class ResourceStage(ResourceStageBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# --- Pydantic схемы для видов производства ---

class ProductionKindBase(BaseModel):
    ref_1c: str
    name: str


class ProductionKindCreate(ProductionKindBase):
    pass


class ProductionKindUpdate(ProductionKindBase):
    pass


class ProductionKind(ProductionKindBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ResourceProductionKindBase(BaseModel):
    resource_id: int
    production_kind_id: int


class ResourceProductionKindCreate(ResourceProductionKindBase):
    pass


class ResourceProductionKind(ResourceProductionKindBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ODataSyncRequest(BaseModel):
    base_url: str
    entity_name: str
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    filter_query: Optional[str] = None
    select_fields: Optional[List[str]] = None
    dry_run: bool = False
    zero_missing: bool = False


class ODataSyncStats(BaseModel):
    items_total: int
    matched_in_odata: int
    unmatched_zeroed: int
    items_updated: int
    items_unchanged: int
    dry_run: bool
    odata_url: str
    odata_entity: str

class ResourceStageWithName(ResourceStageBase):
    id: int
    stage_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
# --- MRP Production response models (for grouped endpoint) ---

class ProductionStage(BaseModel):
    stage_id: int
    area_id: Optional[int] = None
    area_name: Optional[str] = None
    bucket_type: Optional[str] = "daily"
    bucket_date: Optional[str] = None
    hours: Optional[float] = 0.0
    missingNorm: Optional[bool] = None


class ProductionFlags(BaseModel):
    missingArea: Optional[bool] = None
    missingNorm: Optional[bool] = None
    componentBlocked: Optional[bool] = None
    componentPartial: Optional[bool] = None
    capacityShiftDays: Optional[int] = None


class ProductionGroupOrder(BaseModel):
    agg_key: str
    item_id: int
    item_name: Optional[str] = None
    item_article: Optional[str] = None
    unit: Optional[str] = None
    qty: float
    norm_hours_total: float
    norm_hours_per_unit: Optional[float] = None
    order_id: Optional[int] = None
    display_qty: Optional[float] = None
    display_norm_hours_total: Optional[float] = None
    overload: Optional[bool] = None


class ProductionGroup(BaseModel):
    area_id: Optional[int] = None
    area_name: str
    orders: List[ProductionGroupOrder] = []
    norm_sum_hours: float = 0.0
    min_days_to_need: Optional[int] = None
    cap_overload_hours: float = 0.0
    cap_overloaded_buckets: int = 0


class ProductionGroupedResponse(BaseModel):
    groups: List[ProductionGroup]
    total_groups: int
    total_orders: int
    limit: int
    offset: int


class ReworkGroupOrder(BaseModel):
    rework_id: int
    item_id: int
    item_name: Optional[str] = None
    item_article: Optional[str] = None
    unit: Optional[str] = None
    qty: float
    requested_qty: float
    planned_qty: float
    need_date: Optional[str] = None
    order_date: Optional[str] = None
    lead_time_days: int = 0
    priority_index: Optional[float] = None
    bucket_type: Optional[str] = "daily"
    bucket_date: Optional[str] = None
    spec_id: Optional[int] = None
    spec_code: Optional[str] = None
    spec_name: Optional[str] = None
    component_limit: Optional[float] = None
    component_blocked: bool = False
    component_partial: bool = False
    shortage: Optional[dict] = None


class ReworkGroup(BaseModel):
    group_id: Optional[int] = None
    group_name: str
    orders: List[ReworkGroupOrder] = []
    sum_qty: float = 0.0
    sum_requested_qty: float = 0.0
    sum_planned_qty: float = 0.0
    blocked_orders: int = 0
    partial_orders: int = 0


class ReworkGroupedResponse(BaseModel):
    groups: List[ReworkGroup]
    total_groups: int
    total_orders: int
    limit: int
    offset: int


class PurchaseCategoryGroupOrder(BaseModel):
    purchase_id: int
    item_id: int
    item_name: Optional[str] = None
    item_article: Optional[str] = None
    unit: Optional[str] = None
    qty: float
    need_date: Optional[str] = None
    order_date: Optional[str] = None
    lead_time_days: int = 0
    priority_index: Optional[float] = None
    bucket_type: Optional[str] = "daily"
    bucket_date: Optional[str] = None
    supplier_ref1c: Optional[str] = None


class PurchaseCategoryGroup(BaseModel):
    group_id: Optional[int] = None
    group_name: str
    orders: List[PurchaseCategoryGroupOrder] = []
    sum_qty: float = 0.0


class PurchaseCategoryGroupedResponse(BaseModel):
    groups: List[PurchaseCategoryGroup]
    total_groups: int
    total_orders: int
    limit: int
    offset: int


class PlannedReworkBase(BaseModel):
    run_id: int
    item_id: int
    spec_id: Optional[int] = None
    requested_qty: float
    planned_qty: float
    qty: float
    need_date: date
    order_date: date
    lead_time_days: int
    priority_index: Optional[float] = None
    bucket_date: date
    component_limit: Optional[float] = None
    component_blocked: bool = False
    component_partial: bool = False
    shortage: Optional[dict] = None


class PlannedReworkCreate(PlannedReworkBase):
    pass


class PlannedReworkUpdate(PlannedReworkBase):
    pass


class PlannedRework(PlannedReworkBase):
    rework_id: int

    class Config:
        from_attributes = True
