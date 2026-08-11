"""Structured diff between two agent states.

A textual diff of two state documents is nearly useless -- reordered keys and
shifted list indices bury the one line you care about. This module diffs the
model instead: memory and artifacts are matched by identity, capabilities as
sets, scalars field by field.

The output answers the question people actually ask of two checkpoints: *what
did the agent do between here and there?*
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import AgentState, Artifact, MemoryEntry


@dataclass(frozen=True)
class FieldChange:
    path: str
    before: Any
    after: Any

    def render(self) -> str:
        return f"    {self.path}: {_brief(self.before)} -> {_brief(self.after)}"


@dataclass(frozen=True)
class SetDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.added and not self.removed

    def to_dict(self) -> dict[str, list[str]]:
        return {"added": list(self.added), "removed": list(self.removed)}


@dataclass(frozen=True)
class CollectionDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": list(self.modified),
        }


@dataclass(frozen=True)
class StateDiff:
    left: str
    right: str
    identity: list[FieldChange] = field(default_factory=list)
    objective: list[FieldChange] = field(default_factory=list)
    execution: list[FieldChange] = field(default_factory=list)
    provider: list[FieldChange] = field(default_factory=list)
    memory: CollectionDelta = field(default_factory=CollectionDelta)
    artifacts: CollectionDelta = field(default_factory=CollectionDelta)
    capabilities: SetDelta = field(default_factory=SetDelta)
    context_delta: int = 0
    events_added: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(
            [
                self.identity,
                self.objective,
                self.execution,
                self.provider,
                not self.memory.empty,
                not self.artifacts.empty,
                not self.capabilities.empty,
                self.context_delta,
                self.events_added,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "identity": [vars(c) for c in self.identity],
            "objective": [vars(c) for c in self.objective],
            "execution": [vars(c) for c in self.execution],
            "provider": [vars(c) for c in self.provider],
            "memory": self.memory.to_dict(),
            "artifacts": self.artifacts.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "context_message_delta": self.context_delta,
            "events_added": list(self.events_added),
        }

    def render(self) -> str:
        if self.empty:
            return f"{self.left[:19]} -> {self.right[:19]}\n  (identical state)"
        lines = [f"{self.left[:19]} -> {self.right[:19]}"]

        for title, changes in (
            ("Identity", self.identity),
            ("Objective", self.objective),
            ("Execution", self.execution),
            ("Provider", self.provider),
        ):
            if changes:
                lines.append(f"  {title}:")
                lines += [c.render() for c in changes]

        if not self.memory.empty:
            lines.append("  Memory:")
            lines += [f"    + {i}" for i in self.memory.added]
            lines += [f"    - {i}" for i in self.memory.removed]
            lines += [f"    ~ {i}" for i in self.memory.modified]

        if not self.artifacts.empty:
            lines.append("  Artifacts:")
            lines += [f"    + {i}" for i in self.artifacts.added]
            lines += [f"    - {i}" for i in self.artifacts.removed]
            lines += [f"    ~ {i} (contents changed)" for i in self.artifacts.modified]

        if not self.capabilities.empty:
            lines.append("  Capabilities:")
            lines += [f"    + {c}" for c in self.capabilities.added]
            lines += [f"    - {c}" for c in self.capabilities.removed]

        if self.context_delta:
            sign = "+" if self.context_delta > 0 else ""
            lines.append(f"  Context:\n    {sign}{self.context_delta} messages")

        if self.events_added:
            lines.append(f"  Events (+{len(self.events_added)}):")
            lines += [f"    {e}" for e in self.events_added[:12]]
            if len(self.events_added) > 12:
                lines.append(f"    ... {len(self.events_added) - 12} more")

        return "\n".join(lines)


def diff_states(left: AgentState, right: AgentState) -> StateDiff:
    """Compare two states, ``left`` treated as the earlier one."""
    return StateDiff(
        left=left.digest(),
        right=right.digest(),
        identity=_scalar_changes("identity", left.identity.to_dict(), right.identity.to_dict()),
        objective=_scalar_changes("objective", left.objective.to_dict(), right.objective.to_dict()),
        execution=_scalar_changes("execution", left.execution.to_dict(), right.execution.to_dict()),
        # provider.opaque is deliberately excluded: it is unreadable by design
        # and would report a change on every single turn for most runtimes.
        provider=_scalar_changes(
            "provider",
            {k: v for k, v in left.provider.to_dict().items() if k != "opaque"},
            {k: v for k, v in right.provider.to_dict().items() if k != "opaque"},
        ),
        memory=_collection_delta(
            {m.id: m for m in left.memory}, {m.id: m for m in right.memory}, _memory_identity
        ),
        artifacts=_collection_delta(
            {a.id: a for a in left.artifacts},
            {a.id: a for a in right.artifacts},
            _artifact_identity,
        ),
        capabilities=SetDelta(
            added=sorted(set(right.capabilities.requires) - set(left.capabilities.requires)),
            removed=sorted(set(left.capabilities.requires) - set(right.capabilities.requires)),
        ),
        context_delta=len(right.context) - len(left.context),
        events_added=_new_events(left, right),
    )


def _scalar_changes(
    prefix: str, before: dict[str, Any], after: dict[str, Any]
) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old != new:
            changes.append(FieldChange(f"{prefix}.{key}", old, new))
    return changes


def _memory_identity(entry: MemoryEntry) -> tuple[Any, ...]:
    return (entry.kind.value, entry.content, entry.importance, entry.pinned, entry.transformed)


def _artifact_identity(artifact: Artifact) -> tuple[Any, ...]:
    return (artifact.digest, artifact.path, tuple(artifact.derived_from))


def _collection_delta(
    before: dict[str, Any], after: dict[str, Any], identity: Any
) -> CollectionDelta:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(
        key for key in set(before) & set(after) if identity(before[key]) != identity(after[key])
    )
    return CollectionDelta(added=added, removed=removed, modified=modified)


def _new_events(left: AgentState, right: AgentState) -> list[str]:
    """Events present in ``right`` past the last sequence number in ``left``.

    Uses the sequence number rather than set difference so that an event log
    which legitimately repeats a type is not collapsed.
    """
    cutoff = left.events[-1].seq if left.events else -1
    return [f"{e.seq:>4}  {e.type}" for e in right.events if e.seq > cutoff]


def _brief(value: Any, limit: int = 60) -> str:
    text = "(unset)" if value in (None, "", [], {}) else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
