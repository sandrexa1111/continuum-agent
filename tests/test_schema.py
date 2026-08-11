"""The published JSON Schema must agree with what the implementation emits.

A schema in ``spec/`` that nobody validates against is documentation pretending
to be a contract. These tests point the real schema at real states produced by
the real adapter, and check that it rejects the documents the spec says are
invalid.

The schema is written by hand against ``spec/state-image.md`` rather than
reflected off the dataclasses -- reflection would make it agree with the code by
construction, including where the code is wrong, and the disagreement is the
whole point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.model import (
    AgentState,
    Artifact,
    Capabilities,
    Identity,
    MemoryEntry,
    MemoryKind,
)

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema is a dev-only dependency")

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "spec" / "agent-state.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(schema):
    return jsonschema.Draft202012Validator(schema)


def test_schema_is_published(schema):
    assert schema["$schema"].endswith("2020-12/schema")
    assert "Continuum Agent State" in schema["title"]


def test_schema_is_itself_valid(schema):
    jsonschema.Draft202012Validator.check_schema(schema)


def test_schema_is_up_to_date():
    """Regenerating must be a no-op, or the published schema has drifted."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/generate_schema.py", "--check"],
        cwd=SCHEMA_PATH.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class TestRealStatesValidate:
    def test_fixture_state(self, validator, state):
        validator.validate(state.to_dict())

    def test_reference_adapter_output(self, validator, workspace):
        from continuum.adapters.native import NativeReviewAgent

        final = NativeReviewAgent(workspace).run_to_completion()
        validator.validate(final.to_dict())

    def test_minimal_state(self, validator):
        validator.validate(AgentState(identity=Identity(agent_id="a")).to_dict())

    def test_state_carrying_unknown_fields(self, validator, state):
        """Forward compatibility: extra top-level fields must still validate.

        A schema that forbade them would make every older validator reject
        documents that spec/compatibility.md requires readers to round-trip.
        """
        document = state.to_dict()
        document["written_by_a_future_version"] = {"anything": [1, 2]}
        validator.validate(document)


class TestSchemaRejectsInvalidDocuments:
    def _invalid(self, validator, document) -> bool:
        return not validator.is_valid(document)

    def test_missing_format_version(self, validator):
        assert self._invalid(validator, {"identity": {"agent_id": "a"}})

    def test_missing_identity(self, validator):
        assert self._invalid(validator, {"format_version": "0.1"})

    def test_malformed_agent_id(self, validator):
        assert self._invalid(
            validator, {"format_version": "0.1", "identity": {"agent_id": "has spaces"}}
        )

    def test_unknown_memory_kind(self, validator, state):
        document = state.to_dict()
        document["memory"][0]["kind"] = "telepathic"
        assert self._invalid(validator, document)

    def test_importance_out_of_range(self, validator, state):
        document = state.to_dict()
        document["memory"][0]["importance"] = 4.2
        assert self._invalid(validator, document)

    def test_negative_step(self, validator, state):
        document = state.to_dict()
        document["execution"]["step"] = -1
        assert self._invalid(validator, document)

    def test_unknown_execution_status(self, validator, state):
        document = state.to_dict()
        document["execution"]["status"] = "vibing"
        assert self._invalid(validator, document)

    def test_absolute_artifact_path(self, validator, state):
        document = state.to_dict()
        document["artifacts"][0]["path"] = "/etc/passwd"
        assert self._invalid(validator, document)

    def test_traversing_artifact_path(self, validator, state):
        document = state.to_dict()
        document["artifacts"][0]["path"] = "../../secrets.txt"
        assert self._invalid(validator, document)

    def test_malformed_digest(self, validator, state):
        document = state.to_dict()
        document["artifacts"][0]["digest"] = "md5:whatever"
        assert self._invalid(validator, document)

    def test_wildcard_in_a_requirement(self, validator):
        document = {
            "format_version": "0.1",
            "identity": {"agent_id": "a"},
            "capabilities": {"requires": ["filesystem.*"]},
        }
        assert self._invalid(validator, document)

    def test_wildcard_in_a_grant_is_allowed(self, validator):
        document = {
            "format_version": "0.1",
            "identity": {"agent_id": "a"},
            "capabilities": {"granted": ["filesystem.*", "*"]},
        }
        assert validator.is_valid(document)


def test_schema_covers_every_section_the_model_emits(schema, state):
    """A section the model writes but the schema never mentions is a gap."""
    rich = state.replace(
        memory=[*state.memory, MemoryEntry(id="m9", kind=MemoryKind.WORKING, content="x")],
        artifacts=[*state.artifacts, Artifact(id="extra", path="a/b.md")],
        capabilities=Capabilities(requires=["a.read"], optional=["b.read"], granted=["a.*"]),
    )
    emitted = set(rich.to_dict())
    described = set(schema["properties"])
    assert emitted <= described, f"schema does not describe: {sorted(emitted - described)}"
