import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
import os

# Add the backend directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from app.database import Base
# Import models so SQLAlchemy registers tables on Base.metadata for create_all()
# (tests use in-memory SQLite and create schema from ORM metadata).
from app import models  # noqa: F401

# Use an in-memory SQLite database for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="function")
def db_session():
    """
    Fixture to create a new database session for each test function.
    """
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def building_ledger_generation(db_session):
    """Explicit mutable Ledger context for generation-aware writer tests."""
    imported = models.PhysicalImportBatch(
        batch_key="test-physical-import",
        status="completed",
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="test-building-generation",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch=imported,
        algorithm_version="tests/1",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(models.PlanningTruthState(
        id=1,
        current_generation_id=generation.id,
    ))
    db_session.commit()
    return generation
