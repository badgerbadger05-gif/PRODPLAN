from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime


class ItemBase(BaseModel):
    item_code: str
    item_name: str
    item_article: Optional[str] = None
    item_ref1c: Optional[str] = None
    replenishment_method: Optional[str] = None
    replenishment_time: Optional[int] = None
    unit: Optional[str] = None
    stock_qty: float = 0.0
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