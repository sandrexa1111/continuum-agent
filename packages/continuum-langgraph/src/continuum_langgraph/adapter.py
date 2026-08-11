"""A Continuum adapter for LangGraph.

This package exists to test one claim: that Continuum's adapter interface is
real, and that a runtime nobody wrote for Continuum can be checkpointed,
exported, moved and resumed through it without changing Continuum core.

LangGraph is a fair test. It is independently developed, widely used, has its
own checkpointing model that does *not* match Continuum's, and keeps genuinely
non-portable state (checkpoint identity, channel versions, pending task ids).
It also runs perfectly well with plain Python node functions, so the whole
interoperability demonstration stays offline and deterministic.

What travels, and what does not
-------------------------------

**Portable.** Channels named in a :class:`~continuum_langgraph.binding.GraphBinding`
-- the objective, memory-bearing channels, artifact channels. These reach a
runtime that has never heard of LangGraph.

**Adapter-defined.** All remaining channel values, the ``next`` frontier, and
the derived resume node. Captured in ``execution.cursor``, which the format
stores and diffs but never interprets. Another LangGraph can use these; the
native runtime cannot.

**Not portable at all.** LangGraph's checkpoint identity (``checkpoint_id``,
``checkpoint_ns``, ``thread_id``) and its internal bookkeeping
(``versions_seen``, pending task ids). A restored thread gets new ones. These
live in ``provider.opaque`` and ``runtime_opaque`` precisely so that migration
drops them loudly.

**Not in the state at all.** The graph itself. A Continuum image carries what
the agent knows, not the program that was running it, so the destination must
already have the same graph compiled. Rather than leave that as a footnote,
export records a :class:`~continuum_langgraph.binding.GraphFingerprint` and
import refuses a mismatch.
"""

from __future__ import annotations

import importlib.metadata as metadata
import platform
import sys
from collections.abc import Callable
from typing import Any

from continuum.capabilities import satisfies
from continuum.errors import AdapterError
from continuum.model import (
    AgentState,
    Artifact,
    Capabilities,
    Environment,
    Execution,
    ExecutionStatus,
    Identity,
    MemoryEntry,
    MemoryKind,
    Objective,
    Provider,
)

from .binding import ExportedFields, GraphBinding, GraphFingerprint

ADAPTER_NAME = "langgraph"

REQUIRED_CAPABILITIES = ("graph.execute",)

_START = "__start__"
_END = "__end__"
_INTERNAL_NODES = frozenset({_START, _END, "__input__", "__interrupt__"})


class LangGraphAdapter:
    """Checkpoint and resume a compiled LangGraph application.

    ``graph_factory`` returns a *builder* (an uncompiled ``StateGraph``). The
    adapter compiles it with its own checkpointer, which is what lets a state
    exported from one process be imported into a genuinely fresh runtime rather
    than handed back to the object that produced it.
    """

    name = ADAPTER_NAME

    def __init__(
        self,
        graph_factory: Callable[[], Any],
        *,
        agent_id: str = "langgraph-agent",
        thread_id: str = "default",
        binding: GraphBinding | None = None,
        interrupt_before: tuple[str, ...] = (),
        checkpointer: Any | None = None,
        capabilities: Capabilities | None = None,
        workspace: str = ".",
    ) -> None:
        from langgraph.checkpoint.memory import InMemorySaver

        self.graph_factory = graph_factory
        self.agent_id = agent_id
        self.thread_id = thread_id
        self.binding = binding or GraphBinding()
        self.workspace = workspace
        self._checkpointer = checkpointer or InMemorySaver()
        self._app = graph_factory().compile(
            checkpointer=self._checkpointer,
            interrupt_before=list(interrupt_before) or None,
        )
        self._capabilities = capabilities or Capabilities(
            requires=list(REQUIRED_CAPABILITIES), granted=list(REQUIRED_CAPABILITIES)
        )
        self._started = False
        self._initial: dict[str, Any] | None = None
        self._base_thread_id = thread_id
        self._import_generation = 0
        self.last_export: ExportedFields = ExportedFields()

    # -- driving -------------------------------------------------------

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    def start(self, initial: dict[str, Any]) -> None:
        """Seed the graph without running it."""
        self._initial = dict(initial)

    def step(self) -> bool:
        """Advance the graph by one node. Returns True while work remains.

        LangGraph's own unit of progress is a super-step, so this streams a
        single update rather than calling ``invoke``, which would run to the
        next interrupt. One call, one checkpointable boundary -- the contract
        ``spec/adapters.md`` asks for.

        The stream is closed before the frontier is read, and that ordering is
        load-bearing. Reading ``get_state`` while the generator is still
        suspended returns a stale frontier on the resume path -- the first step
        reported correctly and every later one claimed the graph was finished
        while ``next`` still held a node. Closing first forces LangGraph to
        finish committing the super-step.
        """
        payload: Any = None
        if not self._started:
            if self._initial is None:
                raise AdapterError("call start(initial) before stepping a fresh graph")
            payload = self._initial
            self._started = True

        stream = self._app.stream(payload, self.config, stream_mode="updates")
        try:
            next(stream, None)
        finally:
            stream.close()
        return bool(self._app.get_state(self.config).next)

    def run_to_completion(self, max_steps: int = 200) -> AgentState:
        steps = 0
        while self.step():
            steps += 1
            if steps > max_steps:
                raise AdapterError(f"graph did not settle within {max_steps} steps")
        return self.export_state()

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._app.get_state(self.config).values or {})

    # -- adapter protocol ----------------------------------------------

    def export_state(self) -> AgentState:
        """Capture the graph's current position as a portable state document.

        Repeatable, as the protocol requires: no timestamps or identifiers are
        minted here. ``created_at`` is left empty rather than filled with
        ``now()``, because a checkpoint that changes address every time you
        serialize it breaks deduplication.
        """
        snapshot = self._app.get_state(self.config)
        values = dict(snapshot.values or {})
        next_nodes = [str(n) for n in (snapshot.next or ())]
        fingerprint = GraphFingerprint.of(self._app)

        portable: list[str] = []
        memory: list[MemoryEntry] = []
        for channel in self.binding.memory_channels:
            if channel not in values:
                continue
            portable.append(channel)
            raw = values[channel]
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            for index, item in enumerate(items):
                memory.append(
                    MemoryEntry(
                        id=f"{_slug(channel)}-{index:03d}",
                        kind=MemoryKind(self.binding.memory_kind),
                        content=str(item),
                        source=f"langgraph:{channel}",
                        importance=0.6,
                        pinned=channel in self.binding.pinned_channels,
                        attributes={"channel": channel, "index": index},
                    )
                )

        artifacts: list[Artifact] = []
        for channel in self.binding.artifact_channels:
            if channel not in values:
                continue
            portable.append(channel)
            raw = values[channel]
            for item in raw if isinstance(raw, (list, tuple)) else [raw]:
                artifacts.append(
                    Artifact(id=_slug(str(item)), path=str(item), media_type="text/plain")
                )

        goal = ""
        if self.binding.goal_channel and self.binding.goal_channel in values:
            goal = str(values[self.binding.goal_channel])
            portable.append(self.binding.goal_channel)

        adapter_only = self.binding.opaque_channels(list(values))
        resume_node = self._derive_resume_node(next_nodes, snapshot)

        self.last_export = ExportedFields(
            portable=sorted(set(portable)),
            adapter_only=adapter_only,
            dropped_on_migration=[
                "provider.opaque.checkpoint_id",
                "provider.opaque.checkpoint_ns",
                "provider.opaque.thread_id",
                "runtime_opaque.graph_fingerprint",
                "runtime_opaque.langgraph_metadata",
                "runtime_opaque.pending_task_ids",
            ],
        )

        done = not next_nodes
        return AgentState(
            identity=Identity(agent_id=self.agent_id, display_name="LangGraph agent"),
            objective=Objective(goal=goal),
            execution=Execution(
                current_task=next_nodes[0] if next_nodes else "complete",
                status=ExecutionStatus.COMPLETED if done else ExecutionStatus.SUSPENDED,
                step=int((snapshot.metadata or {}).get("step", 0) or 0),
                cursor={
                    # Adapter-defined by the spec: Continuum stores and diffs
                    # this and never interprets it.
                    "next": next_nodes,
                    "resume_as_node": resume_node,
                    "channel_values": _jsonable(values),
                    "adapter_only_channels": adapter_only,
                    "binding": self.binding.to_dict(),
                },
                pending_tasks=next_nodes,
            ),
            provider=Provider(
                adapter=ADAPTER_NAME,
                provider="langgraph",
                model="",
                params={"interrupt_before": []},
                # LangGraph's own identity for this run. A restored thread gets
                # new values, so this must not survive a migration.
                opaque={
                    "thread_id": self.thread_id,
                    "checkpoint_id": str(
                        (snapshot.config or {}).get("configurable", {}).get("checkpoint_id", "")
                    ),
                    "checkpoint_ns": str(
                        (snapshot.config or {}).get("configurable", {}).get("checkpoint_ns", "")
                    ),
                },
            ),
            memory=memory,
            artifacts=artifacts,
            capabilities=self._capabilities,
            environment=Environment(
                os=platform.system().lower(),
                arch=platform.machine().lower(),
                runtime="cpython",
                runtime_version=".".join(str(p) for p in sys.version_info[:3]),
                tools=[f"langgraph=={_version('langgraph')}"],
            ),
            runtime_opaque={
                "graph_fingerprint": fingerprint.to_dict(),
                "langgraph_metadata": _jsonable(dict(snapshot.metadata or {})),
                "pending_task_ids": [str(t.id) for t in (snapshot.tasks or ())],
            },
        )

    def import_state(self, state: AgentState) -> None:
        """Rebuild this runtime so the graph continues from ``state``.

        All-or-nothing: every precondition is checked before anything is
        written, so a state this adapter cannot honour leaves the runtime
        untouched rather than half-applied.
        """
        missing = [c for c in REQUIRED_CAPABILITIES if not satisfies(c, state.capabilities.granted)]
        if missing:
            raise AdapterError(
                f"{ADAPTER_NAME} cannot run without {missing}; grant them "
                "(a namespace grant such as 'graph.*' works) before resuming"
            )

        cursor = state.execution.cursor
        if "channel_values" not in cursor:
            raise AdapterError(
                "this state has no LangGraph channel values in execution.cursor. "
                "It was probably produced by a different adapter -- the portable "
                "sections (objective, memory, artifacts) can be read, but the graph "
                "position cannot be reconstructed from them."
            )

        recorded = state.runtime_opaque.get("graph_fingerprint")
        if recorded:
            source = GraphFingerprint.from_dict(recorded)
            problems = source.differences(GraphFingerprint.of(self._app))
            if problems:
                raise AdapterError(
                    "graph topology mismatch: the checkpoint was taken from a "
                    "different graph.\n  "
                    + "\n  ".join(problems)
                    + "\nContinuum images carry agent state, not the program. "
                    "Compile the same graph at the destination."
                )

        values = dict(cursor["channel_values"])
        resume_node = cursor.get("resume_as_node")
        if resume_node in (None, "", _START):
            resume_node = None

        # Importing means "become this state", not "merge into whatever is
        # already here" -- and on a reducer channel those differ badly.
        # `findings: Annotated[list, operator.add]` makes update_state additive,
        # so writing an accumulated value into a non-empty thread appends it to
        # itself and silently doubles the agent's memory. Clearing first is the
        # only way the round trip is a fixed point.
        self._clear_thread()

        self._app.update_state(self.config, values, as_node=resume_node)
        self._started = True
        self._capabilities = state.capabilities
        self.agent_id = state.identity.agent_id

    def _clear_thread(self) -> None:
        """Empty the destination thread before writing a state into it.

        Prefers the checkpointer's own ``delete_thread``. Not every backend
        implements it -- older releases and third-party savers may not -- so the
        fallback moves to a fresh thread namespace instead. The namespace is
        derived from a counter rather than a random id, so a given sequence of
        operations stays reproducible.
        """
        deleter = getattr(self._checkpointer, "delete_thread", None)
        if callable(deleter):
            try:
                deleter(self.thread_id)
                return
            except (NotImplementedError, AttributeError):
                pass
        self._import_generation += 1
        self.thread_id = f"{self._base_thread_id}#import{self._import_generation}"

    # -- internals -----------------------------------------------------

    def _derive_resume_node(self, next_nodes: list[str], snapshot: Any) -> str | None:
        """Work out which node to impersonate when writing state back.

        ``update_state(as_node=X)`` means "behave as if X just finished", so the
        graph then routes to X's successors. The node we need is therefore the
        *predecessor* of the frontier, not the frontier itself.

        Topology gives the candidates; ``versions_seen`` says which of them
        actually ran. Using both matters for a diamond, where a node can have
        several predecessors and only one of them executed.
        """
        if not next_nodes:
            return None

        frontier = next_nodes[0]
        drawn = self._app.get_graph()
        predecessors = [
            str(e.source)
            for e in drawn.edges
            if str(e.target) == frontier and str(e.source) not in _INTERNAL_NODES
        ]
        if not predecessors:
            return None
        if len(predecessors) == 1:
            return predecessors[0]

        executed = self._executed_nodes()
        ran = [p for p in predecessors if p in executed]
        if len(ran) == 1:
            return ran[0]
        # Ambiguous: prefer determinism over a coin flip, and record nothing
        # rather than guess wrong. import_state will start from the frontier.
        return sorted(ran)[-1] if ran else None

    def _executed_nodes(self) -> set[str]:
        tup = self._checkpointer.get_tuple(self.config)
        if tup is None:
            return set()
        seen = (tup.checkpoint or {}).get("versions_seen", {})
        return {n for n in seen if n not in _INTERNAL_NODES}


def _slug(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return cleaned.strip("-")[:64] or "item"


def _jsonable(value: Any) -> Any:
    """Coerce channel values into something canonically encodable.

    Continuum rejects anything without a stable JSON encoding, deliberately.
    Graph channels can hold arbitrary Python, so unknown types are stringified
    here rather than allowed to blow up at digest time -- with the tradeoff
    that such a channel round-trips as text, not as its original object.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def factory(graph_factory: Callable[[], Any], **kwargs: Any) -> LangGraphAdapter:
    """Entry-point factory for the ``langgraph`` adapter."""
    return LangGraphAdapter(graph_factory, **kwargs)
