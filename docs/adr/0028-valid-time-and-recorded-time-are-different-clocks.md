# ADR 0028 — Temporal presentation: valid time and recorded time are different clocks; status and temporal state are different axes

**Status:** Accepted (Phase 10)

## Context

The backend is bitemporal (ADR 0002). A memory has:

* **valid time** (`valid_from` / `valid_until`: when the fact was true in
  the world);
* **record time** (`created_at` / `updated_at`: when the system learned or
  changed it);
* a lifecycle **status** (active, conflicting, historical, superseded,
  invalid, archived, merged).

For graph data the server also computes a **temporal state** (current,
historical, future) relative to an instant. Conflating these produces wrong
answers such as "Active, so true now" or "recorded in May, so true since
May".

## Decision

* **Two labelled clocks.**
  * "True from X until Y" or "True since X (no known end)" describes valid
    time.
  * "Recorded …" and "Updated …" describe record time.
  * The UI never invents an end date.
* **Two separate axes.**
  * **Status chips:** "Active" (not "Current"), "Disputed", "Superseded",
    "Retracted", "Duplicate (merged)"…
  * **Temporal chips:** "True now", "No longer true", "Not yet true".

  Every chip has an icon and text, never colour alone. A unit test checks
  that all seven statuses have distinct labels.
* **No client temporal computation.**
  * A memory's temporal state comes from the server's timeline response.
  * Entity "true then / no longer true" comes from
    `/entities/{id}/history?as_of=`.
  * Graph states come from the slice.
  * The only client date work is formatting, and converting the lens's
    local `datetime-local` value to a timezone-aware ISO instant, because
    the backend rejects naive datetimes.
* **One global lens.** "Now / As of" sits in the header and is kept in
  React context.
  * Pages that honour it pass `as_of` to the server.
  * Graph requests send `as_of` only with `temporal_scope=as_of|all`,
    never with `current` (the server returns 422).
  * Pages whose endpoint has no temporal filter (entity search, an
    entity's memory list, documents) show **"Not time-filtered"** instead
    of pretending.

## Consequences

* An E2E test sets the lens to March and checks that the server reports
  EasyOCR, not PaddleOCR, as true then.
* "Relevance" in ranked search is labelled relative and "not a measure of
  truth". Confidence is labelled "extraction confidence: metadata, not a
  probability".
