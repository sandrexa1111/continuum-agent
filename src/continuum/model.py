"""The portable agent-state model.

This module defines what Continuum considers "an agent's state" and nothing
else -- no I/O, no storage, no provider calls. Keeping it pure is what lets the
format specification in ``spec/state-image.md`` be checked against real code.

Three ideas drive the shape of these types:

**Portability is a property of a field, not of the document.** Some state
genuinely moves between runtimes (the objective, structured memory, artifact
references). Some can only be translated (a provider's message representation).
Some cannot move at all (a provider's server-side conversation handle). The
model separates these instead of pretending everything is portable; see
:class:`Provider.opaque` and :mod:`continuum.migrate`.

**Unknown fields survive a round trip.** A reader built against format 0.1 that
loads a document written by a future 0.1.x writer preserves the fields it does
not understand in ``extensions`` and re-emits them on write. Without this, any
tool in the ecosystem silently destroys data written by any newer tool.

**Nothing is implicitly timestamped.** Every constructor takes its timestamps
explicitly so that tests, replays, and content addresses are reproducible. The
convenience of an implicit ``datetime.now()`` is not worth a state format whose
digest changes every time you serialize it.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .canonical import digest as content_digest
from .errors import FormatError, VersionError

FORMAT_VERSION = "0.1"
"""Format version written by this build.

Compatibility rule (``spec/compatibility.md``): readers accept any document
whose major version matches and whose minor version is less than or equal to
their own, preserving unknown fields. A major bump means "this reader must not
guess".
"""

_SUPPORTED_MAJOR = 0

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


_clock: Callable[[], str] | None = None


def now_iso() -> str:
    """Return the current UTC time as a second-precision ISO 8601 string.

    Second precision on purpose: sub-second timestamps make state documents
    differ on fields nobody is reading, which shows up as spurious diffs.

    Routed through a replaceable clock so that determinism is testable. Content
    addresses depend on timestamps, so "the same run produces the same digests"
    is only a checkable claim if the clock can be pinned -- see
    :func:`fixed_clock`.
    """
    if _clock is not None:
        return _clock()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def fixed_clock(
    start: str = "2026-01-01T00:00:00Z", step_seconds: int = 0
) -> Iterator[Callable[[], str]]:
    """Pin :func:`now_iso` to a deterministic sequence for the duration.

    With the default ``step_seconds=0`` every call returns ``start``, which is
    what reproducibility tests want. A positive step advances the clock on each
    call, which is useful for exercising ordering without waiting in real time.

    Intended for tests, replays, and reproducible demos. Production code should
    leave the system clock alone -- a state whose timestamps are fiction is
    worse than one whose digests vary.
    """
    global _clock
    previous = _clock
    moment = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    counter = {"n": 0}

    def tick() -> str:
        current = moment + timedelta(seconds=step_seconds * counter["n"])
        counter["n"] += 1
        return current.isoformat().replace("+00:00", "Z")

    _clock = tick
    try:
        yield tick
    finally:
        _clock = previous


def _require_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise FormatError(
            f"{what} must match {_ID_PATTERN.pattern} (got {value!r}); "
            "identifiers appear in file paths and event logs, so the character "
            "set is deliberately narrow"
        )
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise FormatError(f"{what} must be a string, got {type(value).__name__}")
    return value


def _str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FormatError(f"{what} must be a list of strings, got {type(value).__name__}")
    return [_require_str(v, f"{what}[]") for v in value]


def _dict_or_empty(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FormatError(f"{what} must be an object, got {type(value).__name__}")
    return dict(value)


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    """Remove empty optional fields so absent and empty encode identically.

    Two states that differ only in whether an empty list was written must share
    a content address, otherwise deduplication and diffs both misbehave.
    """
    return {k: v for k, v in data.items() if v not in (None, [], {}, "")}


class MemoryKind(str, Enum):
    """Coarse memory categories.

    Deliberately coarse. Continuum does not impose a memory architecture; these
    categories only exist so a destination runtime can decide what it is able to
    accept. A framework with a richer taxonomy keeps its own labels in
    :attr:`MemoryEntry.attributes`.
    """

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    EXTERNAL = "external-reference"


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Identity:
    agent_id: str
    display_name: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "agent_id": self.agent_id,
                "display_name": self.display_name,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Identity:
        return cls(
            agent_id=_require_id(data.get("agent_id", ""), "identity.agent_id"),
            display_name=data.get("display_name"),
            created_at=data.get("created_at", ""),
        )


@dataclass(frozen=True)
class Objective:
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "goal": self.goal,
                "constraints": list(self.constraints),
                "success_criteria": list(self.success_criteria),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Objective:
        return cls(
            goal=data.get("goal", ""),
            constraints=_str_list(data.get("constraints"), "objective.constraints"),
            success_criteria=_str_list(data.get("success_criteria"), "objective.success_criteria"),
        )


@dataclass(frozen=True)
class Execution:
    """Where the agent is in its work.

    ``cursor`` is the one field a runtime may shape freely: it is the
    adapter's own resumption pointer (a step index, a queue offset, a plan node
    id). Continuum stores and diffs it but never interprets it.
    """

    current_task: str = ""
    status: ExecutionStatus = ExecutionStatus.SUSPENDED
    step: int = 0
    cursor: dict[str, Any] = field(default_factory=dict)
    pending_tasks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "current_task": self.current_task,
                "status": self.status.value,
                "step": self.step,
                "cursor": dict(self.cursor),
                "pending_tasks": list(self.pending_tasks),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Execution:
        raw_status = data.get("status", ExecutionStatus.SUSPENDED.value)
        try:
            status = ExecutionStatus(raw_status)
        except ValueError as exc:
            raise FormatError(
                f"execution.status {raw_status!r} is not one of "
                f"{[s.value for s in ExecutionStatus]}"
            ) from exc
        step = data.get("step", 0)
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise FormatError(f"execution.step must be a non-negative integer, got {step!r}")
        return cls(
            current_task=data.get("current_task", ""),
            status=status,
            step=step,
            cursor=_dict_or_empty(data.get("cursor"), "execution.cursor"),
            pending_tasks=_str_list(data.get("pending_tasks"), "execution.pending_tasks"),
        )


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    kind: MemoryKind
    content: str
    created_at: str = ""
    source: str = ""
    importance: float = 0.5
    pinned: bool = False
    transformed: bool = False
    """True if this entry was produced by compaction rather than by the agent.

    Compaction rewrites history. A destination runtime, a human reviewing a
    checkpoint, and any evaluation built on top of these images all need to be
    able to tell original observations from summaries of them.
    """
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "id": self.id,
                "kind": self.kind.value,
                "content": self.content,
                "created_at": self.created_at,
                "source": self.source,
                "importance": self.importance,
                "pinned": self.pinned,
                "transformed": self.transformed,
                "attributes": dict(self.attributes),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        try:
            kind = MemoryKind(data.get("kind", MemoryKind.WORKING.value))
        except ValueError as exc:
            raise FormatError(
                f"memory.kind {data.get('kind')!r} is not one of {[k.value for k in MemoryKind]}"
            ) from exc
        importance = data.get("importance", 0.5)
        if not isinstance(importance, (int, float)) or isinstance(importance, bool):
            raise FormatError(f"memory.importance must be numeric, got {importance!r}")
        if not 0.0 <= float(importance) <= 1.0:
            raise FormatError(f"memory.importance must be within [0, 1], got {importance!r}")
        return cls(
            id=_require_id(data.get("id", ""), "memory.id"),
            kind=kind,
            content=_require_str(data.get("content", ""), "memory.content"),
            created_at=data.get("created_at", ""),
            source=data.get("source", ""),
            importance=float(importance),
            pinned=bool(data.get("pinned", False)),
            transformed=bool(data.get("transformed", False)),
            attributes=_dict_or_empty(data.get("attributes"), "memory.attributes"),
        )


@dataclass(frozen=True)
class Message:
    """One turn of provider-facing conversation context.

    This is the least portable part of the model and is treated as such: see
    :func:`continuum.migrate.migrate`, which reports the context section as
    TRANSLATED rather than PORTABLE for any cross-provider move.
    """

    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    pinned: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "role": self.role,
                "content": self.content,
                "name": self.name,
                "tool_call_id": self.tool_call_id,
                "pinned": self.pinned,
                "metadata": dict(self.metadata),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=_require_str(data.get("role", ""), "context.role"),
            content=_require_str(data.get("content", ""), "context.content"),
            name=data.get("name"),
            tool_call_id=data.get("tool_call_id"),
            pinned=bool(data.get("pinned", False)),
            metadata=_dict_or_empty(data.get("metadata"), "context.metadata"),
        )


@dataclass(frozen=True)
class Artifact:
    """A file the agent produced, addressed by content.

    ``derived_from`` carries artifact ids, forming the artifact graph. It is a
    DAG by construction -- :meth:`AgentState.validate` rejects cycles -- because
    a cyclic provenance graph makes "what did this report come from?"
    unanswerable.
    """

    id: str
    path: str
    digest: str = ""
    media_type: str = "application/octet-stream"
    derived_from: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "id": self.id,
                "path": self.path,
                "digest": self.digest,
                "media_type": self.media_type,
                "derived_from": list(self.derived_from),
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            id=_require_id(data.get("id", ""), "artifact.id"),
            path=_require_str(data.get("path", ""), "artifact.path"),
            digest=data.get("digest", ""),
            media_type=data.get("media_type", "application/octet-stream"),
            derived_from=_str_list(data.get("derived_from"), "artifact.derived_from"),
            created_at=data.get("created_at", ""),
        )


@dataclass(frozen=True)
class Capabilities:
    """What the agent needs from its environment, and what it currently has.

    ``requires`` is a hard gate on resume; ``optional`` degrades with a warning.
    Splitting them is the difference between "this agent cannot run here" and
    "this agent will run here with less reach", and conflating the two is how
    agents silently resume without the ability to finish their task.
    """

    requires: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "requires": sorted(set(self.requires)),
                "optional": sorted(set(self.optional)),
                "granted": sorted(set(self.granted)),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capabilities:
        return cls(
            requires=_str_list(data.get("requires"), "capabilities.requires"),
            optional=_str_list(data.get("optional"), "capabilities.optional"),
            granted=_str_list(data.get("granted"), "capabilities.granted"),
        )


@dataclass(frozen=True)
class Environment:
    """A description of where the agent was running.

    Names of environment variables are recorded; values never are. Recording the
    names is genuinely useful for diagnosing a failed resume ("the source had
    ``GITHUB_TOKEN`` set, this host does not") and carries no secret.
    """

    os: str = ""
    arch: str = ""
    runtime: str = ""
    runtime_version: str = ""
    workspace_digest: str = ""
    git_commit: str = ""
    env_var_names: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "os": self.os,
                "arch": self.arch,
                "runtime": self.runtime,
                "runtime_version": self.runtime_version,
                "workspace_digest": self.workspace_digest,
                "git_commit": self.git_commit,
                "env_var_names": sorted(set(self.env_var_names)),
                "tools": sorted(set(self.tools)),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Environment:
        return cls(
            os=data.get("os", ""),
            arch=data.get("arch", ""),
            runtime=data.get("runtime", ""),
            runtime_version=data.get("runtime_version", ""),
            workspace_digest=data.get("workspace_digest", ""),
            git_commit=data.get("git_commit", ""),
            env_var_names=_str_list(data.get("env_var_names"), "environment.env_var_names"),
            tools=_str_list(data.get("tools"), "environment.tools"),
        )


@dataclass(frozen=True)
class Provider:
    """Which model/runtime produced this state.

    ``opaque`` is explicitly non-portable: provider-side conversation handles,
    cached prefixes, server-managed thread ids. It is preserved on a same-
    provider resume and dropped -- loudly -- on migration.
    """

    adapter: str = ""
    provider: str = ""
    model: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    opaque: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "adapter": self.adapter,
                "provider": self.provider,
                "model": self.model,
                "params": dict(self.params),
                "opaque": dict(self.opaque),
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Provider:
        return cls(
            adapter=data.get("adapter", ""),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            params=_dict_or_empty(data.get("params"), "provider.params"),
            opaque=_dict_or_empty(data.get("opaque"), "provider.opaque"),
        )


@dataclass(frozen=True)
class Event:
    """An append-only record of something observable that happened."""

    seq: int
    ts: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {"seq": self.seq, "ts": self.ts, "type": self.type, "data": dict(self.data)}
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        seq = data.get("seq", 0)
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise FormatError(f"event.seq must be a non-negative integer, got {seq!r}")
        return cls(
            seq=seq,
            ts=data.get("ts", ""),
            type=_require_str(data.get("type", ""), "event.type"),
            data=_dict_or_empty(data.get("data"), "event.data"),
        )


@dataclass(frozen=True)
class Lineage:
    """Ancestry of this state within the checkpoint graph.

    ``parent`` is a content address, so lineage is verifiable rather than
    merely asserted: given the parent object you can confirm the claim.
    """

    parent: str | None = None
    root: str | None = None
    forked_from: str | None = None
    fork_label: str = ""
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "parent": self.parent,
                "root": self.root,
                "forked_from": self.forked_from,
                "fork_label": self.fork_label,
                "generation": self.generation,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lineage:
        generation = data.get("generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise FormatError(
                f"lineage.generation must be a non-negative integer, got {generation!r}"
            )
        return cls(
            parent=data.get("parent"),
            root=data.get("root"),
            forked_from=data.get("forked_from"),
            fork_label=data.get("fork_label", ""),
            generation=generation,
        )


@dataclass
class AgentState:
    """A complete, portable snapshot of an agent's execution state."""

    identity: Identity
    format_version: str = FORMAT_VERSION
    objective: Objective = field(default_factory=Objective)
    execution: Execution = field(default_factory=Execution)
    provider: Provider = field(default_factory=Provider)
    memory: list[MemoryEntry] = field(default_factory=list)
    context: list[Message] = field(default_factory=list)
    capabilities: Capabilities = field(default_factory=Capabilities)
    environment: Environment = field(default_factory=Environment)
    artifacts: list[Artifact] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    lineage: Lineage = field(default_factory=Lineage)
    runtime_opaque: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    """Top-level fields written by a newer or unknown producer.

    Preserved verbatim so that round-tripping a document through an older
    reader is lossless. See ``spec/compatibility.md``.
    """

    # -- serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "format_version": self.format_version,
            "identity": self.identity.to_dict(),
            "objective": self.objective.to_dict(),
            "execution": self.execution.to_dict(),
            "provider": self.provider.to_dict(),
            "memory": [m.to_dict() for m in self.memory],
            "context": [m.to_dict() for m in self.context],
            "capabilities": self.capabilities.to_dict(),
            "environment": self.environment.to_dict(),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "events": [e.to_dict() for e in self.events],
            "lineage": self.lineage.to_dict(),
            "runtime_opaque": dict(self.runtime_opaque),
        }
        data = _drop_empty(data)
        # format_version and identity are mandatory even when they look empty.
        data["format_version"] = self.format_version
        data["identity"] = self.identity.to_dict()
        for key, value in self.extensions.items():
            data.setdefault(key, value)
        return data

    _KNOWN_KEYS = frozenset(
        {
            "format_version",
            "identity",
            "objective",
            "execution",
            "provider",
            "memory",
            "context",
            "capabilities",
            "environment",
            "artifacts",
            "events",
            "lineage",
            "runtime_opaque",
        }
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentState:
        if not isinstance(data, dict):
            raise FormatError(f"state document must be an object, got {type(data).__name__}")
        version = data.get("format_version")
        if not isinstance(version, str) or not version:
            raise FormatError("state document is missing a format_version")
        check_version(version)
        state = cls(
            format_version=version,
            identity=Identity.from_dict(_dict_or_empty(data.get("identity"), "identity")),
            objective=Objective.from_dict(_dict_or_empty(data.get("objective"), "objective")),
            execution=Execution.from_dict(_dict_or_empty(data.get("execution"), "execution")),
            provider=Provider.from_dict(_dict_or_empty(data.get("provider"), "provider")),
            memory=[MemoryEntry.from_dict(m) for m in data.get("memory", []) or []],
            context=[Message.from_dict(m) for m in data.get("context", []) or []],
            capabilities=Capabilities.from_dict(
                _dict_or_empty(data.get("capabilities"), "capabilities")
            ),
            environment=Environment.from_dict(
                _dict_or_empty(data.get("environment"), "environment")
            ),
            artifacts=[Artifact.from_dict(a) for a in data.get("artifacts", []) or []],
            events=[Event.from_dict(e) for e in data.get("events", []) or []],
            lineage=Lineage.from_dict(_dict_or_empty(data.get("lineage"), "lineage")),
            runtime_opaque=_dict_or_empty(data.get("runtime_opaque"), "runtime_opaque"),
            extensions={k: v for k, v in data.items() if k not in cls._KNOWN_KEYS},
        )
        state.validate()
        return state

    # -- integrity -----------------------------------------------------

    def digest(self) -> str:
        """Content address of this state.

        Two states with the same address are the same state; this is what makes
        forks free and deduplication correct.
        """
        return content_digest(self.to_dict())

    def core_digest(self) -> str:
        """Content address of everything *except* lineage.

        Lineage describes where a state sits in the checkpoint graph, not what
        the agent is. Without a lineage-free address, checkpointing an idle
        agent would append a new parent link, change the digest, and grow an
        infinite chain of states that are all the same work. This is the
        comparison :func:`continuum.runtime.checkpoint` uses to decide whether
        anything actually happened.
        """
        data = self.to_dict()
        data.pop("lineage", None)
        return content_digest(data)

    def validate(self) -> None:
        """Check invariants that the per-field parsers cannot see.

        Raises :class:`FormatError` on the first violation.
        """
        check_version(self.format_version)

        memory_ids = [m.id for m in self.memory]
        _reject_duplicates(memory_ids, "memory entry id")

        artifact_ids = [a.id for a in self.artifacts]
        _reject_duplicates(artifact_ids, "artifact id")

        known_artifacts = set(artifact_ids)
        for artifact in self.artifacts:
            for parent in artifact.derived_from:
                if parent not in known_artifacts:
                    raise FormatError(
                        f"artifact {artifact.id!r} derives from unknown artifact {parent!r}; "
                        "the artifact graph must be closed within the state"
                    )
        _reject_artifact_cycles(self.artifacts)

        seqs = [e.seq for e in self.events]
        if seqs != sorted(seqs):
            raise FormatError("events must be ordered by ascending seq")
        _reject_duplicates([str(s) for s in seqs], "event seq")

        overlap = set(self.capabilities.requires) & set(self.capabilities.optional)
        if overlap:
            raise FormatError(
                f"capabilities {sorted(overlap)} are declared both required and optional; "
                "a capability is either a hard gate on resume or it is not"
            )

    def replace(self, **changes: Any) -> AgentState:
        """Return a copy with ``changes`` applied, validated."""
        updated = dataclasses.replace(self, **changes)
        updated.validate()
        return updated

    def next_event_seq(self) -> int:
        return (self.events[-1].seq + 1) if self.events else 0

    def with_event(self, type: str, data: dict[str, Any] | None = None, ts: str = "") -> AgentState:
        """Return a copy with one event appended."""
        event = Event(seq=self.next_event_seq(), ts=ts or now_iso(), type=type, data=data or {})
        return self.replace(events=[*self.events, event])


def _reject_duplicates(values: list[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise FormatError(f"duplicate {what}: {value!r}")
        seen.add(value)


def _reject_artifact_cycles(artifacts: list[Artifact]) -> None:
    """Depth-first cycle detection over the artifact provenance graph."""
    edges = {a.id: list(a.derived_from) for a in artifacts}
    UNVISITED, ACTIVE, DONE = 0, 1, 2
    marks = dict.fromkeys(edges, UNVISITED)

    def visit(node: str, path: list[str]) -> None:
        if marks.get(node) == ACTIVE:
            cycle = " -> ".join([*path, node])
            raise FormatError(f"artifact provenance graph contains a cycle: {cycle}")
        if marks.get(node) == DONE:
            return
        marks[node] = ACTIVE
        for parent in edges.get(node, []):
            visit(parent, [*path, node])
        marks[node] = DONE

    for node in edges:
        visit(node, [])


def check_version(version: str) -> None:
    """Raise :class:`VersionError` if this build cannot read ``version``."""
    parts = version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError) as exc:
        raise FormatError(f"format_version {version!r} is not MAJOR.MINOR") from exc
    supported_minor = int(FORMAT_VERSION.split(".")[1])
    if major != _SUPPORTED_MAJOR or minor > supported_minor:
        raise VersionError(found=version, supported=FORMAT_VERSION)
