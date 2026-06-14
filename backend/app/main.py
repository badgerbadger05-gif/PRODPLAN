from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import items
from app.database import engine
from app.models import Base
from app.routers import sync as sync_router
from app.routers import odata as odata_router
from app.routers import plan as plan_router
from app.routers import nomenclature as nomenclature_router
from app.routers import stages as stages_router
from app.routers import specification as specification_router
from app.routers import resources as resources_router
from app.routers import production_control as production_control_router
from app.routers import purchase_control as purchase_control_router
from app.routers import workshop_binding_review as workshop_binding_review_router

import os
import logging

app = FastAPI(title="PRODPLAN API", version="1.0.0")

# Create tables
Base.metadata.create_all(bind=engine)

# Logging configuration for spec tree debug
logging.basicConfig(level=logging.INFO)
logging.getLogger("specification").setLevel(logging.INFO)

# CORS: разрешаем фронтенд (nginx на 9000)
frontend_origin = os.getenv("FRONTEND_ORIGIN", "http://localhost:9000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_origin, "http://127.0.0.1:9000"],
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
app.include_router(stages_router.router, prefix="/api")
app.include_router(specification_router.router, prefix="/api")
app.include_router(resources_router.router, prefix="/api")
app.include_router(production_control_router.router, prefix="/api")
app.include_router(purchase_control_router.router, prefix="/api")
app.include_router(workshop_binding_review_router.router, prefix="/api")

@app.get("/")
async def root():
    return {"message": "Welcome to PRODPLAN API"}

@app.get("/health")
async def health():
    return {"status": "ok"}
