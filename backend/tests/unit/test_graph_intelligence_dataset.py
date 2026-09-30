"""Structure of eval/datasets/graph_intelligence_v1.json (Phase 15, M16)."""

from __future__ import annotations

import json
from pathlib import Path

DATASET = json.loads(
    (Path(__file__).parents[2] / "eval" / "datasets" / "graph_intelligence_v1.json").read_text()
)
TASKS = DATASET["tasks"]
OPS = {
    "path", "path_budget", "neighborhood", "entity_context", "explain", "evolution",
    "context", "search_alias", "isolation",
}  # fmt: skip
#: The category letters M16 requires (A-Q), plus X for the cross-user probe.
REQUIRED_LETTERS = set("ABCDEFGHIJKLMNOPQ") | {"X"}


def test_task_ids_are_unique() -> None:
    ids = [t["id"] for t in TASKS]
    assert len(ids) == len(set(ids))


def test_every_required_category_is_covered() -> None:
    letters = {t["category"].split("_", 1)[0] for t in TASKS}
    assert letters >= REQUIRED_LETTERS, sorted(REQUIRED_LETTERS - letters)


def test_every_op_is_known_and_every_task_states_its_expectation() -> None:
    for t in TASKS:
        assert t["op"] in OPS, t["id"]
        if t["op"] != "isolation":
            assert t.get("expect"), t["id"]


def test_world_references_resolve() -> None:
    keys: set[str] = set()
    for m in DATASET["world"]:
        for ref in ("supersedes", "conflicts_with"):
            if ref in m:
                assert m[ref] in keys, f"{m['key']} refers to a later or missing {m[ref]}"
        assert m["key"] not in keys
        keys.add(m["key"])


def test_tasks_only_name_entities_and_memories_that_exist() -> None:
    names = {e[0] for m in DATASET["world"] for e in m["entities"]}
    names |= {DATASET["hub"]["name"], DATASET["merge"]["merged"]}
    for t in TASKS:
        for field in ("from", "to", "entity", "source", "target"):
            if field in t:
                assert t[field] in names, (t["id"], field, t[field])
        for field in ("memory",):
            if field in t:
                assert t[field] in {m["key"] for m in DATASET["world"]}, t["id"]
        for k in t.get("expect", {}).get("supporting", []) + t.get("expect", {}).get(
            "memories", []
        ):
            assert k in {m["key"] for m in DATASET["world"]}, (t["id"], k)


def test_the_dataset_holds_both_polarities_for_the_hard_cases() -> None:
    """A rejection task without a matching inclusion task can pass by returning
    nothing; every context task here also requires items to be included."""
    for t in TASKS:
        if t["op"] == "context":
            assert t["expect"].get("include"), t["id"]
