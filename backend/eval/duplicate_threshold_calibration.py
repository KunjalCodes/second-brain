"""Semantic-duplicate threshold calibration (PHASE4_PLAN.md sections 10/16).

Methodology (binding, not incidental):

1. A labeled dataset of (text_a, text_b, label) pairs is split in advance into
   `dev` and `holdout` (`eval/datasets/duplicate_calibration_v1.jsonl`,
   `split` field). Similarity for every pair is computed once, from the real
   `FakeEmbeddingClient` + `app.semantic.similarity.cosine_similarity` --
   never assumed or hand-picked to fit a target threshold.
2. A threshold sweep runs on `dev` ONLY, computing precision/recall/F1/FPR at
   each candidate threshold.
3. The operating point is chosen by the approved policy: **the lowest false-
   positive rate subject to recall >= MIN_ACCEPTABLE_RECALL** -- never "max
   F1", which the user's approval explicitly rejected as the selection
   criterion.
4. The chosen threshold is evaluated EXACTLY ONCE on `holdout`. Its result is
   reported as-is -- this script contains no code path that adjusts the
   threshold based on the holdout result, by design.

No fabricated numbers: every figure below comes from an actual run against
the real (deterministic) embedding client.

Usage:
    uv run python eval/duplicate_threshold_calibration.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embeddings.fake import FakeEmbeddingClient  # noqa: E402
from app.semantic.similarity import cosine_similarity  # noqa: E402

DATASET_PATH = Path(__file__).parent / "datasets" / "duplicate_calibration_v1.jsonl"

#: The approved policy's minimum acceptable recall -- a stated choice, not
#: derived from the data. Lower would accept missing more true duplicates;
#: this project's conservative stance (ADR 0009: never merge without strong
#: evidence, but also don't miss so many that review is pointless) picks 0.9.
MIN_ACCEPTABLE_RECALL = 0.9

#: Sweep granularity.
THRESHOLDS = [round(0.05 * i, 2) for i in range(1, 20)]  # 0.05 .. 0.95


@dataclass(frozen=True)
class LabeledPair:
    id: str
    split: str
    label: int
    similarity: float


@dataclass(frozen=True)
class SweepRow:
    threshold: float
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def fpr(self) -> float:
        return self.fp / (self.fp + self.tn) if (self.fp + self.tn) else 0.0


async def load_pairs() -> list[LabeledPair]:
    client = FakeEmbeddingClient()
    pairs: list[LabeledPair] = []
    for line in DATASET_PATH.read_text().splitlines():
        if not line.strip():
            continue
        spec = json.loads(line)
        result_a = await client.embed(text=spec["text_a"])
        result_b = await client.embed(text=spec["text_b"])
        similarity = cosine_similarity(result_a.vector, result_b.vector)
        pairs.append(
            LabeledPair(
                id=spec["id"], split=spec["split"], label=spec["label"], similarity=similarity
            )
        )
    return pairs


def sweep(pairs: list[LabeledPair], thresholds: list[float]) -> list[SweepRow]:
    rows = []
    for t in thresholds:
        tp = fp = tn = fn = 0
        for p in pairs:
            predicted_duplicate = p.similarity >= t
            actual_duplicate = p.label == 1
            if predicted_duplicate and actual_duplicate:
                tp += 1
            elif predicted_duplicate and not actual_duplicate:
                fp += 1
            elif not predicted_duplicate and actual_duplicate:
                fn += 1
            else:
                tn += 1
        rows.append(SweepRow(threshold=t, tp=tp, fp=fp, tn=tn, fn=fn))
    return rows


def select_operating_point(rows: list[SweepRow], min_recall: float) -> SweepRow | None:
    """Lowest FPR subject to recall >= min_recall; ties broken toward the
    HIGHER threshold (more conservative -- fewer things called duplicates).
    """
    candidates = [r for r in rows if r.recall >= min_recall]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (r.fpr, -r.threshold))


def evaluate_at(pairs: list[LabeledPair], threshold: float) -> SweepRow:
    return sweep(pairs, [threshold])[0]


async def main() -> int:
    all_pairs = await load_pairs()
    dev = [p for p in all_pairs if p.split == "dev"]
    holdout = [p for p in all_pairs if p.split == "holdout"]

    print(f"Loaded {len(dev)} dev pairs, {len(holdout)} holdout pairs from {DATASET_PATH.name}\n")
    print("Dev pairs (id, label, similarity):")
    for p in sorted(dev, key=lambda p: p.similarity):
        print(f"  {p.id:14} label={p.label}  similarity={p.similarity:.3f}")
    print()

    rows = sweep(dev, THRESHOLDS)
    print("Threshold sweep on DEV (never touches holdout):")
    print(f"  {'threshold':>9} {'precision':>9} {'recall':>7} {'f1':>6} {'fpr':>6} {'tp/fp/tn/fn'}")
    for r in rows:
        print(
            f"  {r.threshold:>9.2f} {r.precision:>9.2%} {r.recall:>7.2%} {r.f1:>6.2f} "
            f"{r.fpr:>6.2%} {r.tp}/{r.fp}/{r.tn}/{r.fn}"
        )
    print()

    chosen = select_operating_point(rows, MIN_ACCEPTABLE_RECALL)
    if chosen is None:
        print(f"No threshold on DEV achieves recall >= {MIN_ACCEPTABLE_RECALL:.0%}.")
        return 1

    print(
        f"Selected operating point (policy: lowest FPR subject to recall >= "
        f"{MIN_ACCEPTABLE_RECALL:.0%}): threshold={chosen.threshold:.2f} "
        f"dev_precision={chosen.precision:.2%} dev_recall={chosen.recall:.2%} "
        f"dev_fpr={chosen.fpr:.2%}\n"
    )

    print("Holdout pairs (id, label, similarity):")
    for p in sorted(holdout, key=lambda p: p.similarity):
        print(f"  {p.id:14} label={p.label}  similarity={p.similarity:.3f}")
    print()

    holdout_result = evaluate_at(holdout, chosen.threshold)
    print(
        f"HOLDOUT evaluation at threshold={chosen.threshold:.2f} (evaluated ONCE, "
        f"never used to adjust the threshold):"
    )
    print(f"  precision={holdout_result.precision:.2%}")
    print(f"  recall={holdout_result.recall:.2%}")
    print(f"  f1={holdout_result.f1:.2f}")
    print(f"  fpr={holdout_result.fpr:.2%}")
    print(
        f"  tp={holdout_result.tp} fp={holdout_result.fp} "
        f"tn={holdout_result.tn} fn={holdout_result.fn}"
    )
    print(
        "\nThis is a small, hand-built calibration dataset (12 dev / 8 holdout "
        "pairs) demonstrating the methodology with real measured numbers -- it "
        "is not a claim that this threshold is production-ready; a real "
        "deployment needs a much larger labeled dataset before this changes "
        "settings.semantic_duplicate_threshold."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
