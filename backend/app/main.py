from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import items
from app.routers import sync as sync_router
from app.routers import odata as odata_router
from app.routers import plan as plan_router
from app.routers import nomenclature as nomenclature_router
from app.routers import specification as specification_router
from app.routers import specification_repair as specification_repair_router
from app.routers import resources as resources_router
from app.routers import production_control as production_control_router
from app.routers import purchase_control as purchase_control_router
from app.routers import workshop_binding_review as workshop_binding_review_router
from app.routers import paint_weld as paint_weld_router
from app.routers import item_ledger as item_ledger_router
from app.routers import item_ledger_admin as item_ledger_admin_router
from app.routers import planning_rates as planning_rates_router
from app.routers import release_feasibility as release_feasibility_router

import os
import logging

app = FastAPI(title="PRODPLAN API", version="1.0.0")

# Logging configuration for spec tree debug
logging.basicConfig(level=logging.INFO)
logging.getLogger("specification").setLevel(logging.INFO)

# CORS: действующий серверный frontend — 9020; 9000 и 9300 используются
# локальной разработкой. Выведенный из эксплуатации порт 9010 не разрешён.
# FRONTEND_ORIGIN переопределяет список целиком; допускается перечисление
# через запятую.
_DEFAULT_FRONTEND_PORTS = (9000, 9020, 9300)
_DEFAULT_FRONTEND_ORIGINS = [
    f"http://{host}:{port}"
    for port in _DEFAULT_FRONTEND_PORTS
    for host in ("localhost", "127.0.0.1")
]


def _resolve_frontend_origins() -> list[str]:
    raw = os.getenv("FRONTEND_ORIGIN")
    if not raw or not raw.strip():
        return list(_DEFAULT_FRONTEND_ORIGINS)
    configured = [origin.strip() for origin in raw.split(",") if origin.strip()]
    # Дедупликация с сохранением порядка.
    return list(dict.fromkeys(configured))


frontend_origins = _resolve_frontend_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Routers
app.include_router(items.router, prefix="/api")
app.include_router(sync_router.router, prefix="/api")
app.include_router(odata_router.router, prefix="/api")
app.include_router(plan_router.router, prefix="/api")
app.include_router(nomenclature_router.router, prefix="/api")
app.include_router(specification_router.router, prefix="/api")
app.include_router(specification_repair_router.router, prefix="/api")
app.include_router(resources_router.router, prefix="/api")
app.include_router(production_control_router.router, prefix="/api")
app.include_router(purchase_control_router.router, prefix="/api")
app.include_router(workshop_binding_review_router.router, prefix="/api")
app.include_router(paint_weld_router.router, prefix="/api")
app.include_router(item_ledger_router.router, prefix="/api")
app.include_router(item_ledger_admin_router.router, prefix="/api")
app.include_router(planning_rates_router.router, prefix="/api")
app.include_router(release_feasibility_router.router, prefix="/api")

@app.get("/")
async def root():
    return {"message": "Welcome to PRODPLAN API"}

@app.get("/health")
async def health():
    return {"status": "ok"}
