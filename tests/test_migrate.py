"""Migration, capability checking, and context compaction.

The theme throughout: loss must be reported, never silent.
"""

from __future__ import annotations

import pytest

from continuum.capabilities import Verdict, check_capabilities, satisfies
from continuum.compaction import compact_context, estimate_tokens, structural_summary
from continuum.errors import ContinuumError, FormatError
from continuum.migrate import (
    ROLES_CHAT_ONLY,
    ROLES_WITH_TOOLS,
    MigrationTarget,
    Portability,
    migrate,
)
from continuum.model import Capabilities, Message


def sections(report, level):
    return {f.section for f in report.by_portability(level)}


class TestCapabilities:
    def test_all_granted_passes(self, state):
        report = check_capabilities(state, ["filesystem.read", "ledger.read", "web.search"])
        assert report.verdict is Verdict.PASS
        assert report.ok

    def test_missing_optional_is_partial_not_blocked(self, state):
        report = check_capabilities(state, ["filesystem.read", "ledger.read"])
        assert report.verdict is Verdict.PARTIAL
        assert report.ok
        assert report.missing_optional == ["web.search"]

    def test_missing_required_blocks(self, state):
        report = check_capabilities(state, ["filesystem.read"])
        assert report.verdict is Verdict.BLOCKED
        assert not report.ok
        assert report.missing_required == ["ledger.read"]

    def test_namespace_wildcard_grant(self, state):
        report = check_capabilities(state, ["filesystem.*", "ledger.*", "web.*"])
        assert report.verdict is Verdict.PASS

    def test_global_wildcard_grant(self, state):
        assert check_capabilities(state, ["*"]).verdict is Verdict.PASS

    def test_wildcard_does_not_cross_namespaces(self, state):
        report = check_capabilities(state, ["filesystem.*"])
        assert "ledger.read" in report.missing_required

    def test_unused_grants_are_reported(self, state):
        report = check_capabilities(state, ["filesystem.read", "ledger.read", "unused.thing"])
        assert "unused.thing" in report.unused_grants

    def test_wildcard_requirement_is_refused(self, state):
        # "I need something under filesystem" is not a checkable claim.
        broken = state.replace(capabilities=Capabilities(requires=["filesystem.*"]))
        with pytest.raises(FormatError, match="wildcard"):
            check_capabilities(broken, ["filesystem.read"])

    def test_satisfies_helper_matches_the_report(self, state):
        assert satisfies("filesystem.read", ["filesystem.*"])
        assert not satisfies("ledger.read", ["filesystem.*"])


class TestCompaction:
    def _chatty(self, state, count=12):
        messages = [state.context[0]]  # keep the pinned system prompt
        messages += [
            Message(role="user" if i % 2 else "assistant", content=f"turn {i} " + "detail " * 12)
            for i in range(count)
        ]
        return state.replace(context=messages)

    def test_within_budget_is_a_no_op(self, state):
        result = compact_context(state, max_tokens=10_000)
        assert result.lossless
        assert result.state.digest() == state.digest()

    def test_over_budget_drops_and_summarizes(self, state):
        chatty = self._chatty(state)
        result = compact_context(chatty, max_tokens=200)

        assert not result.lossless
        assert result.dropped > 0
        assert result.tokens_after <= 200
        assert len(result.state.context) == result.kept

    def test_summary_is_marked_as_transformed(self, state):
        result = compact_context(self._chatty(state), max_tokens=200)
        summary = next(m for m in result.state.memory if m.id == result.summary_memory_id)

        # A reader must always be able to tell a summary from an observation.
        assert summary.transformed is True
        assert summary.source == "continuum.compaction"

    def test_pinned_and_system_messages_are_never_dropped(self, state):
        result = compact_context(self._chatty(state), max_tokens=200)
        roles = [m.role for m in result.state.context]
        assert "system" in roles
        assert any(m.pinned for m in result.state.context)

    def test_most_recent_messages_are_kept(self, state):
        chatty = self._chatty(state)
        result = compact_context(chatty, max_tokens=200, keep_recent=3)
        assert result.state.context[-3:] == chatty.context[-3:]

    def test_impossible_budget_raises_rather_than_truncating_instructions(self, state):
        with pytest.raises(ContinuumError, match="cannot compact below"):
            compact_context(self._chatty(state), max_tokens=5)

    def test_is_deterministic(self, state):
        chatty = self._chatty(state)
        first = compact_context(chatty, max_tokens=200)
        second = compact_context(chatty, max_tokens=200)
        # Timestamps on the summary entry differ, so compare the text itself.
        assert [m.content for m in first.state.context] == [m.content for m in second.state.context]

    def test_structural_summary_invents_nothing(self):
        messages = [
            Message(role="user", content="first message"),
            Message(role="assistant", content="second message"),
        ]
        summary = structural_summary(messages)
        assert "2 earlier messages" in summary
        assert "not recoverable" in summary

    def test_custom_token_counter_is_used(self, state):
        calls = []

        def counter(text: str) -> int:
            calls.append(text)
            return 1

        compact_context(self._chatty(state), max_tokens=3, token_counter=counter)
        assert calls

    def test_estimator_is_pessimistic(self):
        # Under-estimating the budget is recoverable; over-estimating means the
        # destination rejects the request.
        assert estimate_tokens("a" * 300) >= 100

    def test_zero_budget_is_a_usage_error(self, state):
        with pytest.raises(ValueError):
            compact_context(state, max_tokens=0)


class TestMigration:
    def _target(self, **kwargs):
        base = {
            "adapter": "other-runtime",
            "provider": "another-provider",
            "model": "small-model",
            "granted_capabilities": ["filesystem.read", "ledger.read", "web.search"],
        }
        base.update(kwargs)
        return MigrationTarget(**base)

    def test_durable_sections_are_reported_portable(self, state):
        report = migrate(state, self._target()).report
        portable = sections(report, Portability.PORTABLE)
        assert {"identity", "objective", "execution", "memory", "artifacts", "events"} <= portable

    def test_memory_and_artifacts_survive_intact(self, state):
        result = migrate(state, self._target())
        assert result.state.memory == state.memory
        assert result.state.artifacts == state.artifacts

    def test_cross_provider_context_is_translated_not_portable(self, state):
        report = migrate(state, self._target()).report
        assert "context" in sections(report, Portability.TRANSLATED)

    def test_same_provider_context_stays_portable(self, state):
        report = migrate(state, self._target(provider="mock", adapter="native-reviewer")).report
        assert "context" in sections(report, Portability.PORTABLE)

    def test_provider_opaque_state_is_dropped_and_named(self, state):
        result = migrate(state, self._target())
        assert "provider.opaque" in sections(result.report, Portability.UNAVAILABLE)
        assert result.state.provider.opaque == {}

        detail = next(f.detail for f in result.report.findings if f.section == "provider.opaque")
        assert "thread_id" in detail  # named, not merely counted

    def test_provider_opaque_survives_a_same_provider_move(self, state):
        result = migrate(state, self._target(provider="mock", adapter="native-reviewer"))
        assert result.state.provider.opaque == state.provider.opaque

    def test_unsupported_roles_are_remapped_and_recorded(self, state):
        with_tool = state.replace(
            context=[*state.context, Message(role="tool", content="ledger returned 42")]
        )
        result = migrate(with_tool, self._target(roles=ROLES_CHAT_ONLY))

        moved = result.state.context[-1]
        assert moved.role == "user"
        assert moved.metadata["continuum.original_role"] == "tool"
        assert moved.content.startswith("[tool]")

    def test_supported_roles_are_left_alone(self, state):
        with_tool = state.replace(context=[*state.context, Message(role="tool", content="ok")])
        result = migrate(with_tool, self._target(roles=ROLES_WITH_TOOLS))
        assert result.state.context[-1].role == "tool"

    def test_missing_capability_marks_the_report_blocked(self, state):
        result = migrate(state, self._target(granted_capabilities=["filesystem.read"]))
        assert result.report.blocked
        assert not result.ok

    def test_a_blocked_migration_still_returns_a_state(self, state):
        # Callers running a compatibility matrix want a row, not a traceback.
        result = migrate(state, self._target(granted_capabilities=[]))
        assert result.report.blocked
        assert result.state is not None

    def test_impossible_context_budget_is_reported_not_raised(self, state):
        result = migrate(state, self._target(max_context_tokens=1))
        assert result.report.blocked
        assert "context.budget" in result.report.fatal
        assert "context.budget" in sections(result.report, Portability.UNAVAILABLE)

    def test_sufficient_context_budget_is_portable(self, state):
        report = migrate(state, self._target(max_context_tokens=100_000)).report
        assert "context.budget" in sections(report, Portability.PORTABLE)

    def test_migration_appends_an_event(self, state):
        result = migrate(state, self._target())
        last = result.state.events[-1]
        assert last.type == "migration.completed"
        assert "provider.opaque" in last.data["dropped_sections"]

    def test_target_grants_replace_source_grants(self, state):
        result = migrate(state, self._target(granted_capabilities=["*"]))
        assert result.state.capabilities.granted == ["*"]

    def test_report_serializes_for_machine_consumption(self, state):
        payload = migrate(state, self._target()).report.to_dict()
        assert payload["blocked"] is False
        assert {f["section"] for f in payload["findings"]}
        assert payload["capabilities"]["verdict"] in {v.value for v in Verdict}

    def test_lossless_only_when_nothing_was_translated(self, state):
        assert not migrate(state, self._target()).report.lossless
