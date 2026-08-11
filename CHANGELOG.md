# Changelog

Format per [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/), with the caveat that
pre-1.0 minors may break the Python API. The **state format version** is
tracked separately and currently sits at `0.1`.

## [Unreleased]

### Added

- **`continuum-langgraph`** — an adapter for [LangGraph](https://github.com/langchain-ai/langgraph),
  published as a **separate package** so core keeps its zero-dependency
  guarantee. Discovered through the `continuum.adapters` entry point with no
  change to Continuum core, which is the point: the adapter interface is now
  demonstrated rather than asserted.
- **Cross-runtime interoperability demonstration**
  (`packages/continuum-langgraph/examples/interop_demo.py`) — a LangGraph agent
  stopped mid-graph, exported, inspected by core tooling with `langgraph` never
  imported, and resumed to completion in a fresh runtime with a new
  checkpointer. Runs offline. Two of its six scenes are refusals.
- **`spec/agent-state.schema.json`** — the JSON Schema promised in the roadmap.
  Hand-written against the prose spec rather than reflected off the dataclasses,
  so it can disagree with the implementation; CI regenerates and diffs it, and
  20 tests validate real states against it.
- **Graph fingerprinting** — a Continuum image carries agent state, not the
  program that produced it. Export records the graph's node and edge sets and
  import refuses a topology mismatch, turning a documented limitation into a
  checkable one.
- CI jobs asserting that core gains no runtime dependencies, that the adapter is
  discovered without core changes, and that the schema has not drifted.

### Fixed

Two defects in the adapter, both found by its own conformance tests:

- **Reducer channels duplicated on import.** LangGraph applies channel reducers
  on `update_state`, so writing an accumulated value into a non-empty thread
  appended it to itself and silently doubled the agent's memory. `import_state`
  now clears the destination thread first.
- **Stale execution frontier after a resumed step.** Reading graph state while
  the update stream was still suspended returned a stale `next`, so every step
  after the first claimed the graph had finished while a node was still pending.

## [0.1.0] — 2026-08-11

First public release. Experimental.

### Added

- **State model** (`continuum.model`) — versioned, validated `AgentState`
  covering identity, objective, execution, memory, context, capabilities,
  environment, artifacts, events, and lineage. Unknown top-level fields survive
  a round trip.
- **Canonical encoding** (`continuum.canonical`) — deterministic JSON and
  SHA-256 content addressing.
- **Object store** (`continuum.store`) — content-addressed, atomic writes,
  integrity verified on read, checkpoint graph with ancestry.
- **`.asi` images** (`continuum.image`) — ZIP container, fully verified on read,
  hardened against path traversal and decompression bombs.
- **Runtime verbs** (`continuum.runtime`) — `checkpoint` (idempotent), `fork`
  (storage-sharing, isolated), `resume` (capability-gated), `checkout`.
- **Migration** (`continuum.migrate`) — cross-provider translation reporting
  every section as PORTABLE, TRANSLATED, or UNAVAILABLE.
- **Context compaction** (`continuum.compaction`) — model-free deterministic
  summarization that never drops pinned or system messages, and refuses rather
  than truncating instructions.
- **Capabilities** (`continuum.capabilities`) — required vs optional, namespace
  wildcard grants, resume gating.
- **Structured diff** (`continuum.diff`) — memory and artifacts matched by
  identity, capabilities as sets, provider opaque state excluded.
- **Secret scanning** (`continuum.redact`) — ten pattern rules plus an opt-in
  entropy heuristic, with stable non-reversible placeholders.
- **Adapter protocol** (`continuum.adapters`) — structural protocol with
  entry-point discovery, so third-party adapters need no core changes.
- **Reference runtime** — a five-stage review agent that reaches decisions by
  rule, making the demo and test suite fully offline and reproducible.
- **CLI** — `init`, `demo`, `run`, `history`, `inspect`, `export`, `import`,
  `fork`, `resume`, `diff`, `migrate`, `capabilities`, `sanitize`, `verify`,
  `adapters`. Exit codes: 0 ok, 1 error, 2 usage, 3 check failed.
- **Specification** — `spec/state-image.md`, `compatibility.md`, `adapters.md`,
  `store.md`, written to be implementable without reading the source.
- **ADRs** — five records covering canonical JSON, zero dependencies, the ZIP
  container, forward compatibility, and the model-free reference runtime.
- 244 tests, 95% statement coverage, strict mypy, CI across four Python versions
  and three operating systems.

### Known limitations

Enumerated in the README. The load-bearing ones: model internals are out of
scope, only the reference adapter ships, token counts are estimates without a
supplied tokenizer, the secret scanner finds accidents rather than adversaries,
and there is no multi-writer coordination.

[Unreleased]: https://github.com/sandrexa1111/continuum-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sandrexa1111/continuum-agent/releases/tag/v0.1.0
