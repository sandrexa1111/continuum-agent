# continuum-langgraph

A [Continuum](https://github.com/sandrexa1111/continuum-agent) adapter for
[LangGraph](https://github.com/langchain-ai/langgraph).

Checkpoint a running LangGraph agent, export it to a portable `.asi` image, move
it, and resume it in a completely fresh runtime.

```bash
pip install continuum-langgraph
python examples/interop_demo.py     # runs offline: no model, no key, no network
```

## Why this package exists

Continuum's central claim is that its adapter interface is real — that a runtime
nobody wrote for Continuum can be checkpointed through it without changing
Continuum core. A project can assert that; only a second, independent adapter
demonstrates it.

LangGraph is a fair test. It is independently developed, has its own
checkpointing model that does *not* match Continuum's, keeps genuinely
non-portable state, and runs deterministically with plain Python nodes so the
whole demonstration stays offline.

Two structural facts back the claim up:

- This package is **installed separately**. Continuum core has zero runtime
  dependencies and still does; `langgraph` and its tree live behind this
  boundary.
- It is discovered through the `continuum.adapters` entry point, so it appears
  in `continuum adapters` with **no change to Continuum core**.

## What travels, and what does not

| | Where it lands | Survives a move to another runtime? |
|---|---|---|
| Objective, memory, artifacts (channels named in a `GraphBinding`) | portable model sections | **Yes** — readable by any runtime |
| Remaining channel values, `next` frontier, resume node | `execution.cursor` | Preserved, but only LangGraph can interpret it |
| `thread_id`, `checkpoint_id`, `checkpoint_ns` | `provider.opaque` | **No** — dropped and named in the report |
| `versions_seen`, pending task ids, graph fingerprint | `runtime_opaque` | **No** — dropped when the adapter changes |
| The graph itself | *not in the state at all* | **No** — see below |

**The graph is not in the image.** A Continuum image carries what the agent
knows, not the program that was running it, so the destination must already have
the same graph compiled. Rather than leave that as a footnote, export records a
fingerprint of the node and edge sets and import refuses a mismatch:

```text
REFUSED: graph topology mismatch: the checkpoint was taken from a different graph.
  destination graph is missing node(s): ['extract']
  destination graph is missing edge(s): [('extract', 'rank'), ('scan', 'extract')]
Continuum images carry agent state, not the program. Compile the same graph at
the destination.
```

**Cross-framework resume is not claimed.** A LangGraph frontier is meaningless
to another runtime. Cross-framework *inspection and analysis* is claimed, and
works — core Continuum reads the objective, memory, artifacts and event history
of a LangGraph image with `langgraph` never imported.

## Usage

```python
from continuum_langgraph import GraphBinding, LangGraphAdapter

binding = GraphBinding(
    goal_channel="goal",  # -> objective.goal
    memory_channels=("findings",),  # -> portable memory entries
)

agent = LangGraphAdapter(build_graph, thread_id="run-1", binding=binding)
agent.start({"goal": "review the corpus", "findings": [], "stage": "scan"})
agent.step()
agent.step()

state = agent.export_state()  # a normal Continuum AgentState
```

`build_graph` returns an *uncompiled* `StateGraph`. The adapter compiles it with
its own checkpointer, which is what lets a state exported from one process be
imported into a genuinely fresh runtime rather than handed back to the object
that produced it.

Then, anywhere:

```python
from continuum import read_image

destination = LangGraphAdapter(build_graph, thread_id="other-host", binding=binding)
destination.import_state(read_image("agent.asi").state)
destination.run_to_completion()
```

### GraphBinding

A `StateGraph` is a bag of typed channels. Continuum cannot guess which one is
the objective and which is a scratch counter, and guessing wrong is worse than
not guessing — a channel silently mapped to `memory` would travel to another
runtime carrying a meaning nobody intended. So the mapping is declared.

Channels not named in a binding are still captured; they land in
`execution.cursor`, which the format explicitly does not interpret.

## Two bugs this package found

Both were caught by the conformance tests, and both have regression tests that
explain themselves:

**Reducer channels duplicated on import.** `findings: Annotated[list, operator.add]`
means LangGraph applies the reducer on `update_state`, so writing an accumulated
value back into a thread that already holds it appends it to itself and silently
doubles the agent's memory. It hid at first because the interop path imports into
a *fresh* thread, where the channel is empty and `[] + values == values`. Only
the round-trip conformance check exposed it. `import_state` now clears the
destination thread first — importing means "become this state", not "merge into
whatever is already here".

**A stale frontier after a resumed step.** Reading `get_state` while the update
stream was still suspended returned a stale `next` on the resume path: the first
step reported correctly and every later one claimed the graph had finished while
a node was still pending. The stream is now closed before the frontier is read.

## Limitations

- **Channel values must be JSON-encodable.** Continuum rejects anything without
  a stable encoding, deliberately. Unknown types are stringified rather than
  allowed to fail at digest time, which means such a channel round-trips as
  text, not as its original object.
- **Ambiguous resume points.** When the frontier has several predecessors that
  all ran — a diamond — the resume node cannot be determined from topology and
  `versions_seen` alone. The adapter records nothing rather than guessing.
- **Tested against LangGraph 1.2.** Earlier and later versions are not claimed
  until they are actually run.
- **`interrupt`/`Command` resumption is not modelled.** Human-in-the-loop
  interrupts carry state this adapter does not currently capture.

## Development

```bash
pip install -e ".[dev]"
pytest        # 32 tests: conformance, regression, migration, discovery
```

The conformance class walks the checklist in
[`spec/adapters.md`](../../spec/adapters.md) section 4 point by point.

## License

Apache-2.0, same as Continuum.
