"""Cross-runtime interoperability demonstration.

Moves a real LangGraph agent through Continuum and back, then shows exactly
what a *different* runtime can and cannot do with the same image.

Runs offline: the graph's nodes are plain Python functions, so there is no
model, no key, and no network anywhere in this script.

    python examples/interop_demo.py

Six scenes. Two of them are refusals, because a demonstration that only shows
things working is not evidence that the checks exist.
"""

from __future__ import annotations

import operator
import tempfile
from pathlib import Path
from typing import Annotated, TypedDict

from continuum import inspect_image, read_image, write_image
from continuum.errors import AdapterError
from continuum.migrate import MigrationTarget, migrate
from langgraph.graph import END, START, StateGraph

from continuum_langgraph import GraphBinding, LangGraphAdapter

# ---------------------------------------------------------------- the graph


class ReviewState(TypedDict):
    goal: str
    findings: Annotated[list[str], operator.add]
    stage: str
    scratch: dict


def scan(state: ReviewState) -> dict:
    return {
        "findings": ["auth-service.md", "billing.txt"],
        "stage": "extract",
        "scratch": {"scanned": 2, "cursor_token": "internal-42"},
    }


def extract(state: ReviewState) -> dict:
    return {"findings": ["FIXME insecure default credentials"], "stage": "rank"}


def rank(state: ReviewState) -> dict:
    return {"findings": ["ranked #1: FIXME insecure default credentials"], "stage": "done"}


def build_graph() -> StateGraph:
    builder = StateGraph(ReviewState)
    builder.add_node("scan", scan)
    builder.add_node("extract", extract)
    builder.add_node("rank", rank)
    builder.add_edge(START, "scan")
    builder.add_edge("scan", "extract")
    builder.add_edge("extract", "rank")
    builder.add_edge("rank", END)
    return builder


def build_graph_without_extract() -> StateGraph:
    """A different program with the same channels."""
    builder = StateGraph(ReviewState)
    builder.add_node("scan", scan)
    builder.add_node("rank", rank)
    builder.add_edge(START, "scan")
    builder.add_edge("scan", "rank")
    builder.add_edge("rank", END)
    return builder


BINDING = GraphBinding(goal_channel="goal", memory_channels=("findings",))


def scene(number: int, title: str) -> None:
    print()
    print(f"== {number}. {title} ".ljust(72, "="))
    print()


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="continuum-interop-"))
    print("LangGraph <-> Continuum interoperability")
    print(f"  workspace  {workspace}")
    print("  provider   plain Python graph nodes (no model, no network)")

    # -- 1 -------------------------------------------------------------
    scene(1, "Run a LangGraph agent partway, then stop")
    source = LangGraphAdapter(
        build_graph, agent_id="lg-reviewer", thread_id="production-1", binding=BINDING
    )
    source.start({"goal": "review the corpus", "findings": [], "stage": "scan", "scratch": {}})
    source.step()  # scan
    source.step()  # extract

    state = source.export_state()
    print(f"  next node    {state.execution.current_task!r} (step {state.execution.step})")
    print(f"  channels     {sorted(state.execution.cursor['channel_values'])}")
    print(f"  repeatable   {source.export_state().digest() == state.digest()}")

    # -- 2 -------------------------------------------------------------
    scene(2, "Export, and inspect it with tooling that knows nothing about LangGraph")
    image = write_image(state, workspace / "langgraph-agent.asi")
    summary = inspect_image(image)
    print(f"  wrote        {image.name} ({image.stat().st_size} bytes)")
    print(
        f"  verified     round trip {'matches' if read_image(image).state.digest() == state.digest() else 'MISMATCH'}"
    )
    print(f"  agent        {summary['agent_id']}")
    print(f"  objective    {summary['objective']}")
    print(f"  memory       {summary['counts']['memory']} portable entries")
    print(f"  provider     {summary['provider']['adapter']}/{summary['provider']['provider']}")
    print()
    print("  Core Continuum read all of that with langgraph never imported.")

    # -- 3 -------------------------------------------------------------
    scene(3, "What survives a move to a different runtime")
    report = migrate(
        state,
        MigrationTarget(
            adapter="native-reviewer",
            provider="mock",
            model="deterministic-reviewer",
            granted_capabilities=["graph.execute", "filesystem.read", "filesystem.write"],
        ),
    ).report
    print(report.render())

    exported = source.last_export
    print("  LangGraph-specific detail:")
    print(f"    portable channels      {exported.portable}")
    print(f"    adapter-only channels  {exported.adapter_only}")
    print("    dropped on migration:")
    for field in exported.dropped_on_migration:
        print(f"      - {field}")

    # -- 4 -------------------------------------------------------------
    scene(4, "Resume in a completely fresh LangGraph runtime")
    destination = LangGraphAdapter(
        build_graph,
        agent_id="lg-reviewer",
        thread_id="disaster-recovery-host",  # different thread, new checkpointer
        binding=BINDING,
    )
    destination.import_state(read_image(image).state)
    print(
        f"  before resume  stage={destination.values['stage']!r} findings={len(destination.values['findings'])}"
    )

    final = destination.run_to_completion()
    print(
        f"  after resume   stage={destination.values['stage']!r} findings={len(destination.values['findings'])}"
    )
    print(f"  status         {final.execution.status.value}")
    print()
    print("  It continued rather than restarting:")
    scanned = destination.values["findings"].count("auth-service.md")
    print(
        f"    scan output appears {scanned} time(s) -- {'not re-run' if scanned == 1 else 'RE-RAN (bug)'}"
    )
    print(
        f"    rank output present -- {'ranked #1: FIXME insecure default credentials' in destination.values['findings']}"
    )

    moved = destination.export_state()
    print()
    print("  Identity did not survive, exactly as reported in scene 3:")
    print(f"    source checkpoint_id  {state.provider.opaque['checkpoint_id'][:20]}...")
    print(f"    resumed checkpoint_id {moved.provider.opaque['checkpoint_id'][:20]}...")
    print(
        f"    same?                 {state.provider.opaque['checkpoint_id'] == moved.provider.opaque['checkpoint_id']}"
    )

    # -- 5 -------------------------------------------------------------
    scene(5, "Refuse a resume into a different program")
    wrong = LangGraphAdapter(build_graph_without_extract, thread_id="wrong-graph", binding=BINDING)
    try:
        wrong.import_state(state)
        print("  UNEXPECTED: mismatched graph accepted")
    except AdapterError as exc:
        print(f"  REFUSED: {exc}")

    # -- 6 -------------------------------------------------------------
    scene(6, "What the other runtime genuinely cannot take")
    print("  The native reference runtime can read the portable sections of this")
    print("  image -- objective, memory, artifacts, capabilities, events -- but it")
    print("  cannot resume the graph, because a LangGraph frontier is meaningless")
    print("  to it. Continuum reports that rather than pretending otherwise:")
    print()
    print(f"    execution.cursor keys : {sorted(state.execution.cursor)}")
    print("    interpretable by      : langgraph adapter only")
    print("    portable to any runtime: objective, memory, artifacts, capabilities, events")
    print()
    print("  A Continuum image carries what the agent knows, not the program that")
    print("  was running it. Cross-framework resume is not claimed; cross-framework")
    print("  *inspection and analysis* is, and works.")

    print()
    print(f"image kept at {image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
