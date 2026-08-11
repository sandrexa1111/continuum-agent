"""The three verbs: checkpoint, fork, resume.

Everything below is a thin, opinionated layer over the model and the store.
The opinions:

**Checkpointing an agent that has not moved is free and produces no new
state.** Idempotence matters because checkpointing belongs inside retry loops
and supervisor restarts, where it will be called far more often than the agent
actually advances.

**Forking is not copying.** A fork records ancestry and shares every unchanged
object with its parent, so N branches cost roughly one state plus N small
deltas rather than N full copies.

**Resuming is gated.** A state that declares required capabilities the
destination cannot grant does not run. The override exists and is explicit
(``allow_degraded``), because the alternative -- an agent that resumes without
the ability to finish and discovers this halfway through -- is worse than a
refusal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .adapters.base import ContinuumAdapter
from .capabilities import CompatibilityReport, check_capabilities
from .errors import AdapterError, ResumeBlocked, StoreError
from .image import Image, read_image, write_image
from .model import AgentState, ExecutionStatus, Lineage
from .store import CheckpointRef, Store

AdapterT = TypeVar("AdapterT", bound=ContinuumAdapter)
"""Preserves the caller's concrete adapter type through :func:`resume`.

``resume(image, NativeReviewAgent, ...)`` returns a ``NativeReviewAgent``, not
a bare protocol object, so runtime-specific methods stay reachable and
type-checked without a cast at every call site.
"""


def checkpoint(
    agent: ContinuumAdapter,
    store: Store,
    *,
    label: str = "",
) -> CheckpointRef:
    """Snapshot ``agent`` into ``store`` and return the resulting reference.

    If the agent's state is unchanged since the last checkpoint, the existing
    reference is returned and nothing new is written.
    """
    state = agent.export_state()
    state.validate()

    head = store.head(state.identity.agent_id)
    if head and store.has(head):
        previous = store.get_state(head)
        if previous.core_digest() == state.core_digest():
            existing = store.find_checkpoint(head)
            if existing:
                return existing
        state = state.replace(
            lineage=Lineage(
                parent=head,
                root=previous.lineage.root or head,
                forked_from=state.lineage.forked_from,
                fork_label=state.lineage.fork_label,
                generation=previous.lineage.generation + 1,
            )
        )
    else:
        state = state.replace(
            lineage=Lineage(
                parent=None,
                root=None,
                forked_from=state.lineage.forked_from,
                fork_label=state.lineage.fork_label,
                generation=0,
            )
        )

    return store.record_checkpoint(state, label=label)


@dataclass(frozen=True)
class Fork:
    label: str
    digest: str
    state: AgentState


def fork(
    state: AgentState,
    store: Store,
    labels: Sequence[str],
) -> list[Fork]:
    """Create independent branches from one state.

    Each branch is a full, valid state that records the checkpoint it came
    from. Branches share storage with their parent for everything they have not
    changed, so the cost of a fork is the delta, not the state.

    Labels must be unique -- two branches with the same name are
    indistinguishable in ``continuum history``, which defeats the purpose of
    running them side by side.
    """
    if not labels:
        raise ValueError("fork requires at least one label")
    if len(set(labels)) != len(labels):
        raise ValueError(f"fork labels must be unique, got {list(labels)}")

    parent_digest = store.put_state(state)
    root = state.lineage.root or parent_digest

    forks: list[Fork] = []
    for label in labels:
        branch = state.replace(
            lineage=Lineage(
                parent=parent_digest,
                root=root,
                forked_from=parent_digest,
                fork_label=label,
                generation=state.lineage.generation + 1,
            )
        )
        branch = branch.with_event(
            "agent.forked",
            {"from": parent_digest, "label": label, "siblings": len(labels)},
        )
        ref = store.record_checkpoint(branch, label=f"fork:{label}")
        forks.append(Fork(label=label, digest=ref.digest, state=branch))

    # record_checkpoint moves the head; after a fan-out there is no single
    # "current" branch, so the head is returned to the fork point.
    store.set_head(state.identity.agent_id, parent_digest)
    return forks


def resume(
    source: AgentState | Image | Path | str,
    factory: Callable[..., AdapterT],
    *,
    granted: Iterable[str] | None = None,
    allow_degraded: bool = False,
    **factory_kwargs: object,
) -> tuple[AdapterT, CompatibilityReport]:
    """Rebuild a runtime from a state, image, or ``.asi`` path.

    Raises :class:`~continuum.errors.ResumeBlocked` when the destination cannot
    satisfy a required capability and ``allow_degraded`` is not set.

    ``allow_degraded`` lifts *Continuum's* gate. It does not, and cannot,
    compel the runtime: an adapter that genuinely has no reduced mode is still
    free to refuse, and the reference adapter does. The override exists for
    runtimes that can do useful work with fewer capabilities -- skipping an
    optional enrichment step, say -- not as a way to force an agent into an
    environment where it cannot function. Both gates are reported honestly
    rather than one silently overriding the other.
    """
    state = _as_state(source)
    grants = sorted(set(granted)) if granted is not None else list(state.capabilities.granted)
    report = check_capabilities(state, grants)

    if not report.ok and not allow_degraded:
        raise ResumeBlocked(report)

    prepared = state.replace(
        capabilities=state.capabilities.__class__(
            requires=state.capabilities.requires,
            optional=state.capabilities.optional,
            granted=grants,
        ),
        execution=state.execution.__class__(
            current_task=state.execution.current_task,
            status=(
                ExecutionStatus.RUNNING
                if state.execution.status in (ExecutionStatus.SUSPENDED, ExecutionStatus.BLOCKED)
                else state.execution.status
            ),
            step=state.execution.step,
            cursor=state.execution.cursor,
            pending_tasks=state.execution.pending_tasks,
        ),
    )
    prepared = prepared.with_event(
        "checkpoint.resumed",
        {
            "verdict": report.verdict.value,
            "degraded": bool(report.missing_required) or bool(report.missing_optional),
            "missing_required": report.missing_required,
        },
    )

    agent = factory(**factory_kwargs)
    try:
        agent.import_state(prepared)
    except AdapterError as exc:
        if report.ok:
            raise
        raise AdapterError(
            f"{exc}\n\n"
            "Continuum's capability gate was overridden with allow_degraded, but the "
            f"adapter refused anyway (missing: {report.missing_required}). The override "
            "lifts Continuum's check; it cannot give a runtime a capability it needs."
        ) from exc
    return agent, report


def checkout(store: Store, ref: str) -> AgentState:
    """Load a state by full digest, unambiguous prefix, or fork label."""
    for checkpoint_ref in store.checkpoints():
        if checkpoint_ref.label in (ref, f"fork:{ref}"):
            return store.get_state(checkpoint_ref.digest)
    try:
        return store.get_state(store.resolve(ref))
    except StoreError as exc:
        raise StoreError(f"{ref!r} is not a known checkpoint, prefix, or label") from exc


def export_image(
    state: AgentState, path: Path | str, blobs: dict[str, bytes] | None = None
) -> Path:
    """Write ``state`` to a portable ``.asi`` image."""
    return write_image(state, path, blobs)


def _as_state(source: AgentState | Image | Path | str) -> AgentState:
    if isinstance(source, AgentState):
        return source
    if isinstance(source, Image):
        return source.state
    return read_image(source).state
