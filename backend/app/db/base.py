"""SQLAlchemy declarative base.

PostgreSQL holds application and conversation state only. The canonical memory
graph lives in Neo4j and is never duplicated here -- see
docs/adr/0006-postgresql-for-application-state.md.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
