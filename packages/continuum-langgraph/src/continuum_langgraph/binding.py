"""Declaring which parts of a graph's state mean something portable.

A LangGraph ``StateGraph`` is a bag of typed channels. Continuum cannot guess
which channel is the agent's objective and which is a scratch counter, and
guessing wrong is worse than not guessing: a channel silently mapped to
``memory`` would travel to another runtime carrying a meaning nobody intended.

So the mapping is declared. A :class:`GraphBinding` says which channels are the
goal, which accumulate memory, and which name artifacts. Everything else is
still captured -- nothing is lost -- but it lands in the adapter-defined
``execution.cursor``, which the format explicitly does not interpret.

The practical consequence is the interesting one. Channels named in a binding
become *portable*: another runtime that has never heard of LangGraph can read
them. Channels not named in a binding are preserved but only meaningful to
LangGraph. That split is what the migration report is reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GraphBinding:
    """Maps graph channels onto portable sections of the state model."""

    goal_channel: str | None = None
    """Channel holding the agent's objective, if any."""

    memory_channels: tuple[str, ...] = ()
    """Channels whose contents become portable memory entries.

    Expected to hold sequences. A scalar channel is wrapped as a single entry
    rather than rejected, since a runtime may legitimately keep one rolling
    summary.
    """

    artifact_channels: tuple[str, ...] = ()
    """Channels naming files the agent produced, as paths relative to a workspace."""

    memory_kind: str = "episodic"
    """Which memory category entries from ``memory_channels`` are recorded as."""

    pinned_channels: tuple[str, ...] = ()
    """Channels whose memory entries are pinned, so compaction never drops them."""

    def portable_channels(self) -> set[str]:
        named = {*self.memory_channels, *self.artifact_channels, *self.pinned_channels}
        if self.goal_channel:
            named.add(self.goal_channel)
        return named

    def opaque_channels(self, all_channels: list[str]) -> list[str]:
        """Channels captured but meaningful only to LangGraph."""
        return sorted(set(all_channels) - self.portable_channels())

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_channel": self.goal_channel,
            "memory_channels": list(self.memory_channels),
            "artifact_channels": list(self.artifact_channels),
            "memory_kind": self.memory_kind,
            "pinned_channels": list(self.pinned_channels),
        }


@dataclass(frozen=True)
class GraphFingerprint:
    """A structural identity for a compiled graph.

    The graph's *topology is not part of the state document* -- a Continuum
    image carries what the agent knows, not the program that was running it.
    That is a real limitation, and the honest way to handle it is to make it
    detectable rather than to write it in a README and hope.

    So export records a fingerprint of the node and edge sets, and import
    compares. Restoring a checkpoint into a graph with different topology is
    then a clear refusal instead of an agent that resumes into the wrong node
    and produces plausible nonsense.
    """

    nodes: tuple[str, ...] = ()
    edges: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, compiled: Any) -> GraphFingerprint:
        drawn = compiled.get_graph()
        return cls(
            nodes=tuple(sorted(str(n) for n in drawn.nodes)),
            edges=tuple(sorted((str(e.source), str(e.target)) for e in drawn.edges)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.nodes), "edges": [list(e) for e in self.edges]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphFingerprint:
        return cls(
            nodes=tuple(data.get("nodes", ())),
            edges=tuple(tuple(e) for e in data.get("edges", ())),
        )

    def differences(self, other: GraphFingerprint) -> list[str]:
        problems: list[str] = []
        missing_nodes = set(self.nodes) - set(other.nodes)
        extra_nodes = set(other.nodes) - set(self.nodes)
        if missing_nodes:
            problems.append(f"destination graph is missing node(s): {sorted(missing_nodes)}")
        if extra_nodes:
            problems.append(f"destination graph has extra node(s): {sorted(extra_nodes)}")

        missing_edges = set(self.edges) - set(other.edges)
        extra_edges = set(other.edges) - set(self.edges)
        if missing_edges:
            problems.append(f"destination graph is missing edge(s): {sorted(missing_edges)}")
        if extra_edges:
            problems.append(f"destination graph has extra edge(s): {sorted(extra_edges)}")
        return problems


@dataclass
class ExportedFields:
    """Bookkeeping returned alongside an export, for the interop report."""

    portable: list[str] = field(default_factory=list)
    adapter_only: list[str] = field(default_factory=list)
    dropped_on_migration: list[str] = field(default_factory=list)
