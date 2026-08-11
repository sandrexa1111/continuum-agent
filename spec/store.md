# Object store layout

**Layout version 1 · experimental**

The store is local, on-disk, and deliberately simple enough to inspect with
`ls`. It is the counterpart to the `.asi` image: images move one state between
machines, the store keeps many related states cheaply and records how they
descend from each other.

---

## 1. Layout

```text
<workdir>/.continuum/
├── config.json                 { "layout_version": 1, "created_at": "…" }
├── objects/
│   └── <first two hex>/<remaining 62 hex>      zlib-compressed object bytes
└── refs/
    ├── checkpoints.json        the checkpoint index
    └── heads.json              { "<agent_id>": "sha256:…" }
```

`.continuum` is located by walking upward from the working directory, as `git`
does for `.git`.

---

## 2. Objects

An object's name is the content address of its **uncompressed** bytes. zlib
compression is a storage detail; if the address depended on it, two stores using
different compression levels would disagree about identity.

The two-character directory fan-out keeps directory sizes reasonable on
filesystems that degrade when a single directory holds very many entries.

### 2.1 Immutability

Objects are write-once. Storing bytes that are already present is a no-op — this
is exactly what makes forking cheap, since branches share every object they have
not changed.

### 2.2 Atomicity

Every write MUST go to a temporary file in the destination directory and then be
renamed into place. A store interrupted mid-write is then *missing* an object,
which is recoverable; a store containing a half-written object under a valid
address is not, because every later read would trust it.

### 2.3 Verification

A reader MUST recompute the digest on load and refuse the object if it does not
match its address. `continuum verify` walks every object and reports failures;
content addressing is only a guarantee if something actually checks it.

---

## 3. `refs/checkpoints.json`

An array of index entries:

```json
[
  {
    "digest": "sha256:…",
    "agent_id": "reviewer",
    "created_at": "2026-08-11T14:10:58Z",
    "label": "after-extract",
    "parent": "sha256:…",
    "forked_from": null
  }
]
```

This index is **metadata, not truth**. Ancestry is also recorded inside each
state's `lineage` and is verifiable from the objects alone, so `refs/` is a
traversal accelerator that can be rebuilt by scanning `objects/`. An
implementation MUST NOT treat a missing or stale index as data loss.

Recording a state already present in the index MUST NOT create a second entry.

---

## 4. `refs/heads.json`

Maps `agent_id` to the most recent checkpoint for that agent. Advanced by
`checkpoint`; reset to the fork point by `fork`, since after a fan-out there is
no single current branch.

---

## 5. Concurrency

**Not yet coordinated.** The atomic-rename discipline means concurrent writers
will not corrupt individual objects, but `refs/*.json` is read-modify-write and
concurrent updates can lose an index entry — recoverable, since the objects
survive and the index is rebuildable, but not silent-safe.

One writer per store for now. Locking is on the roadmap and is listed as a
limitation in the README rather than quietly assumed away.

---

## 6. Garbage

There is currently no collection. Objects unreachable from any checkpoint stay
on disk. For the workloads this is built for — tens to thousands of small state
documents — that is an acceptable trade, and a real `gc` command is on the
roadmap.
