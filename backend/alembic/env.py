import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, create_engine
from sqlalchemy.engine import Connection

# Import project metadata
# Ensure backend is on sys.path when running alembic from backend/
try:
    from app.database import Base  # SQLAlchemy Declarative Base
    from app import models  # noqa: F401 - ensure models are imported so metadata is populated
except Exception as e:
    # Fallback: try relative import when executed differently
    try:
        from backend.app.database import Base  # type: ignore
        from backend.app import models  # type: ignore  # noqa: F401
    except Exception as e2:
        raise

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None and os.path.exists(config.config_file_name):
    fileConfig(config.config_file_name)

# Override DB URL from env if provided
db_url_env = os.getenv("DATABASE_URL")
if db_url_env:
    config.set_main_option("sqlalchemy.url", db_url_env)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.
    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable here as well.
    By skipping the Engine creation we don't even need a DBAPI to be available.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not configured in alembic.ini and DATABASE_URL env is not set")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.
    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not configured in alembic.ini and DATABASE_URL env is not set")

    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:  # type: Connection
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()