# Compatibility and versioning

**Version 0.1 · experimental**

A format that cannot evolve is dead on arrival; one that evolves carelessly
destroys the data written before it. This document defines what a reader must do
when it meets a document it does not fully understand.

---

## 1. Version rule

`format_version` is `MAJOR.MINOR`.

A reader at `M.m` MUST:

| document | behaviour |
|---|---|
| `M.n` where `n ≤ m` | read normally |
| `M.n` where `n > m` | **reject** with a version error |
| `X.n` where `X ≠ M` | **reject** with a version error |

Rejecting a newer minor may look strict, but the alternative is worse. A newer
minor may add a field that changes the *meaning* of one the reader does know —
a new capability-negotiation flag, say. Guessing produces an agent that runs
under the wrong assumptions, which is more damaging than a clear refusal.

A MAJOR bump means "a reader of the previous major must not guess". Everything
that can be added compatibly goes in a MINOR.

---

## 2. Unknown field preservation

A reader MUST preserve top-level fields it does not recognize and re-emit them
unchanged when writing the document back.

This single rule is what makes an ecosystem possible. Without it, any older tool
in a pipeline silently deletes whatever every newer tool wrote — the failure is
invisible, arrives late, and is nearly impossible to attribute.

The reference implementation keeps these in `AgentState.extensions`. Verified by
`tests/test_model.py::TestForwardCompatibility`.

Preservation is REQUIRED for unknown *top-level* fields. It is RECOMMENDED for
unknown fields inside known objects, and the reference implementation does not
yet do this — a gap, recorded honestly rather than papered over.

---

## 3. What may change in a MINOR

Permitted:

- adding an optional field
- adding an enum member to a field whose readers are documented to tolerate
  unknown members
- adding a reserved event type
- relaxing a validation rule

Not permitted:

- removing or renaming a field
- changing a field's type
- tightening a validation rule so that previously valid documents are rejected
- changing how a content address is computed

Changing the canonical encoding is always a MAJOR change: it invalidates every
address ever issued.

---

## 4. Portability classes

Migration reports each section as exactly one of:

**PORTABLE** — moves unchanged; its content address is preserved.

**TRANSLATED** — moves, but not byte-for-byte. Roles remapped, context compacted.
Still means roughly the same thing; no longer the same bytes.

**UNAVAILABLE** — does not move. Named in the report, never dropped silently.

Fixed classifications:

| section | same adapter + provider | different provider | different adapter |
|---|---|---|---|
| `identity`, `objective`, `execution` | PORTABLE | PORTABLE | PORTABLE |
| `memory`, `artifacts`, `events`, `lineage` | PORTABLE | PORTABLE | PORTABLE |
| `context` | PORTABLE | TRANSLATED | TRANSLATED |
| `environment` | TRANSLATED | TRANSLATED | TRANSLATED |
| `provider.opaque` | PORTABLE | UNAVAILABLE | UNAVAILABLE |
| `runtime_opaque` | PORTABLE | PORTABLE | UNAVAILABLE |

`environment` is never PORTABLE, even in place: it describes the host the state
came from, and the destination's values differ by definition. It is retained for
diagnostics.

**Model internals are out of scope entirely.** Attention state, KV caches, and
provider-side prefix caches are not represented, not exported, and not claimed
to move. The unit of portability is execution state.

---

## 5. Design tests for changes

Any change to the format or the interfaces must still pass all four:

1. **Adapter independence.** Can another framework implement an adapter without
   modifying Continuum core? If not, the interface is wrong.
2. **Specification independence.** Can a reader implement the format from
   `spec/` without reading `src/`? If not, the spec is incomplete.
3. **Forward evolution.** Can the format grow without invalidating existing
   images? If not, the versioning is wrong.
4. **Honest loss.** Does every piece of dropped information appear in a report?
   If not, the migration is lying.

---

## 6. Store layout versioning

The object store carries its own `layout_version` (currently `1`), independent
of `format_version`. A store may be reorganized without changing the wire
format, and vice versa. A reader MUST refuse a store whose `layout_version`
exceeds its own.
