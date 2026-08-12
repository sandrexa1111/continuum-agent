# continuum-langgraph

LangGraph adapter for [Continuum](../../README.md).

The package demonstrates that Continuum's adapter boundary works with an independently developed runtime: a LangGraph execution can be stopped mid-graph, exported through Continuum, inspected as portable state, and resumed in a fresh LangGraph runtime with a new checkpointer.

> **Status:** experimental `v0.1.0`. Tested against the LangGraph version used by this repository's CI.

## Install from this repository

From the `continuum-agent` repository root:

```bash
pip install -e .
pip install -e ./packages/continuum-langgraph
```

Then run the offline interoperability demonstration:

```bash
python packages/continuum-langgraph/examples/interop_demo.py
```

The demo uses deterministic Python nodes; it does not require a model, API key, or network call.

## What this adapter proves

`continuum-langgraph` is installed separately from Continuum core.

- `continuum-agent` keeps zero runtime dependencies
- LangGraph is only required by this adapter package
- the adapter is discovered through the `continuum.adapters` entry point
- Continuum core does not need framework-specific code
- exported state can be imported into a fresh LangGraph runtime rather than handed back to the original object

CI checks these boundaries.

## State mapping

A LangGraph state contains both application-level data and framework-specific execution information. The adapter keeps those categories explicit.

| State | Continuum representation | Portability |
|---|---|---|
| bound objective, memory and artifact channels | portable state sections | readable without LangGraph |
| remaining channel values and execution frontier | `execution.cursor` | preserved, but LangGraph-specific |
| thread/checkpoint identifiers | provider-specific metadata | not portable; reported as dropped |
| graph/runtime bookkeeping | runtime-specific metadata | adapter-specific |
| graph program itself | not stored in the state image | destination must provide it |

## Graph compatibility

A Continuum image carries execution state, not executable graph code.

The adapter fingerprints the graph topology when exporting. Import refuses a destination graph with a different node/edge structure instead of attempting to resume against an incompatible program.

This is intentional: cross-framework **inspection and analysis** of portable state is supported, while cross-framework execution resume is not claimed.

## Basic usage

```python
from continuum import read_image
from continuum_langgraph import GraphBinding, LangGraphAdapter

binding = GraphBinding(
    goal_channel="goal",
    memory_channels=("findings",),
)

source = LangGraphAdapter(
    build_graph,
    thread_id="run-1",
    binding=binding,
)
source.start({"goal": "review the corpus", "findings": [], "stage": "scan"})
source.step()
source.step()

state = source.export_state()
```

At a destination with the same graph definition:

```python
destination = LangGraphAdapter(
    build_graph,
    thread_id="run-2",
    binding=binding,
)
destination.import_state(read_image("agent.asi").state)
destination.run_to_completion()
```

`GraphBinding` is explicit because Continuum should not guess which arbitrary LangGraph channel represents an objective, memory, or artifact. Unbound channels remain available in the framework-specific execution cursor.

## Conformance issues caught during implementation

The adapter tests exposed two framework-integration bugs that are now regression-tested:

1. **Reducer-channel duplication.** Importing accumulated reducer state into a non-empty thread could append the data to itself instead of replacing the destination state. Import now clears the destination thread where supported before restoration.
2. **Stale execution frontier.** Reading graph state while an update stream was still suspended could report an outdated `next` frontier. The stream is closed before the adapter reads the stable frontier.

## Limitations

- Channel values need a stable serializable representation. Unknown objects may lose type information.
- The destination must provide a compatible graph program.
- Some graph shapes have ambiguous resume points; the adapter refuses to guess when topology and checkpoint metadata are insufficient.
- Provider/checkpointer identifiers are intentionally not treated as portable state.
- Cross-framework resume is outside the current claim.

## Development

From the repository root:

```bash
pip install -e .
pip install -e "./packages/continuum-langgraph[dev]"
pytest packages/continuum-langgraph/tests -q
python packages/continuum-langgraph/examples/interop_demo.py
```

The adapter package currently has 32 tests in addition to Continuum core's test suite.
