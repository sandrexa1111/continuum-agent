# Agent State Image — format specification

**Version 0.1 · experimental**

This document defines the on-disk format. It is written so that an independent
implementation can be built from this file alone, without reading the reference
implementation. Where the two disagree, that is a bug in one of them; please
report it.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are used as in
RFC 2119.

---

## 1. Terms

**State document** — a JSON object describing one agent's execution state.

**Content address** — the string `sha256:` followed by the lowercase hex
SHA-256 digest of some byte sequence.

**Image** — a `.asi` file: a ZIP container holding one state document, its
manifest, and optionally the blobs it references.

**Object store** — a local directory holding many state documents and blobs
addressed by content. Defined in [store.md](store.md).

---

## 2. Canonical encoding

Content addresses are computed over a *canonical* encoding. An implementation
MUST produce byte-identical output for logically equal documents, or addresses
will not agree between implementations.

A canonical encoding MUST:

1. be UTF-8 with no byte-order mark;
2. order object keys ascending by Unicode code point;
3. emit no whitespace between tokens;
4. emit non-ASCII characters literally rather than as `\uXXXX` escapes;
5. reject `NaN`, `Infinity`, and `-Infinity`;
6. reject non-string object keys.

Numbers:

- Integers MUST be encoded without a decimal point or exponent.
- Non-integer numbers MUST use the shortest representation that round-trips to
  the same IEEE-754 double (equivalent to ECMA-262 `Number::toString`).
- Producers SHOULD keep integers within ±2^53 so that readers using IEEE-754
  doubles do not lose precision. This specification does not forbid larger
  integers, but interoperability is not guaranteed for them.

The content address of a state document is the address of its canonical
encoding.

### 2.1 Empty and absent

A producer MUST omit an optional field whose value is `null`, `""`, `[]`, or
`{}`. Absent and empty therefore encode identically, and two states differing
only in that respect share an address.

The fields `format_version` and `identity` are REQUIRED and MUST be emitted even
if `identity` is otherwise empty.

---

## 3. The state document

```json
{
  "format_version": "0.1",
  "identity":     { "agent_id": "analyst-7", "display_name": "…", "created_at": "…" },
  "objective":    { "goal": "…", "constraints": [], "success_criteria": [] },
  "execution":    { "current_task": "…", "status": "suspended", "step": 3,
                    "cursor": {}, "pending_tasks": [] },
  "provider":     { "adapter": "…", "provider": "…", "model": "…",
                    "params": {}, "opaque": {} },
  "memory":       [ { "id": "mem-1", "kind": "episodic", "content": "…" } ],
  "context":      [ { "role": "system", "content": "…", "pinned": true } ],
  "capabilities": { "requires": [], "optional": [], "granted": [] },
  "environment":  { "os": "…", "arch": "…", "env_var_names": [] },
  "artifacts":    [ { "id": "report", "path": "…", "digest": "sha256:…",
                      "derived_from": [] } ],
  "events":       [ { "seq": 0, "ts": "…", "type": "task.changed", "data": {} } ],
  "lineage":      { "parent": "sha256:…", "root": "sha256:…", "generation": 1 },
  "runtime_opaque": {}
}
```

### 3.1 `format_version`

`MAJOR.MINOR`. See [compatibility.md](compatibility.md).

### 3.2 `identity`

| field | type | notes |
|---|---|---|
| `agent_id` | string | REQUIRED. MUST match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |
| `display_name` | string | optional |
| `created_at` | string | RFC 3339 UTC, second precision |

The identifier character set is deliberately narrow: agent ids appear in file
paths and log lines.

### 3.3 `execution`

| field | type | notes |
|---|---|---|
| `current_task` | string | free-form label |
| `status` | enum | `running`, `suspended`, `blocked`, `completed`, `failed` |
| `step` | integer | ≥ 0 |
| `cursor` | object | **adapter-defined.** Readers MUST preserve it and MUST NOT interpret it |
| `pending_tasks` | array of string | |

`cursor` is the runtime's own resumption pointer. It is the one field whose
shape the format does not constrain.

### 3.4 `memory`

Each entry:

| field | type | notes |
|---|---|---|
| `id` | string | REQUIRED, unique within the document, same charset as `agent_id` |
| `kind` | enum | `working`, `episodic`, `semantic`, `procedural`, `external-reference` |
| `content` | string | REQUIRED |
| `created_at` | string | RFC 3339 UTC |
| `source` | string | free-form provenance |
| `importance` | number | within `[0, 1]` |
| `pinned` | boolean | compaction MUST NOT drop pinned entries |
| `transformed` | boolean | see below |
| `attributes` | object | producer-defined |

`transformed` MUST be `true` when the entry was produced by summarizing or
rewriting other state rather than observed by the agent. Readers rely on this to
distinguish an observation from a summary of one; a producer that omits it on a
generated summary is non-conformant.

The five kinds are intentionally coarse. Continuum does not impose a memory
architecture; a framework with a richer taxonomy keeps its own labels in
`attributes`.

### 3.5 `context`

An ordered array of provider-facing messages: `role`, `content`, and optional
`name`, `tool_call_id`, `pinned`, `metadata`.

This is the least portable section. Cross-provider migration re-encodes it; see
[compatibility.md](compatibility.md) §4.

### 3.6 `capabilities`

| field | meaning |
|---|---|
| `requires` | hard gate. A resume MUST fail if any is unsatisfied |
| `optional` | soft. A resume MAY proceed, and SHOULD report degradation |
| `granted` | what the environment that produced this state provided |

Capability names are dotted (`filesystem.read`, `email.send`).

- A **grant** MAY end in `.*` to cover a namespace, or be exactly `*`.
- A **requirement** MUST NOT contain a wildcard. "Something under filesystem"
  is not a checkable claim.
- A name MUST NOT appear in both `requires` and `optional`.

### 3.7 `artifacts`

| field | notes |
|---|---|
| `id` | REQUIRED, unique within the document |
| `path` | REQUIRED, relative to the workspace. MUST NOT be absolute or contain `..` |
| `digest` | content address of the artifact's bytes |
| `media_type` | defaults to `application/octet-stream` |
| `derived_from` | array of artifact `id`s in the same document |

`derived_from` forms the provenance graph. Every referenced id MUST exist in the
document, and the graph MUST be acyclic. A cyclic graph makes "where did this
come from?" unanswerable, so readers MUST reject it.

### 3.8 `events`

Append-only. `seq` MUST be a non-negative integer, unique, and non-decreasing
across the array.

Reserved types — producers SHOULD use these names for these meanings, and MAY
define others:

```text
checkpoint.created     checkpoint.resumed     agent.forked
tool.called            memory.written         artifact.created
task.changed           permission.changed     context.compacted
migration.started      migration.completed    state.sanitized
```

### 3.9 `lineage`

| field | notes |
|---|---|
| `parent` | content address of the state this descends from |
| `root` | content address of the origin of this chain |
| `forked_from` | content address this branch diverged at |
| `fork_label` | branch name |
| `generation` | non-negative integer, depth from root |

Because these are content addresses, ancestry is *verifiable*: given the parent
object, a reader can confirm the claim rather than trust it.

### 3.10 `provider.opaque` and `runtime_opaque`

Both are producer-defined objects for state that is **not portable**:
server-side conversation handles, cached prefix identifiers, adapter scratch
data.

- They MUST be preserved across a same-adapter, same-provider resume.
- They MUST be dropped on migration to a different provider (`provider.opaque`)
  or a different adapter (`runtime_opaque`), and the loss MUST be reported.

Producers MUST NOT place anything in these fields that the agent cannot function
without, since any migration will discard them.

---

## 4. The `.asi` container

A ZIP archive (deflate or store).

```text
manifest.json          REQUIRED
state.json             REQUIRED — canonical encoding of the state document
objects/<hex>          OPTIONAL — blobs, each named by its own digest, no prefix
```

### 4.1 `manifest.json`

```json
{
  "format_version": "0.1",
  "producer": "continuum-agent/0.1.0",
  "created_at": "2026-08-11T14:10:58Z",
  "state_digest": "sha256:…",
  "agent_id": "reviewer",
  "objects": { "sha256:…": { "size": 1234 } },
  "artifacts_external": ["sha256:…"]
}
```

`artifacts_external` lists artifact digests referenced by the state but *not*
packed in this image. Artifacts may legitimately live elsewhere; the manifest
records which, so a reader knows what it is missing before it starts.

### 4.2 Verification (normative)

On read, an implementation MUST, before returning any content to a caller:

1. reject the file if it is not a valid ZIP container;
2. reject it if any bytes follow the end-of-central-directory record — the
   archive MUST end exactly where that record says it does;
3. reject it if `manifest.json` or `state.json` is absent;
4. reject it if `manifest.format_version` is unreadable under
   [compatibility.md](compatibility.md);
5. recompute the digest of `state.json` and reject the file unless it equals
   `manifest.state_digest`;
6. recompute the digest of every `objects/<hex>` entry and reject the file
   unless it equals its own name.

Rule 2 is easy to omit and important. ZIP readers locate the central directory
by scanning backwards from the end of the file, so appending bytes to a valid
archive leaves every entry and every digest unchanged — the image still verifies
under rules 3–6 alone. Trailing data must be treated as tampering.

An implementation MUST also refuse entries whose paths are absolute or contain a
`..` component, and SHOULD refuse an archive whose total uncompressed size is
implausible for a state image. Images are exchanged between machines and may
arrive from an untrusted source; the container is a security boundary, not a
convenience.

---

## 5. Conformance

An implementation is **conformant at 0.1** if it:

- produces canonical encodings matching §2 (checkable: the same document must
  yield the same address as any other conformant implementation);
- enforces the validation rules of §3.4, §3.6, §3.7, and §3.8;
- performs every check in §4.2 before exposing content;
- preserves unknown fields per [compatibility.md](compatibility.md) §2;
- never emits secret *values* in `environment` — names only.

---

## 6. Status

Experimental. `0.1` is a first attempt to write this down precisely, not a
standard. The file extension `.asi` is claimed by convention only.

Feedback on the format is more valuable than feedback on the implementation. If
a rule here makes your runtime awkward to represent, that is a specification
bug worth filing.
