"""Structured diff between states."""

from __future__ import annotations

from continuum.diff import diff_states
from continuum.model import Artifact, Capabilities, MemoryEntry, MemoryKind, Message


class TestEmptyDiff:
    def test_identical_states_produce_an_empty_diff(self, state):
        result = diff_states(state, state)
        assert result.empty
        assert "identical state" in result.render()

    def test_lineage_alone_is_not_reported_as_a_change(self, state):
        from continuum.model import Lineage

        moved = state.replace(lineage=Lineage(parent="sha256:" + "a" * 64, generation=2))
        # Lineage describes position in the graph, not what the agent did.
        assert diff_states(state, moved).empty


class TestScalarSections:
    def test_objective_change_is_reported(self, state):
        changed = state.replace(objective=state.objective.__class__(goal="new goal"))
        paths = {c.path for c in diff_states(state, changed).objective}
        assert "objective.goal" in paths

    def test_execution_progress_is_reported(self, state):
        advanced = state.replace(
            execution=state.execution.__class__(current_task="implementation", step=4)
        )
        result = diff_states(state, advanced)
        paths = {c.path for c in result.execution}
        assert {"execution.current_task", "execution.step"} <= paths
        assert "research_market" in result.render()

    def test_provider_change_is_reported(self, state):
        moved = state.replace(
            provider=state.provider.__class__(adapter="a", provider="p2", model="m2")
        )
        assert {c.path for c in diff_states(state, moved).provider} >= {
            "provider.model",
            "provider.provider",
        }

    def test_provider_opaque_is_excluded(self, state):
        # Opaque handles churn every turn and are unreadable by design; showing
        # them would make every diff noisy and none of it actionable.
        churned = state.replace(
            provider=state.provider.__class__(
                adapter=state.provider.adapter,
                provider=state.provider.provider,
                model=state.provider.model,
                params=state.provider.params,
                opaque={"thread_id": "completely-different"},
            )
        )
        assert diff_states(state, churned).empty


class TestCollections:
    def test_added_memory_is_reported(self, state):
        added = state.replace(
            memory=[
                *state.memory,
                MemoryEntry(id="mem-3", kind=MemoryKind.WORKING, content="new"),
            ]
        )
        assert diff_states(state, added).memory.added == ["mem-3"]

    def test_removed_memory_is_reported(self, state):
        trimmed = state.replace(memory=state.memory[:1])
        assert diff_states(state, trimmed).memory.removed == ["mem-2"]

    def test_modified_memory_is_reported_by_content(self, state):
        edited = list(state.memory)
        edited[0] = MemoryEntry(
            id="mem-1", kind=MemoryKind.EPISODIC, content="revised finding", importance=0.7
        )
        result = diff_states(state, state.replace(memory=edited))
        assert result.memory.modified == ["mem-1"]

    def test_artifact_content_change_is_detected_by_digest(self, state):
        edited = [
            Artifact(id="research", path="artifacts/research.md", digest="sha256:" + "f" * 64),
            state.artifacts[1],
        ]
        result = diff_states(state, state.replace(artifacts=edited))
        assert result.artifacts.modified == ["research"]
        assert "contents changed" in result.render()

    def test_new_artifact_is_reported(self, state):
        added = [*state.artifacts, Artifact(id="proposal", path="artifacts/proposal.md")]
        assert diff_states(state, state.replace(artifacts=added)).artifacts.added == ["proposal"]

    def test_capability_grants_are_diffed_as_sets(self, state):
        widened = state.replace(
            capabilities=Capabilities(
                requires=[*state.capabilities.requires, "github.write"],
                optional=state.capabilities.optional,
            )
        )
        result = diff_states(state, widened)
        assert result.capabilities.added == ["github.write"]
        assert "+ github.write" in result.render()

    def test_capability_removal_is_reported(self, state):
        narrowed = state.replace(
            capabilities=Capabilities(requires=["filesystem.read"], optional=["web.search"])
        )
        assert diff_states(state, narrowed).capabilities.removed == ["ledger.read"]


class TestContextAndEvents:
    def test_context_growth_is_summarized_not_dumped(self, state):
        chatty = state.replace(
            context=[*state.context, Message(role="user", content="another turn")]
        )
        result = diff_states(state, chatty)
        assert result.context_delta == 1
        assert "+1 messages" in result.render()

    def test_context_shrink_is_reported(self, state):
        trimmed = state.replace(context=state.context[:1])
        assert diff_states(state, trimmed).context_delta == -2

    def test_new_events_are_listed_by_sequence(self, state):
        advanced = state.with_event("artifact.created", {"id": "report"})
        result = diff_states(state, advanced)
        assert len(result.events_added) == 1
        assert "artifact.created" in result.events_added[0]

    def test_repeated_event_types_are_not_collapsed(self, state):
        advanced = state
        for _ in range(3):
            advanced = advanced.with_event("tool.called", {"tool": "same"})
        assert len(diff_states(state, advanced).events_added) == 3


class TestSerialization:
    def test_diff_serializes_for_machine_consumption(self, state):
        changed = state.replace(objective=state.objective.__class__(goal="new"))
        payload = diff_states(state, changed).to_dict()
        assert payload["left"] == state.digest()
        assert payload["right"] == changed.digest()
        assert "objective.goal" in {c["path"] for c in payload["objective"]}

    def test_render_is_stable(self, state):
        changed = state.replace(objective=state.objective.__class__(goal="new"))
        assert diff_states(state, changed).render() == diff_states(state, changed).render()
