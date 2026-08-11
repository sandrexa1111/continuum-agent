"""Secret detection and redaction for state images.

A checkpoint captures whatever the agent was holding, and agents hold
credentials: a token pasted into context, an API key echoed by a failing tool,
a connection string in a memory entry. Images get copied between machines and
attached to bug reports, so "export the agent's state" must not quietly mean
"export the agent's secrets".

Two detectors, deliberately kept apart:

**Pattern rules** match credentials with recognizable shapes. High precision --
these fire on real secrets and little else, so they are on by default.

**Entropy heuristic** flags long, random-looking strings with no recognizable
shape. Lower precision: it also matches hashes, base64 payloads, and UUID-ish
identifiers. Off by default, available as ``--aggressive``.

Redaction is *stable*: the same secret always yields the same placeholder, via
a truncated digest of the value. Two checkpoints that both contain the same
leaked token still diff as equal in that field, which keeps the sanitized
history usable. The placeholder is not reversible.

This finds accidents. It is not a guarantee -- a secret with no recognizable
shape, split across fields, or encoded will pass. The README says so too.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .model import AgentState, MemoryEntry, Message

_MIN_ENTROPY_LEN = 32

# Hex tops out at log2(16) = 4.0 bits/char, so any threshold above that makes
# the heuristic structurally blind to hex-encoded keys. 3.5 sits below hex
# (~3.79) and above single-class identifiers, which the character-class check
# then filters out. See _high_entropy_spans.
_ENTROPY_THRESHOLD = 3.5


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0
    """Which capture group holds the secret itself.

    Group 0 redacts the whole match. Rules like ``password = hunter2`` need to
    keep the key visible and redact only the value, or the report becomes
    unreadable.
    """


# Order matters: the first rule to claim a span wins, so specific patterns must
# precede general ones. `sk-ant-...` would otherwise be reported as a generic
# `sk-` key, which is a worse finding for whoever has to act on it.
PATTERN_RULES: tuple[Rule, ...] = (
    Rule("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    Rule("anthropic-style-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    Rule("openai-style-key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    Rule("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    Rule(
        "private-key-block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]*?-----END [^-]*-----"
        ),
    ),
    Rule(
        "credentialed-url",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s@/]{3,})@"),
        group=1,
    ),
    Rule(
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|credential)\b"
            r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
        ),
        group=1,
    ),
)


@dataclass(frozen=True)
class Finding:
    location: str
    rule: str
    placeholder: str
    confidence: str = "high"

    def render(self) -> str:
        return f"  [{self.confidence:<6}] {self.rule:<20} {self.location}"


@dataclass(frozen=True)
class RedactionReport:
    findings: list[Finding] = field(default_factory=list)
    scanned_fields: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "scanned_fields": self.scanned_fields,
            "findings": [
                {
                    "location": f.location,
                    "rule": f.rule,
                    "placeholder": f.placeholder,
                    "confidence": f.confidence,
                }
                for f in self.findings
            ],
        }

    def render(self) -> str:
        if self.clean:
            return f"SECRET SCAN   CLEAN ({self.scanned_fields} fields scanned)"
        lines = [
            f"SECRET SCAN   {len(self.findings)} FINDING(S) ({self.scanned_fields} fields scanned)"
        ]
        lines += [f.render() for f in self.findings]
        lines.append("")
        lines.append(
            "  Values were replaced with stable, non-reversible placeholders. "
            "Rotate anything real that appears above -- redacting the checkpoint "
            "does not un-leak a credential that already reached this state."
        )
        return "\n".join(lines)


def placeholder_for(secret: str, rule: str) -> str:
    """Stable, non-reversible stand-in for a detected secret."""
    fingerprint = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:10]
    return f"[REDACTED:{rule}:{fingerprint}]"


def shannon_entropy(text: str) -> float:
    """Bits of entropy per character."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def scan_text(text: str, location: str, *, aggressive: bool = False) -> tuple[str, list[Finding]]:
    """Redact secrets in ``text``. Returns the cleaned text and what was found.

    Matching happens in one pass over the *original* text: every rule proposes
    spans, overlaps are resolved, and replacement happens once at the end.

    Substituting rule-by-rule instead would let a later rule match inside an
    earlier rule's placeholder -- ``[REDACTED:github-token:440bb94834]`` looks
    exactly like ``token:<value>`` to the generic assignment rule -- producing
    nested placeholders and phantom findings.
    """
    spans: list[tuple[int, int, str, str]] = []  # (start, end, secret, rule)

    for rule in PATTERN_RULES:
        for match in rule.pattern.finditer(text):
            secret = match.group(rule.group)
            if not secret:
                continue
            start = match.start(rule.group)
            spans.append((start, start + len(secret), secret, rule.name))

    if aggressive:
        spans.extend(_high_entropy_spans(text))

    return _apply_spans(text, spans, location)


def _apply_spans(
    text: str, spans: list[tuple[int, int, str, str]], location: str
) -> tuple[str, list[Finding]]:
    """Replace non-overlapping spans, earliest and longest first."""
    # Earliest start wins; on a tie the longer match wins, so a specific rule
    # that captures more of the credential beats a generic partial match.
    ordered = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))

    findings: list[Finding] = []
    pieces: list[str] = []
    cursor = 0
    for start, end, secret, rule in ordered:
        if start < cursor:
            continue  # already covered by an earlier, higher-priority span
        token = placeholder_for(secret, rule)
        findings.append(
            Finding(
                location=location,
                rule=rule,
                placeholder=token,
                confidence="low" if rule == "high-entropy" else "high",
            )
        )
        pieces.append(text[cursor:start])
        pieces.append(token)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), findings


_ENTROPY_CANDIDATE = re.compile(rf"[A-Za-z0-9+/=_-]{{{_MIN_ENTROPY_LEN},}}")


def _high_entropy_spans(text: str) -> list[tuple[int, int, str, str]]:
    """Propose spans that look random rather than written.

    Entropy alone does not separate secrets from long identifiers: a 64-char
    hex digest scores about 3.79 bits/char and ``configuration_manager_factory``
    about 3.55. The extra signal is *character-class mixing* -- credentials mix
    cases or digits, English identifiers rarely do. Requiring both cuts the
    obvious false positives without losing hex, whose 4.0 bits/char ceiling
    sits below any threshold high enough to exclude prose.
    """
    spans: list[tuple[int, int, str, str]] = []
    for match in _ENTROPY_CANDIDATE.finditer(text):
        candidate = match.group(0)
        if shannon_entropy(candidate) < _ENTROPY_THRESHOLD:
            continue
        if _character_classes(candidate) < 2:
            continue
        spans.append((match.start(), match.end(), candidate, "high-entropy"))
    return spans


def _character_classes(value: str) -> int:
    return sum(
        [
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
            any(c in "+/=_-" for c in value),
        ]
    )


def sanitize_state(
    state: AgentState,
    *,
    aggressive: bool = False,
    drop_memory_kinds: Iterable[str] = (),
    strip_runtime_opaque: bool = False,
) -> tuple[AgentState, RedactionReport]:
    """Return a redacted copy of ``state`` and a report of what was removed.

    ``drop_memory_kinds`` removes whole memory categories -- useful when
    sharing a checkpoint for reproduction and ``working`` memory is known to
    hold scratch credentials. ``strip_runtime_opaque`` clears adapter-private
    fields, which are the least inspectable part of any state.
    """
    findings: list[Finding] = []
    scanned = 0

    memory: list[MemoryEntry] = []
    dropped_kinds = set(drop_memory_kinds)
    for entry in state.memory:
        if entry.kind.value in dropped_kinds:
            findings.append(
                Finding(
                    location=f"memory[{entry.id}]",
                    rule="dropped-memory-kind",
                    placeholder="(entry removed)",
                )
            )
            continue
        scanned += 1
        cleaned, found = scan_text(
            entry.content, f"memory[{entry.id}].content", aggressive=aggressive
        )
        findings.extend(found)
        memory.append(entry if cleaned == entry.content else _replace(entry, content=cleaned))

    context: list[Message] = []
    for index, message in enumerate(state.context):
        scanned += 1
        cleaned, found = scan_text(
            message.content, f"context[{index}].content", aggressive=aggressive
        )
        findings.extend(found)
        context.append(
            message if cleaned == message.content else _replace(message, content=cleaned)
        )

    provider_params, found = _scan_mapping(
        state.provider.params, "provider.params", aggressive=aggressive
    )
    findings.extend(found)
    scanned += len(state.provider.params)

    cursor, found = _scan_mapping(state.execution.cursor, "execution.cursor", aggressive=aggressive)
    findings.extend(found)
    scanned += len(state.execution.cursor)

    if strip_runtime_opaque and state.runtime_opaque:
        findings.append(
            Finding(
                location="runtime_opaque",
                rule="stripped-runtime-opaque",
                placeholder="(cleared)",
            )
        )
        runtime_opaque: dict[str, Any] = {}
    else:
        runtime_opaque, found = _scan_mapping(
            state.runtime_opaque, "runtime_opaque", aggressive=aggressive
        )
        findings.extend(found)
        scanned += len(state.runtime_opaque)

    sanitized = state.replace(
        memory=memory,
        context=context,
        provider=_replace(state.provider, params=provider_params),
        execution=_replace(state.execution, cursor=cursor),
        runtime_opaque=runtime_opaque,
    )
    if findings:
        sanitized = sanitized.with_event(
            "state.sanitized",
            {"findings": len(findings), "rules": sorted({f.rule for f in findings})},
        )

    return sanitized, RedactionReport(findings=findings, scanned_fields=scanned)


def _scan_mapping(
    mapping: dict[str, Any], prefix: str, *, aggressive: bool
) -> tuple[dict[str, Any], list[Finding]]:
    """Recursively redact string leaves of a JSON-ish mapping."""
    findings: list[Finding] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, str):
            cleaned, found = scan_text(value, path, aggressive=aggressive)
            findings.extend(found)
            return cleaned
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    return walk(dict(mapping), prefix), findings


def _replace(obj: Any, **changes: Any) -> Any:
    import dataclasses

    return dataclasses.replace(obj, **changes)
