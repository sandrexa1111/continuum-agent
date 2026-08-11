# ADR 004 — Readers preserve unknown fields

**Status:** accepted · **Date:** 2026-08-11

## Context

If Continuum is used by more than one tool, versions will be mixed: a pipeline
where an older reader loads a document written by a newer writer is normal, not
exceptional.

The default behaviour of most deserializers is to drop what they do not
recognize.

## Decision

A reader preserves unrecognized top-level fields and re-emits them unchanged.
The reference implementation collects them in `AgentState.extensions`.

## Rationale

Silent field-dropping is the worst class of data loss: invisible at the moment
it happens, discovered much later, and nearly impossible to attribute to the
tool that caused it. An agent state that has passed through an older tool would
quietly lose whatever a newer one recorded — with no error anywhere.

Preservation makes mixed-version pipelines merely lossy in capability rather
than lossy in data.

## Consequences

- Round-tripping is a genuine fixed point, and is tested as one.
- Adding an optional field is a MINOR change that older readers tolerate, which
  is what makes the format able to grow at all.
- A newer *minor* is still rejected outright, because a new field can change the
  meaning of an existing one. Preservation handles unknown data; it cannot
  handle unknown semantics.
- Not yet applied to unknown fields *nested inside* known objects. That gap is
  recorded in `spec/compatibility.md` section 2 rather than glossed over.
