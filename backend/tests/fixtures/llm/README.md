# LLM fixtures

Recorded `ExtractionResult` JSON, loadable via `app.llm.fake.load_fixture(name,
ExtractionResult)`. These are an alternative to constructing `CandidateMemory`
objects inline in test code -- most tests in this suite construct them inline
(clearer for a single test), but a fixture file is useful when the same recorded
extraction is reused across several tests, or when hand-authoring a plausible
"real provider response" shape is itself the point (e.g. malformed output).

None of these were produced by a real API call -- they are hand-authored to match
the shape a real response would have, keeping the whole test suite free of any
API key or network dependency.
