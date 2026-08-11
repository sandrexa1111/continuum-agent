"""Conformance and regression tests for the LangGraph adapter.

The conformance class walks the checklist in ``spec/adapters.md`` section 4
point by point. If Continuum's adapter interface is real, an independently
built runtime should be able to satisfy it -- and these are the assertions that
decide whether that is true or merely claimed.
"""

from __future__ import annotations

import pytest
from conftest import BINDING, GRANTS, INITIAL, build_different_graph, build_graph
from continuum.adapters.base import ContinuumAdapter, RunnableAdapter
from continuum.errors import AdapterError
from continuum.image import read_image, write_image
from continuum.model import Capabilities, ExecutionStatus

from continuum_langgraph import GraphFingerprint, LangGraphAdapter


class TestProtocolConformance:
    """spec/adapters.md section 4."""

    def test_satisfies_the_protocol_structurally(self, agent):
        # No inheritance from anything of Continuum's.
        assert isinstance(agent, ContinuumAdapter)
        assert isinstance(agent, RunnableAdapter)
        assert ContinuumAdapter not in type(agent).__mro__

    def test_export_state_is_repeatable(self, midway):
        # The requirement adapters most often get wrong: a timestamp or a uuid
        # minted here makes every checkpoint look like a change.
        assert midway.export_state().digest() == midway.export_state().digest()

    def test_export_state_has_no_side_effects(self, midway):
        before = midway.values
        midway.export_state()
        midway.export_state()
        assert midway.values == before
        assert midway._app.get_state(midway.config).next == ("rank",)

    def test_exported_state_validates(self, midway):
        midway.export_state().validate()

    def test_round_trip_leaves_the_runtime_equivalent(self, midway):
        state = midway.export_state()
        midway.import_state(state)
        assert (
            midway.export_state().execution.cursor["channel_values"]
            == (state.execution.cursor["channel_values"])
        )

    def test_reimport_does_not_duplicate_reducer_channels(self, midway):
        """Regression: importing into a non-empty thread doubled memory.

        ``findings`` is ``Annotated[list, operator.add]``. LangGraph applies the
        reducer on ``update_state``, so writing an accumulated value back into a
        thread that already holds it *appends it to itself*. Every re-import
        silently doubled the agent's memory.

        It hid at first because the interop path imports into a fresh thread,
        where the channel is empty and ``[] + values == values``. Only the
        round-trip conformance check exposed it.
        """
        state = midway.export_state()
        before = list(state.execution.cursor["channel_values"]["findings"])

        for _ in range(3):
            midway.import_state(state)

        after = midway.export_state().execution.cursor["channel_values"]["findings"]
        assert after == before
        assert len(midway.export_state().memory) == len(state.memory)

    def test_namespace_grant_is_accepted(self, midway):
        state = midway.export_state()
        wildcarded = state.replace(
            capabilities=Capabilities(requires=["graph.execute"], granted=["graph.*"])
        )
        fresh = LangGraphAdapter(build_graph, thread_id="w", binding=BINDING)
        fresh.import_state(wildcarded)  # must not raise

    def test_missing_capability_is_refused(self, midway):
        state = midway.export_state()
        stripped = state.replace(capabilities=Capabilities(requires=["graph.execute"], granted=[]))
        fresh = LangGraphAdapter(build_graph, thread_id="x", binding=BINDING)
        with pytest.raises(AdapterError, match="cannot run without"):
            fresh.import_state(stripped)

    def test_import_is_all_or_nothing(self, midway):
        """A refused import must not partially apply."""
        state = midway.export_state()
        fresh = LangGraphAdapter(build_graph, thread_id="y", binding=BINDING)
        before = fresh.values

        with pytest.raises(AdapterError):
            fresh.import_state(
                state.replace(capabilities=Capabilities(requires=["graph.execute"], granted=[]))
            )
        assert fresh.values == before


class TestStepSemantics:
    def test_one_step_advances_one_node(self, agent):
        assert agent.step() is True
        assert agent.values["stage"] == "extract"

    def test_frontier_is_correct_after_a_resumed_step(self, agent):
        """Regression: step() reported completion while work remained.

        The frontier was read while the update stream was still suspended,
        which returns a stale value on the resume path. The first step looked
        right and every later one claimed the graph had finished -- with
        `next` still holding a node. Closing the stream first fixes it.
        """
        assert agent.step() is True  # scan  -> extract pending
        assert agent.step() is True  # extract -> rank pending
        assert agent._app.get_state(agent.config).next == ("rank",)
        assert agent.step() is False  # rank -> done
        assert agent._app.get_state(agent.config).next == ()

    def test_run_to_completion(self, agent):
        final = agent.run_to_completion()
        assert final.execution.status is ExecutionStatus.COMPLETED
        assert agent.values["stage"] == "done"

    def test_stepping_without_start_is_a_clear_error(self):
        bare = LangGraphAdapter(build_graph, thread_id="bare", binding=BINDING)
        with pytest.raises(AdapterError, match="call start"):
            bare.step()

    def test_graph_is_deterministic(self):
        runs = []
        for i in range(2):
            adapter = LangGraphAdapter(build_graph, thread_id=f"d{i}", binding=BINDING)
            adapter.start(dict(INITIAL))
            adapter.run_to_completion()
            runs.append(adapter.values)
        assert runs[0] == runs[1]


class TestStateMapping:
    def test_frontier_becomes_the_current_task(self, midway):
        state = midway.export_state()
        assert state.execution.current_task == "rank"
        assert state.execution.pending_tasks == ["rank"]
        assert state.execution.status is ExecutionStatus.SUSPENDED

    def test_bound_channels_become_portable_memory(self, midway):
        state = midway.export_state()
        contents = [m.content for m in state.memory]
        assert "FIXME insecure default credentials" in contents
        assert all(m.source.startswith("langgraph:findings") for m in state.memory)

    def test_goal_channel_becomes_the_objective(self, midway):
        assert midway.export_state().objective.goal == "review the corpus"

    def test_unbound_channels_are_adapter_only_but_not_lost(self, midway):
        state = midway.export_state()
        cursor = state.execution.cursor
        assert "scratch" in cursor["adapter_only_channels"]
        assert "stage" in cursor["adapter_only_channels"]
        # Captured, just not portable.
        assert cursor["channel_values"]["scratch"] == {"scanned": 2}

    def test_langgraph_identity_is_marked_non_portable(self, midway):
        state = midway.export_state()
        assert state.provider.opaque["thread_id"] == "t1"
        assert state.provider.opaque["checkpoint_id"]
        assert "graph_fingerprint" in state.runtime_opaque

    def test_resume_node_is_the_predecessor_not_the_frontier(self, midway):
        cursor = midway.export_state().execution.cursor
        assert cursor["next"] == ["rank"]
        # update_state(as_node=X) means "as if X just ran", so it must be the
        # predecessor or the graph re-runs the wrong node.
        assert cursor["resume_as_node"] == "extract"

    def test_export_reports_which_fields_survive(self, midway):
        midway.export_state()
        reported = midway.last_export
        assert "findings" in reported.portable
        assert "scratch" in reported.adapter_only
        assert any("checkpoint_id" in f for f in reported.dropped_on_migration)


class TestMigrationBetweenRuntimes:
    def test_resumes_in_a_fresh_runtime_with_a_fresh_checkpointer(self, midway):
        """The actual interoperability claim."""
        state = midway.export_state()

        destination = LangGraphAdapter(
            build_graph, agent_id="lg-reviewer", thread_id="COMPLETELY-DIFFERENT", binding=BINDING
        )
        destination.import_state(state)
        final = destination.run_to_completion()

        assert final.execution.status is ExecutionStatus.COMPLETED
        assert destination.values["stage"] == "done"
        # It continued rather than restarting: scan's output is still present
        # and was not produced twice.
        assert destination.values["findings"].count("auth-service.md") == 1
        assert "ranked: FIXME insecure default credentials" in destination.values["findings"]

    def test_survives_a_trip_through_an_asi_image(self, midway, tmp_path):
        state = midway.export_state()
        image = write_image(state, tmp_path / "lg.asi")
        restored = read_image(image).state
        assert restored.digest() == state.digest()

        destination = LangGraphAdapter(build_graph, thread_id="from-image", binding=BINDING)
        destination.import_state(restored)
        destination.run_to_completion()
        assert destination.values["stage"] == "done"

    def test_checkpoint_identity_does_not_survive(self, midway):
        source = midway.export_state()
        destination = LangGraphAdapter(build_graph, thread_id="new-thread", binding=BINDING)
        destination.import_state(source)

        moved = destination.export_state()
        assert moved.provider.opaque["thread_id"] == "new-thread"
        assert moved.provider.opaque["checkpoint_id"] != source.provider.opaque["checkpoint_id"]

    def test_topology_mismatch_is_refused(self, midway):
        """The graph is not in the image, so the mismatch must be detectable."""
        state = midway.export_state()
        wrong = LangGraphAdapter(build_different_graph, thread_id="wrong", binding=BINDING)

        with pytest.raises(AdapterError, match="graph topology mismatch"):
            wrong.import_state(state)

    def test_state_from_another_adapter_is_refused_with_an_explanation(self, midway):
        from continuum.model import AgentState, Identity

        foreign = AgentState(
            identity=Identity(agent_id="native"),
            capabilities=Capabilities(requires=["graph.execute"], granted=GRANTS),
        )
        adapter = LangGraphAdapter(build_graph, thread_id="foreign", binding=BINDING)

        with pytest.raises(AdapterError, match="no LangGraph channel values"):
            adapter.import_state(foreign)


class TestFingerprint:
    def test_identical_graphs_match(self):
        a = LangGraphAdapter(build_graph, thread_id="a", binding=BINDING)
        b = LangGraphAdapter(build_graph, thread_id="b", binding=BINDING)
        assert GraphFingerprint.of(a._app).differences(GraphFingerprint.of(b._app)) == []

    def test_different_graphs_report_specific_differences(self):
        a = LangGraphAdapter(build_graph, thread_id="a", binding=BINDING)
        b = LangGraphAdapter(build_different_graph, thread_id="b", binding=BINDING)
        problems = GraphFingerprint.of(a._app).differences(GraphFingerprint.of(b._app))
        assert any("extract" in p for p in problems)

    def test_fingerprint_survives_serialization(self):
        adapter = LangGraphAdapter(build_graph, thread_id="s", binding=BINDING)
        original = GraphFingerprint.of(adapter._app)
        assert GraphFingerprint.from_dict(original.to_dict()) == original


class TestDiscovery:
    def test_registered_through_the_entry_point(self):
        """Published from its own package, with no change to Continuum core."""
        from continuum.adapters import available, get

        assert "langgraph" in available()
        assert get("langgraph") is not None
