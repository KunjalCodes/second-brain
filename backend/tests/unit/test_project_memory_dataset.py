# ruff: noqa: E501 - compact assertions
"""The project-memory evaluation dataset is internally consistent (Phase 16, M28)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.projects.events import EventIn

DS = json.loads((Path(__file__).resolve().parents[2] / "eval" / "datasets" / "project_memory_v1.json").read_text())


def test_every_scripted_event_is_valid() -> None:
    for e in DS["events"]:
        payload = json.loads(json.dumps(e["p"]).replace("@HEAD", "a" * 40))
        EventIn.model_validate({"session_id": e["s"], "event_type": e["type"], "timestamp": "2026-01-01T00:00:00Z", "payload": payload})


def test_event_ids_are_unique_so_replays_are_identical() -> None:
    ids = [(e["s"], e["m"]) for e in DS["events"]]
    assert len(ids) == len(set(ids))


def test_expectations_refer_to_scripted_titles() -> None:
    titles = {e["p"].get("title") for e in DS["events"] if e["type"] in ("decision", "bug_discovered", "fix")}
    for name in DS["expect"]["decisions"]:
        assert name in titles
    for name in DS["expect"]["bugs"]:
        assert name in titles
    approaches = {e["p"].get("approach") for e in DS["events"]} | titles
    for name in DS["expect"]["attempts"]:
        assert name in approaches
    for name in DS["expect"]["resume"]["distractors"]:
        assert name in titles | approaches


def test_the_distractor_session_is_unrelated_and_older() -> None:
    s0 = [e for e in DS["events"] if e["s"] == "s0"]
    rest = [e for e in DS["events"] if e["s"] != "s0"]
    assert s0 and max(e["m"] for e in s0) < min(e["m"] for e in rest)
    assert all(e["p"].get("files", ["web/invoice.css"])[0].startswith("web/") for e in s0 if e["type"] in ("decision", "bug_discovered"))


def test_continuation_chain_is_declared() -> None:
    start = next(e for e in DS["events"] if e["s"] == "s2" and e["type"] == "session_started")
    assert start["p"]["continues_session"] == "s1"


@pytest.mark.parametrize("key", ["remote", "project_name", "commits", "search", "expect"])
def test_required_sections_exist(key: str) -> None:
    assert DS[key]
