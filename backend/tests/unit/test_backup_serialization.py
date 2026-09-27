"""Phase 11 (M9): graph export values round-trip with their Neo4j types.

A backup that turned `valid_from`/`valid_until` into strings would restore a
graph whose temporal queries silently stop matching -- the restore drill
checks this end to end; this pins the codec.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from neo4j.time import Date, DateTime, Duration, Time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from backup import _decode, _encode  # noqa: E402


def test_temporal_and_nested_values_round_trip_through_json() -> None:
    props = {
        "valid_from": DateTime(2026, 1, 15, 9, 0, 0, tzinfo=None).replace(tzinfo=None),
        "created_at": DateTime.from_iso_format("2026-05-01T09:00:00.123456789+00:00"),
        "day": Date(2026, 3, 1),
        "at": Time(12, 30, 0),
        "span": Duration(months=1, days=2, seconds=3, nanoseconds=4),
        "aliases": ["OpenAI", "Open AI"],
        "confidence": 0.9,
        "valid_until": None,
    }
    restored = {
        k: _decode(v)
        for k, v in json.loads(json.dumps({k: _encode(v) for k, v in props.items()})).items()
    }
    assert restored == props
    assert isinstance(restored["created_at"], DateTime)
    assert restored["created_at"].nanosecond == 123456789
