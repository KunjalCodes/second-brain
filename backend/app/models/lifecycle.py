"""Memory lifecycle and temporal-validity rules.

These are pure functions with no database or framework dependency so that the rules
that matter most for correctness can be unit-tested exhaustively and cheaply.

Temporal model (bi-temporal, see docs/adr/0002-temporal-model.md)
----------------------------------------------------------------
Two independent time axes are recorded:

* **valid time** -- `valid_from` / `valid_until`: when the fact is true *in the world*.
* **transaction time** -- `created_at` / `updated_at`: when the system *recorded* it.

`valid_from` is inclusive and `valid_until` is exclusive, i.e. the half-open interval
``[valid_from, valid_until)``. A `valid_until` of ``None`` means "still true, no known
end". The half-open convention means a fact that ends at the instant another begins
produces no overlap and no gap, which is what makes supersession unambiguous.
"""

from __future__ import annotations

from datetime import datetime

from app.models.enums import MemoryStatus

#: Legal status transitions. Absence of a target status means the transition is
#: rejected with a 409 rather than silently applied.
ALLOWED_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset(
        {
            MemoryStatus.HISTORICAL,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.INVALID,
            MemoryStatus.CONFLICTING,
            MemoryStatus.ARCHIVED,
            MemoryStatus.MERGED,
        }
    ),
    MemoryStatus.CONFLICTING: frozenset(
        {
            MemoryStatus.ACTIVE,
            MemoryStatus.HISTORICAL,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.INVALID,
            MemoryStatus.ARCHIVED,
            MemoryStatus.MERGED,
        }
    ),
    # A closed validity window can only be filed away or retracted; it cannot
    # become current again. Re-asserting a fact creates a new memory instead,
    # which keeps the historical record intact. It CAN still turn out to be a
    # confirmed duplicate of something else (MERGED is an identity claim, not a
    # temporal one -- ADR 0010 -- so it is orthogonal to how the window closed).
    MemoryStatus.HISTORICAL: frozenset(
        {MemoryStatus.ARCHIVED, MemoryStatus.INVALID, MemoryStatus.MERGED}
    ),
    MemoryStatus.SUPERSEDED: frozenset(
        {MemoryStatus.ARCHIVED, MemoryStatus.INVALID, MemoryStatus.MERGED}
    ),
    MemoryStatus.INVALID: frozenset({MemoryStatus.ARCHIVED}),
    MemoryStatus.ARCHIVED: frozenset(),
    # MERGED can still be separately invalidated or archived later (the merge
    # decision and the "is this fact even true" question are independent), but
    # never un-merged via this generic transition table -- reversal, where it
    # exists at all (entity merges only, PHASE4_PLAN.md section 10), is a
    # distinct, explicit action with its own audit trail, never a status reset.
    MemoryStatus.MERGED: frozenset({MemoryStatus.ARCHIVED, MemoryStatus.INVALID}),
}


def can_transition(current: MemoryStatus, target: MemoryStatus) -> bool:
    """Whether `current` -> `target` is a legal status change.

    A transition to the same status is a no-op and always permitted.
    """
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_valid_at(
    valid_from: datetime | None,
    valid_until: datetime | None,
    at: datetime,
) -> bool:
    """Whether a fact with this validity window holds at instant `at`.

    Uses the half-open interval ``[valid_from, valid_until)``. A missing
    `valid_from` is treated as "true since the beginning of time" and a missing
    `valid_until` as "true indefinitely".
    """
    if valid_from is not None and at < valid_from:
        return False
    return not (valid_until is not None and at >= valid_until)


def is_currently_believed(
    status: MemoryStatus, valid_until: datetime | None, now: datetime
) -> bool:
    """Whether a fact should be presented as part of the user's *current* knowledge.

    Requires both a believed status and an open (or future) validity window; a memory
    left ACTIVE with an elapsed `valid_until` is not current.
    """
    from app.models.enums import BELIEVED_STATUSES

    if status not in BELIEVED_STATUSES:
        return False
    return valid_until is None or valid_until > now


def validate_window(valid_from: datetime | None, valid_until: datetime | None) -> None:
    """Raise if a validity window is inside-out.

    Zero-length windows are rejected too: with a half-open interval,
    ``valid_from == valid_until`` describes a fact that is never true, which is
    almost certainly a caller error rather than an intent.
    """
    from app.core.errors import ValidationError

    if valid_from is not None and valid_until is not None and valid_until <= valid_from:
        raise ValidationError(
            "valid_until must be strictly after valid_from",
            details={
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
            },
        )
