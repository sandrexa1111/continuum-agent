# Adapter interface

**Version 0.1 · experimental**

An adapter is the whole of what a runtime must implement to become
checkpointable. Continuum never reaches past it.

---

## 1. The protocol

```python
class ContinuumAdapter(Protocol):
    name: str

    def export_state(self) -> AgentState: ...
    def import_state(self, state: AgentState) -> None: ...
```

Checked structurally, not by inheritance. Your class does not need to import
anything from Continuum — useful for frameworks unwilling to take a dependency
on a pre-1.0 project.

### 1.1 `export_state`

MUST be side-effect free.

MUST be **repeatable**: two calls with no intervening execution MUST produce
states with the same content address.

This is the requirement adapters most often get wrong. Minting a timestamp, a
UUID, or a random request id inside `export_state` makes every checkpoint look
like a change. Deduplication stops working, `continuum history` fills with
identical states, and the idempotence that makes checkpointing safe inside a
retry loop disappears.

If your runtime genuinely needs a fresh timestamp, put it in an event at the
point the thing happened, not at export.

### 1.2 `import_state`

MUST either fully apply the state or raise `AdapterError`. Partially applying a
state the runtime cannot honour leaves an agent in a condition that no
checkpoint describes.

SHOULD check the capabilities it needs using `continuum.capabilities.satisfies`
rather than testing membership directly — namespace grants such as
`filesystem.*` are part of the format, and an adapter that re-implements the
check will wrongly reject them.

An adapter MAY refuse a state even when Continuum's own capability gate was
overridden with `allow_degraded`. That override lifts *Continuum's* check; it
cannot give a runtime a capability it needs. An adapter with no reduced mode
should say so plainly.

### 1.3 `RunnableAdapter` (optional)

```python
def step(self) -> bool: ...
```

Advances one unit of work; returns True while more remains. One call MUST
correspond to one checkpointable boundary — that is what makes "stop, move,
continue" exact rather than approximate.

Implementing this lets Continuum drive your runtime for demos and evaluation.
It is not required for checkpointing.

---

## 2. Discovery

Register through the standard entry-point group:

```toml
[project.entry-points."continuum.adapters"]
my-framework = "my_package.continuum_adapter:factory"
```

The value MUST resolve to a callable returning an adapter instance. Discovered
adapters appear in `continuum adapters`.

Publishing an adapter MUST NOT require any change to Continuum. If it does, the
interface is wrong — see [compatibility.md](compatibility.md) §5.

---

## 3. Mapping your runtime onto the model

Guidance, not requirements. The model is deliberately loose where frameworks
legitimately differ.

**Task position → `execution.cursor`.** The one adapter-defined field. A step
index, a plan node id, a queue offset. Continuum stores and diffs it and never
interprets it.

**Your memory architecture → `memory[].kind` + `attributes`.** The five kinds are
coarse on purpose. Keep your own richer labels in `attributes`; a destination
runtime uses `kind` only to decide what it can accept.

**Provider conversation → `context`.** Plain roles and content. Anything
provider-specific goes in `metadata`.

**Server-held handles → `provider.opaque`.** Thread ids, cached prefix ids.
Understand that these are discarded by any cross-provider migration, so nothing
the agent needs to function may live here.

**Framework scratch → `runtime_opaque`.** Discarded when the adapter changes.

**Files the agent produced → `artifacts`.** Reference by content address and
record `derived_from`. The provenance graph is what makes "which run produced
this report?" answerable.

---

## 4. Conformance checklist

An adapter is conformant if:

- [ ] `export_state` is repeatable — two calls, same address
- [ ] `export_state` has no side effects
- [ ] `import_state` is all-or-nothing
- [ ] capability checks go through `satisfies`, so namespace grants work
- [ ] round trip holds: `import_state(export_state())` leaves the runtime
      equivalent
- [ ] a state exported mid-task, imported elsewhere, resumes at the same task
      and step
- [ ] nothing the agent cannot function without lives in an opaque field

The reference adapter's tests in `tests/test_runtime.py::TestReferenceAdapter`
are a template — the same assertions apply to any adapter.

---

## 5. Worked example

`src/continuum/adapters/native.py` is a complete, working adapter of about 300
lines: a five-stage review agent that reads a corpus, extracts findings, ranks
them under a policy, and writes two artifacts with a derivation edge between
them. It reaches its decisions by rule rather than by model, which is why the
test suite is deterministic and the demo runs offline.

It is the shortest honest answer to "what does an adapter actually look like?"
