from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
import os

# URL подключения к PostgreSQL
# В реальном приложении это должно быть в переменных окружения
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/prodplan"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


PRODUCTION_READ_SNAPSHOT_INFO_KEY = "production_read_snapshot"


@contextmanager
def production_read_snapshot(db):
    """Run one pure production read against a coherent non-blocking snapshot."""
    # Narrow router tests may override get_db with an opaque sentinel because
    # the service call itself is mocked.  Preserve that established override
    # contract; real application and database-backed tests always pass Session.
    if not hasattr(db, "info") or not hasattr(db, "get_bind"):
        yield db
        return
    marker = object()
    previous = db.info.get(PRODUCTION_READ_SNAPSHOT_INFO_KEY, marker)
    if db.get_bind().dialect.name == "postgresql":
        # This dependency is entered before the route issues any query.  SET
        # TRANSACTION is local to this transaction and cannot leak through the
        # pooled connection after the Session is closed/rolled back.
        db.execute(text(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        ))
    db.info[PRODUCTION_READ_SNAPSHOT_INFO_KEY] = True
    try:
        yield db
    finally:
        if previous is marker:
            db.info.pop(PRODUCTION_READ_SNAPSHOT_INFO_KEY, None)
        else:
            db.info[PRODUCTION_READ_SNAPSHOT_INFO_KEY] = previous


def get_db():
    """Создает и возвращает сессию базы данных."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
