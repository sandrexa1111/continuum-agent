"""State model: serialization round trips, validation, and version handling."""

from __future__ import annotations

import pytest

from continuum.errors import FormatError, VersionError
from continuum.model import (
    FORMAT_VERSION,
    AgentState,
    Artifact,
    Capabilities,
    Event,
    Identity,
    MemoryEntry,
    MemoryKind,
    check_version,
)


class TestRoundTrip:
    def test_serialization_round_trip_preserves_digest(self, state):
        restored = AgentState.from_dict(state.to_dict())
        assert restored.digest() == state.digest()

    def test_round_trip_preserves_every_section(self, state):
        restored = AgentState.from_dict(state.to_dict())
        assert restored.identity == state.identity
        assert restored.objective == state.objective
        assert restored.execution == state.execution
        assert restored.provider == state.provider
        assert restored.memory == state.memory
        assert restored.context == state.context
        assert restored.capabilities == state.capabilities
        assert restored.artifacts == state.artifacts
        assert restored.events == state.events

    def test_repeated_round_trips_are_a_fixed_point(self, state):
        once = AgentState.from_dict(state.to_dict())
        twice = AgentState.from_dict(once.to_dict())
        assert once.digest() == twice.digest() == state.digest()

    def test_empty_optionals_do_not_change_the_address(self):
        # A state built with explicit empty lists must address identically to
        # one built without them, or dedup fails on trivially-equal states.
        bare = AgentState(identity=Identity(agent_id="a"))
        explicit = AgentState(identity=Identity(agent_id="a"), memory=[], artifacts=[])
        assert bare.digest() == explicit.digest()


class TestForwardCompatibility:
    def test_unknown_top_level_fields_survive_a_round_trip(self, state):
        document = state.to_dict()
        document["future_section"] = {"written_by": "continuum-2.0", "value": [1, 2]}

        restored = AgentState.from_dict(document)
        assert restored.extensions["future_section"]["written_by"] == "continuum-2.0"
        assert restored.to_dict()["future_section"] == document["future_section"]

    def test_an_old_reader_does_not_destroy_new_data(self, state):
        """The whole point: read by an older tool, written back, still intact."""
        document = state.to_dict()
        document["experimental_budget"] = {"tokens": 500}
        rewritten = AgentState.from_dict(document).to_dict()
        assert rewritten["experimental_budget"] == {"tokens": 500}


class TestVersioning:
    def test_current_version_is_accepted(self):
        check_version(FORMAT_VERSION)

    def test_older_minor_is_accepted(self):
        check_version("0.0")

    def test_newer_minor_is_refused(self):
        with pytest.raises(VersionError) as exc:
            check_version("0.99")
        assert exc.value.found == "0.99"

    def test_newer_major_is_refused(self):
        with pytest.raises(VersionError):
            check_version("1.0")

    def test_malformed_version_is_a_format_error(self):
        with pytest.raises(FormatError):
            check_version("not-a-version")

    def test_missing_version_is_refused(self, state):
        document = state.to_dict()
        del document["format_version"]
        with pytest.raises(FormatError, match="missing a format_version"):
            AgentState.from_dict(document)


class TestValidation:
    def test_duplicate_memory_ids_are_refused(self, state):
        duplicate = MemoryEntry(id="mem-1", kind=MemoryKind.WORKING, content="clash")
        with pytest.raises(FormatError, match="duplicate memory entry id"):
            state.replace(memory=[*state.memory, duplicate])

    def test_duplicate_artifact_ids_are_refused(self, state):
        clash = Artifact(id="research", path="other.md")
        with pytest.raises(FormatError, match="duplicate artifact id"):
            state.replace(artifacts=[*state.artifacts, clash])

    def test_artifact_derived_from_unknown_parent_is_refused(self, state):
        orphan = Artifact(id="orphan", path="o.md", derived_from=["ghost"])
        with pytest.raises(FormatError, match="unknown artifact"):
            state.replace(artifacts=[*state.artifacts, orphan])

    def test_artifact_cycles_are_refused(self):
        # A cyclic provenance graph makes "where did this come from" unanswerable.
        cyclic = [
            Artifact(id="a", path="a.md", derived_from=["b"]),
            Artifact(id="b", path="b.md", derived_from=["a"]),
        ]
        with pytest.raises(FormatError, match="cycle"):
            AgentState(identity=Identity(agent_id="x"), artifacts=cyclic).validate()

    def test_self_referential_artifact_is_refused(self):
        loop = [Artifact(id="a", path="a.md", derived_from=["a"])]
        with pytest.raises(FormatError, match="cycle"):
            AgentState(identity=Identity(agent_id="x"), artifacts=loop).validate()

    def test_deep_artifact_chain_is_allowed(self):
        chain = [Artifact(id="a0", path="a0.md")]
        chain += [
            Artifact(id=f"a{i}", path=f"a{i}.md", derived_from=[f"a{i - 1}"]) for i in range(1, 40)
        ]
        AgentState(identity=Identity(agent_id="x"), artifacts=chain).validate()

    def test_out_of_order_events_are_refused(self, state):
        with pytest.raises(FormatError, match="ascending seq"):
            state.replace(events=[Event(seq=5, ts="", type="a"), Event(seq=2, ts="", type="b")])

    def test_capability_declared_both_required_and_optional_is_refused(self, state):
        with pytest.raises(FormatError, match="both required and optional"):
            state.replace(capabilities=Capabilities(requires=["a.read"], optional=["a.read"]))

    def test_importance_outside_range_is_refused(self):
        with pytest.raises(FormatError, match="within"):
            MemoryEntry.from_dict({"id": "m", "kind": "working", "content": "x", "importance": 1.5})

    def test_negative_step_is_refused(self, state):
        document = state.to_dict()
        document["execution"]["step"] = -1
        with pytest.raises(FormatError, match="non-negative"):
            AgentState.from_dict(document)

    def test_unknown_memory_kind_is_refused(self):
        with pytest.raises(FormatError, match=r"memory\.kind"):
            MemoryEntry.from_dict({"id": "m", "kind": "telepathic", "content": "x"})

    def test_invalid_agent_id_is_refused(self):
        with pytest.raises(FormatError, match=r"identity\.agent_id"):
            Identity.from_dict({"agent_id": "has spaces/and-slashes"})


class TestDigests:
    def test_core_digest_ignores_lineage(self, state):
        from continuum.model import Lineage

        moved = state.replace(lineage=Lineage(parent="sha256:" + "c" * 64, generation=4))
        assert moved.core_digest() == state.core_digest()
        assert moved.digest() != state.digest()

    def test_core_digest_still_tracks_real_change(self, state):
        changed = state.replace(objective=state.objective.__class__(goal="something else"))
        assert changed.core_digest() != state.core_digest()

    def test_events_append_monotonically(self, state):
        updated = state.with_event("tool.called", {"tool": "x"}, ts="2026-01-01T00:00:01Z")
        assert updated.events[-1].seq == state.events[-1].seq + 1
        assert updated.events[-1].type == "tool.called"
