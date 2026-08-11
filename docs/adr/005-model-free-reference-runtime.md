# ADR 005 — The reference runtime uses no model

**Status:** accepted · **Date:** 2026-08-11

## Context

Continuum needs a runtime to demonstrate and test against. The obvious choice
for an AI-agent project is an agent backed by a real model.

## Decision

The reference adapter (`native-reviewer`) is a real five-stage agent that
reaches its decisions by rule. It reports itself as
`mock/deterministic-reviewer` and is never presented as a model.

Where a real agent would call a model, it consults a `ReviewPolicy` whose
parameters arrive through `provider.params`.

## Rationale

Three things a model-backed reference would have cost:

1. **Deterministic tests.** Content addressing means every assertion about
   digests, deduplication, and fork isolation needs byte-stable output. A
   sampled model makes those tests flaky, or forces mocking so extensive that
   the "real" runtime is fictional anyway.
2. **A reproducible demo.** `continuum demo` produces identical addresses on any
   machine under a pinned clock. That is verified by a test.
3. **Evaluability by strangers.** Anyone can install the package and see the
   whole system work with no key, no account, and no network.

The parts under test — serialization, storage, forking, lineage, migration
diagnostics, capability gating — are identical whether decisions come from a
model or a rule. Nothing about the system's correctness depends on inference.

The policy indirection also makes the fork demonstration honest: two branches
run under different policies genuinely produce different artifacts, so "fork and
compare" shows a real divergence rather than a staged one.

## Consequences

- No evidence here that Continuum works with a *particular* real framework. That
  is stated as a limitation, and framework adapters are the top roadmap item.
- The word "mock" appears in provider fields throughout, which is deliberate — a
  reader should never mistake this for a model integration.
