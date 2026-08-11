# ADR 001 — Canonical JSON as the addressed form

**Status:** accepted · **Date:** 2026-08-11

## Context

Every guarantee in Continuum rests on content addressing: identical states share
an object, forks cost a delta, lineage is verifiable, diffs are trustworthy.
That only holds if logically-equal documents encode to identical bytes.

The manifests in the original design sketch were YAML, which is friendlier to
write by hand.

## Decision

The addressed form is canonical JSON: UTF-8, sorted keys, no insignificant
whitespace, literal non-ASCII, non-finite floats rejected.

YAML may be accepted as *input* where hand-authoring matters. It is never the
form that gets hashed.

## Why not YAML

YAML has no canonical serialization. Boolean-looking scalars, quoted versus
unquoted strings, block versus flow style, and anchor expansion all round-trip
differently between libraries and even between versions of the same library.
Two conformant YAML writers can emit different bytes for the same document,
which would mean two implementations disagreeing about a state's identity — the
one failure this project cannot absorb.

JSON is not canonical by default either, but the additional rules needed to make
it so are short, checkable, and already standardized (RFC 8785 covers the same
ground).

## Consequences

- Manifests are less pleasant to read by hand. `continuum inspect` exists partly
  to compensate.
- Float encoding depends on shortest-round-trip formatting, which agrees with
  ECMA-262 across every implementation we care about. Very large integers are a
  documented interoperability boundary rather than a silent hazard.
- Changing the encoding is a MAJOR version bump forever, because it invalidates
  every address ever issued.
