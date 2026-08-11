"""Making a runtime checkpointable without importing anything from Continuum.

This is the ecosystem claim, demonstrated rather than asserted: ``TinyPlanner``
below is an ordinary class that knows nothing about Continuum's types. It gets
checkpointed, forked, moved, and resumed anyway, because the protocol is
structural and the state model is plain data.

Run it:

    python examples/custom_adapter.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from continuum import Store, checkpoint, diff_states, fork, resume
from continuum.model import (
    AgentState,
    Execution,
    ExecutionStatus,
    Identity,
    MemoryEntry,
    MemoryKind,
    Objective,
    Provider,
)
from continuum.model import Capabilities as Caps


class TinyPlanner:
    """A three-step planner with its own internal representation.

    Note what is *not* here: no base class, no Continuum import in the class
    body, no decorators. Only two methods that speak the state model.
    """

    name = "tiny-planner"

    def __init__(self, goal: str = "ship the release", bias: str = "safety") -> None:
        self.goal = goal
        self.bias = bias
        self.done: list[str] = []
        self.notes: list[str] = []

    # -- the runtime's own logic ---------------------------------------

    STEPS = ("gather", "decide", "commit")

    def step(self) -> bool:
        if len(self.done) >= len(self.STEPS):
            return False
        stage = self.STEPS[len(self.done)]
        if stage == "gather":
            self.notes.append("collected 3 open risks")
        elif stage == "decide":
            # The bias is what makes two forks genuinely diverge.
            choice = "defer launch" if self.bias == "safety" else "launch now"
            self.notes.append(f"decision under {self.bias} bias: {choice}")
        else:
            self.notes.append(f"committed: {self.notes[-1]}")
        self.done.append(stage)
        return len(self.done) < len(self.STEPS)

    # -- the adapter protocol ------------------------------------------

    def export_state(self) -> AgentState:
        # Repeatable: nothing minted here. No timestamps, no ids, no randomness.
        return AgentState(
            identity=Identity(agent_id="planner"),
            objective=Objective(goal=self.goal),
            execution=Execution(
                current_task=self.STEPS[len(self.done)] if len(self.done) < 3 else "complete",
                status=(
                    ExecutionStatus.COMPLETED
                    if len(self.done) == len(self.STEPS)
                    else ExecutionStatus.RUNNING
                ),
                step=len(self.done),
                cursor={"done": list(self.done)},
            ),
            provider=Provider(adapter=self.name, provider="none", params={"bias": self.bias}),
            memory=[
                MemoryEntry(id=f"note-{i}", kind=MemoryKind.EPISODIC, content=note)
                for i, note in enumerate(self.notes)
            ],
            capabilities=Caps(requires=["planning.write"], granted=["planning.write"]),
        )

    def import_state(self, state: AgentState) -> None:
        self.goal = state.objective.goal
        self.bias = str(state.provider.params.get("bias", "safety"))
        self.done = list(state.execution.cursor.get("done", []))
        self.notes = [m.content for m in state.memory if m.id.startswith("note-")]


def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="continuum-example-"))
    store = Store.init(workspace)

    planner = TinyPlanner()
    checkpoint(planner, store, label="start")
    planner.step()  # gather
    mid = checkpoint(planner, store, label="after-gather")
    print(f"checkpointed mid-plan: {mid.digest.split(':')[1][:12]}")

    # Re-checkpointing an agent that has not moved writes nothing.
    assert checkpoint(planner, store).digest == mid.digest
    print("re-checkpoint without stepping: same digest, nothing written")

    # Fork the decision point and run both branches to completion.
    branches = fork(store.get_state(mid.digest), store, ["safety", "speed"])
    outcomes = {}
    for branch in branches:
        seeded = branch.state.replace(
            provider=Provider(
                adapter="tiny-planner", provider="none", params={"bias": branch.label}
            )
        )
        runner, report = resume(seeded, TinyPlanner, granted=["planning.*"])
        assert report.ok
        while runner.step():
            pass
        outcomes[branch.label] = runner.export_state()
        print(f"  {branch.label:<8} -> {runner.notes[-1]}")

    print()
    print(diff_states(outcomes["safety"], outcomes["speed"]).render())
    print()
    print(f"store: {store.root}")


if __name__ == "__main__":
    main()
