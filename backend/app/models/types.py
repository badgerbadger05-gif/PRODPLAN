from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import TypeDecorator


# Use JSON for SQLite, JSONB for others
class CrossPlatformJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(JSONB())
        else:
            return dialect.type_descriptor(JSON())


@compiles(CrossPlatformJSON, 'postgresql')
def compile_jsonb(element, compiler, **kw):
    return "JSONB"
