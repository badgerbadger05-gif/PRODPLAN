"""Guards for the disposable, local-only R2 PostgreSQL contour."""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url


R2_DATABASE = "prodplan_r2"
R2_USER = "r2_user"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PRODUCTION_DATABASE_NAMES = {"prodplan", "production", "postgres"}


def validate_r2_dsn(dsn: str) -> URL:
    """Return a parsed R2 DSN or reject an external/default database.

    The guard is deliberately strict: a missing or non-local host is never
    interpreted as a local connection, and the default application database
    is not an acceptable test target.
    """

    try:
        url = make_url(dsn)
    except Exception as exc:  # pragma: no cover - SQLAlchemy supplies details
        raise ValueError("invalid R2 DSN") from exc
    if url.drivername.split("+", 1)[0] != "postgresql":
        raise ValueError("R2 database must use a local PostgreSQL DSN")
    if (url.host or "").lower() not in LOCAL_HOSTS:
        raise ValueError("R2 database host must be local")
    if not url.database:
        raise ValueError("R2 DSN requires explicit database identity")
    if url.database in PRODUCTION_DATABASE_NAMES or url.database != R2_DATABASE:
        raise ValueError("R2 DSN requires explicit prodplan_r2 identity")
    if url.username != R2_USER:
        raise ValueError("R2 DSN requires explicit r2_user identity")
    if url.password in {None, "", "password", "prodplan"}:
        raise ValueError("R2 DSN requires a local test credential")
    return url
