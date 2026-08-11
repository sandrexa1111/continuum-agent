"""Capability compatibility checking.

Resuming an agent into an environment that cannot do what the agent needs is
the quiet failure mode this module exists to prevent. An agent whose task is
"reconcile these invoices and email the supplier" and which resumes somewhere
without ``email.send`` does not fail -- it works for a while and then produces a
wrong result. So the check happens *before* execution, and a missing required
capability blocks the resume rather than warning about it.

Capabilities are dotted names (``filesystem.read``, ``email.send``). A grant may
use a trailing wildcard to cover a namespace (``filesystem.*``), which keeps
host configuration from having to enumerate every verb a runtime might add.
Requirements may not be wildcards: an agent that says it needs "anything under
filesystem" has not said anything checkable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from .errors import FormatError
from .model import AgentState


class Verdict(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CompatibilityReport:
    """The result of checking one state against one environment."""

    satisfied: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    unused_grants: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        if self.missing_required:
            return Verdict.BLOCKED
        if self.missing_optional:
            return Verdict.PARTIAL
        return Verdict.PASS

    @property
    def ok(self) -> bool:
        return not self.missing_required

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "satisfied": list(self.satisfied),
            "missing_required": list(self.missing_required),
            "missing_optional": list(self.missing_optional),
            "unused_grants": list(self.unused_grants),
        }

    def render(self) -> str:
        lines = [f"CAPABILITY CHECK   {self.verdict.value}"]
        if self.satisfied:
            lines.append("  satisfied:")
            lines += [f"    OK        {name}" for name in self.satisfied]
        if self.missing_optional:
            lines.append("  degraded (optional, resume allowed):")
            lines += [f"    MISSING   {name}" for name in self.missing_optional]
        if self.missing_required:
            lines.append("  blocking (required, resume refused):")
            lines += [f"    MISSING   {name}" for name in self.missing_required]
        return "\n".join(lines)


def check_capabilities(
    state: AgentState, granted: Iterable[str] | None = None
) -> CompatibilityReport:
    """Compare a state's declared needs against the capabilities on offer.

    ``granted`` defaults to whatever the state itself recorded as granted,
    which is the right default for a same-host resume and the wrong one for a
    migration -- callers moving an agent should pass the destination's grants.
    """
    grants = set(granted) if granted is not None else set(state.capabilities.granted)
    for requirement in [*state.capabilities.requires, *state.capabilities.optional]:
        if "*" in requirement:
            raise FormatError(
                f"capability requirement {requirement!r} contains a wildcard; "
                "requirements must name exactly what they need so the check is decidable"
            )

    satisfied: list[str] = []
    missing_required: list[str] = []
    missing_optional: list[str] = []
    matched_grants: set[str] = set()

    for requirement in sorted(set(state.capabilities.requires)):
        grant = _matching_grant(requirement, grants)
        if grant is None:
            missing_required.append(requirement)
        else:
            satisfied.append(requirement)
            matched_grants.add(grant)

    for requirement in sorted(set(state.capabilities.optional)):
        grant = _matching_grant(requirement, grants)
        if grant is None:
            missing_optional.append(requirement)
        else:
            satisfied.append(requirement)
            matched_grants.add(grant)

    return CompatibilityReport(
        satisfied=sorted(satisfied),
        missing_required=missing_required,
        missing_optional=missing_optional,
        unused_grants=sorted(grants - matched_grants),
    )


def satisfies(requirement: str, granted: Iterable[str]) -> bool:
    """Return True if ``granted`` covers ``requirement``.

    The single place wildcard semantics are implemented. Adapters must call
    this rather than testing ``requirement in granted`` -- an adapter that
    rolls its own check will reject a legitimate ``filesystem.*`` grant, which
    is exactly the bug this function exists to prevent.
    """
    return _matching_grant(requirement, set(granted)) is not None


def _matching_grant(requirement: str, grants: set[str]) -> str | None:
    """Return the grant that satisfies ``requirement``, preferring exact matches."""
    if requirement in grants:
        return requirement
    for grant in sorted(grants):
        if grant.endswith(".*") and requirement.startswith(grant[:-1]):
            return grant
        if grant == "*":
            return grant
    return None
