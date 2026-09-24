"""Structural validation of the evaluation datasets Phase 7 extended --
pure, offline, no database, no network. Catches dataset corruption (an
earlier real incident in this project: a formatter once reflowed a JSONL
fixture into multi-line pretty-printed JSON, silently breaking every
downstream `json.loads(line)` call) and enforces the specific coverage
Phase 7's `retrieval_necessity` category is required to have.
"""

from __future__ import annotations

import json
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parents[2] / "eval" / "datasets"


def _load(name: str) -> list[dict]:
    path = DATASETS_DIR / name
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


class TestAgentDatasetIsValidJsonl:
    def test_every_line_is_exactly_one_json_object(self) -> None:
        path = DATASETS_DIR / "agent_v1.jsonl"
        with path.open() as f:
            lines = [line for line in f if line.strip()]
        for line in lines:
            # A line that was reflowed into multi-line pretty-printed JSON
            # would fail this -- one line must be one complete object.
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_every_task_has_a_unique_task_id(self) -> None:
        tasks = _load("agent_v1.jsonl")
        task_ids = [t["task_id"] for t in tasks]
        assert len(task_ids) == len(set(task_ids))


class TestRetrievalNecessityCategory:
    """Phase 7 Step 5: the generalized category must cover all six
    required cases, not just a relabeled `ambiguous_entity`.
    """

    def _tasks(self) -> list[dict]:
        tasks = _load("agent_v1.jsonl")
        return [t for t in tasks if t["category"] == "retrieval_necessity"]

    def test_at_least_five_tasks(self) -> None:
        assert len(self._tasks()) >= 5

    def test_includes_a_memory_required_case(self) -> None:
        tasks = self._tasks()
        assert any(
            t["expected_seed_indices"] and not t.get("expect_zero_tool_calls") for t in tasks
        )

    def test_includes_a_memory_not_required_case(self) -> None:
        tasks = self._tasks()
        assert any(t.get("expect_zero_tool_calls") for t in tasks)

    def test_includes_an_ambiguous_entity_case(self) -> None:
        assert any("ambiguous" in t["task_id"] for t in self._tasks())

    def test_includes_an_absent_information_case(self) -> None:
        tasks = self._tasks()
        assert any(t["seed"] and not t["expected_seed_indices"] for t in tasks)

    def test_includes_a_user_specific_fact_case(self) -> None:
        assert any("user_specific" in t["task_id"] for t in self._tasks())

    def test_includes_a_general_knowledge_case(self) -> None:
        tasks = self._tasks()
        general = [t for t in tasks if t.get("expect_zero_tool_calls") and not t["seed"]]
        assert len(general) >= 2  # both the "sounds technical" and the "obviously general" case

    def test_every_task_has_an_answer_contains_signal(self) -> None:
        """Every task must define a checkable expected outcome -- a
        retrieval-necessity task with no `answer_contains` would be
        unverifiable by the real-LLM harness's `answer_contains_expected`
        check.
        """
        for t in self._tasks():
            assert "answer_contains" in t, t["task_id"]

    def test_entity_names_do_not_collide_with_other_categories(self) -> None:
        """The `ambiguous_entity` finding was specifically about a
        real-world-sounding name -- new tasks must use their own entity
        names, not accidentally reuse `ambiguous_entity`'s exact fixture
        (which would just be the same anecdote twice, not a generalization).
        """
        all_tasks = _load("agent_v1.jsonl")
        original = next(t for t in all_tasks if t["task_id"] == "ambiguous_entity")
        original_names = {e["name"] for seed in original["seed"] for e in seed["entities"]}
        for t in self._tasks():
            names = {e["name"] for seed in t["seed"] for e in seed["entities"]}
            assert not (names & original_names), t["task_id"]


class TestDocumentDatasetCoversAllElevenCategories:
    _EXPECTED_CATEGORIES = {
        "extracting_facts_from_documents",
        "provenance_questions",
        "duplicate_information_across_documents",
        "document_prompt_injection",
        "large_document_behavior",
        "multi_document_reasoning",
        "document_conversation_combined_reasoning",
        "temporal_document_facts",
        "conflicting_documents",
        "memory_absence",
    }

    def test_covers_every_planned_category(self) -> None:
        tasks = _load("document_v1.jsonl")
        categories = {t["category"] for t in tasks}
        missing = self._EXPECTED_CATEGORIES - categories
        assert missing == set(), f"missing categories: {missing}"

    def test_every_task_has_a_unique_task_id(self) -> None:
        tasks = _load("document_v1.jsonl")
        task_ids = [t["task_id"] for t in tasks]
        assert len(task_ids) == len(set(task_ids))
