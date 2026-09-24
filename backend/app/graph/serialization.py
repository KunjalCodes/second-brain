"""Conversion between neo4j driver values and plain Python values.

Timestamps are stored as native Neo4j `datetime` so that Cypher can compare validity
windows server-side. The driver hands them back as `neo4j.time.DateTime`, which is not
JSON-serialisable, so every value crossing out of the graph layer passes through
`to_python`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from neo4j.graph import Node, Path, Relationship
from neo4j.time import Date, DateTime, Duration, Time


def to_python(value: Any) -> Any:
    """Recursively convert driver values into JSON-friendly Python values.

    Neo4j `DateTime` values are always returned as timezone-aware UTC datetimes;
    a stored value without a zone would make cross-timezone comparisons silently
    wrong, so naive results are treated as an error at write time, not here.
    """
    if isinstance(value, DateTime):
        return value.to_native()
    if isinstance(value, Date):
        return value.to_native()
    if isinstance(value, Time):
        return value.to_native()
    if isinstance(value, Duration):
        # Months/years are not representable as a fixed timedelta; surface them
        # rather than silently losing them.
        if value.months:
            return str(value)
        return timedelta(
            days=value.days, seconds=value.seconds, microseconds=value.nanoseconds // 1000
        )
    if isinstance(value, datetime | date | time):
        return value
    if isinstance(value, Node):
        return {"_labels": sorted(value.labels), **{k: to_python(v) for k, v in value.items()}}
    if isinstance(value, Relationship):
        return {"_type": value.type, **{k: to_python(v) for k, v in value.items()}}
    if isinstance(value, Path):
        return {
            "nodes": [to_python(n) for n in value.nodes],
            "relationships": [to_python(r) for r in value.relationships],
        }
    if isinstance(value, dict):
        return {k: to_python(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_python(v) for v in value]
    return value


def records_to_dicts(records: list[Any]) -> list[dict[str, Any]]:
    """Convert driver records into plain dicts with converted values."""
    return [{k: to_python(v) for k, v in record.items()} for record in records]
