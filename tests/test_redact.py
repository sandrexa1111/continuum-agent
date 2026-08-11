"""Secret detection.

The credentials below are syntactically valid but fabricated -- they exist to
exercise the matchers and are not real. Each rule gets a positive case and the
suite as a whole gets negative cases, because a scanner that flags everything
is as useless as one that flags nothing.
"""

from __future__ import annotations

import pytest

from continuum.model import MemoryEntry, MemoryKind, Message
from continuum.redact import (
    RedactionReport,
    placeholder_for,
    sanitize_state,
    scan_text,
    shannon_entropy,
)


def _fabricate(prefix: str, body: str) -> str:
    """Assemble a fabricated credential at runtime.

    The prefixes are split across concatenations so that no complete,
    token-shaped literal appears anywhere in this file. Repository secret
    scanners -- GitHub push protection among them -- match on literal shapes and
    will block a push containing one, even when the value is obviously fake.

    Suppressing the scanner with an allowlist entry would be the wrong fix: the
    right habit is that a credential shape never appears verbatim in source. The
    runtime value is identical, so the matchers are exercised exactly as before.
    """
    return prefix + body


FAKE_SECRETS = {
    "aws-access-key-id": _fabricate("AK" + "IA", "IOSFODNN7EXAMPLE"),
    "github-token": _fabricate("ghp" + "_", "A1b2C3d4E5" * 3 + "F6g7H8"),
    "openai-style-key": _fabricate("sk" + "-", "abcdefghij" * 3),
    "anthropic-style-key": _fabricate("sk" + "-ant-", "abcdefghij" * 3),
    "slack-token": _fabricate("xox" + "b-", "123456789012-abcdefghijklmno"),
    "google-api-key": _fabricate("AI" + "za", "b" * 35),
    "jwt": _fabricate(
        "ey" + "JhbGciOiJIUzI1NiJ9.",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ),
}


class TestPatternRules:
    @pytest.mark.parametrize("rule,secret", sorted(FAKE_SECRETS.items()))
    def test_each_rule_matches_its_shape(self, rule, secret):
        cleaned, findings = scan_text(f"the value is {secret} ok", "test")
        assert secret not in cleaned
        assert rule in {f.rule for f in findings}

    def test_credentialed_url_redacts_only_the_password(self):
        cleaned, findings = scan_text(
            "connecting to postgres://svc:hunter2pass@db.internal:5432/billing", "test"
        )
        assert "hunter2pass" not in cleaned
        assert "postgres://svc:" in cleaned  # host and user stay readable
        assert "db.internal" in cleaned
        assert findings[0].rule == "credentialed-url"

    def test_assigned_secret_keeps_the_key_visible(self):
        cleaned, _ = scan_text('api_key = "s3cr3t-value-here"', "test")
        assert "api_key" in cleaned
        assert "s3cr3t-value-here" not in cleaned

    def test_private_key_block_is_removed_whole(self):
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA1234567890\nabcdef\n"
            "-----END RSA PRIVATE KEY-----"
        )
        cleaned, findings = scan_text(f"key:\n{block}\ndone", "test")
        assert "MIIEpAIBAAKCAQEA" not in cleaned
        assert findings[0].rule == "private-key-block"

    def test_multiple_secrets_in_one_field_are_all_found(self):
        text = f"{FAKE_SECRETS['github-token']} and {FAKE_SECRETS['aws-access-key-id']}"
        cleaned, findings = scan_text(text, "test")
        assert len(findings) == 2
        assert not any(s in cleaned for s in FAKE_SECRETS.values())

    def test_placeholders_are_not_themselves_redacted(self):
        """Regression: rule-by-rule substitution re-matched its own output.

        A placeholder like ``[REDACTED:github-token:440bb94834]`` contains the
        literal text ``token:440bb94834``, which the generic assignment rule
        happily matched -- yielding nested placeholders and a phantom third
        finding for a field containing two secrets.
        """
        text = f"{FAKE_SECRETS['github-token']} and {FAKE_SECRETS['aws-access-key-id']}"
        cleaned, findings = scan_text(text, "test")

        assert cleaned.count("[REDACTED:") == 2
        assert "REDACTED:assigned-secret" not in cleaned
        assert {f.rule for f in findings} == {"github-token", "aws-access-key-id"}

    def test_specific_rules_win_over_generic_ones(self):
        cleaned, findings = scan_text(f"key={FAKE_SECRETS['anthropic-style-key']}", "test")
        assert {f.rule for f in findings} == {"anthropic-style-key"}
        assert FAKE_SECRETS["anthropic-style-key"] not in cleaned


class TestFalsePositives:
    @pytest.mark.parametrize(
        "text",
        [
            "the invoice total is 4471.20 and settles net-30",
            "see https://example.com/docs/getting-started for details",
            "commit sha256:abc123 touched src/parser.py:84",
            "def mint_session(user_id: str) -> Session: ...",
            "TODO refactor the parser once the schema settles",
        ],
    )
    def test_ordinary_text_is_left_alone(self, text):
        cleaned, findings = scan_text(text, "test")
        assert cleaned == text
        assert findings == []

    def test_entropy_rule_is_off_by_default(self):
        # Hashes and base64 blobs are common in agent context; flagging them by
        # default would make the scanner noise.
        digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        cleaned, findings = scan_text(f"digest {digest}", "test")
        assert cleaned == f"digest {digest}"
        assert findings == []

    def test_entropy_rule_fires_when_requested(self):
        digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        cleaned, findings = scan_text(f"digest {digest}", "test", aggressive=True)
        assert digest not in cleaned
        assert findings[0].confidence == "low"

    def test_low_entropy_long_strings_are_not_flagged(self):
        repetitive = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        cleaned, _ = scan_text(repetitive, "test", aggressive=True)
        assert cleaned == repetitive


class TestPlaceholders:
    def test_the_same_secret_always_yields_the_same_placeholder(self):
        # Stability keeps a sanitized history diffable.
        assert placeholder_for("abc", "rule") == placeholder_for("abc", "rule")

    def test_different_secrets_yield_different_placeholders(self):
        assert placeholder_for("abc", "rule") != placeholder_for("abd", "rule")

    def test_placeholder_does_not_leak_the_secret(self):
        secret = FAKE_SECRETS["github-token"]
        token = placeholder_for(secret, "github-token")
        assert secret not in token
        assert token.startswith("[REDACTED:github-token:")

    def test_entropy_helper(self):
        assert shannon_entropy("") == 0.0
        assert shannon_entropy("aaaa") == 0.0
        assert shannon_entropy("abcd") == 2.0


class TestSanitizeState:
    def _leaky(self, state):
        return state.replace(
            memory=[
                *state.memory,
                MemoryEntry(
                    id="leak",
                    kind=MemoryKind.WORKING,
                    content=f"retrying with {FAKE_SECRETS['github-token']}",
                ),
            ],
            context=[
                *state.context,
                Message(role="user", content=f"use {FAKE_SECRETS['aws-access-key-id']}"),
            ],
        )

    def test_secrets_are_removed_from_memory_and_context(self, state):
        sanitized, report = sanitize_state(self._leaky(state))

        assert not report.clean
        assert len(report.findings) == 2
        blob = "".join(m.content for m in sanitized.memory)
        blob += "".join(m.content for m in sanitized.context)
        assert not any(secret in blob for secret in FAKE_SECRETS.values())

    def test_clean_state_reports_clean_and_is_unchanged(self, state):
        sanitized, report = sanitize_state(state)
        assert report.clean
        assert sanitized.digest() == state.digest()

    def test_provider_params_are_scanned(self, state):
        leaky = state.replace(
            provider=state.provider.__class__(
                adapter="a", params={"headers": {"authorization": FAKE_SECRETS["jwt"]}}
            )
        )
        sanitized, report = sanitize_state(leaky)
        assert not report.clean
        assert FAKE_SECRETS["jwt"] not in str(sanitized.provider.params)

    def test_nested_structures_are_walked(self, state):
        leaky = state.replace(
            execution=state.execution.__class__(
                cursor={"retries": [{"token": FAKE_SECRETS["slack-token"]}]}
            )
        )
        sanitized, report = sanitize_state(leaky)
        assert not report.clean
        assert FAKE_SECRETS["slack-token"] not in str(sanitized.execution.cursor)

    def test_dropping_a_memory_kind_removes_those_entries(self, state):
        sanitized, report = sanitize_state(state, drop_memory_kinds=["episodic"])
        assert all(m.kind.value != "episodic" for m in sanitized.memory)
        assert "dropped-memory-kind" in {f.rule for f in report.findings}

    def test_stripping_runtime_opaque(self, state):
        opaque = state.replace(runtime_opaque={"scratch": "value"})
        sanitized, report = sanitize_state(opaque, strip_runtime_opaque=True)
        assert sanitized.runtime_opaque == {}
        assert "stripped-runtime-opaque" in {f.rule for f in report.findings}

    def test_sanitizing_records_an_event(self, state):
        sanitized, _ = sanitize_state(self._leaky(state))
        assert sanitized.events[-1].type == "state.sanitized"

    def test_result_remains_a_valid_state(self, state):
        sanitized, _ = sanitize_state(self._leaky(state))
        sanitized.validate()

    def test_report_serializes(self, state):
        _, report = sanitize_state(self._leaky(state))
        payload = report.to_dict()
        assert payload["clean"] is False
        assert payload["findings"][0]["location"].startswith("memory[")

    def test_empty_report_renders_cleanly(self):
        assert "CLEAN" in RedactionReport(scanned_fields=3).render()
