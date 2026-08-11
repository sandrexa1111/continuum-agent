"""Moving agent state to a different runtime, model, or provider.

Migration is the feature most likely to be oversold, so this module is built
around a single rule: **never let information loss be silent.** Every section of
the state is classified as one of

``PORTABLE``
    Moves unchanged. Its content address is preserved.

``TRANSLATED``
    Moves, but not byte-for-byte. Something was rewritten to suit the
    destination -- roles remapped, context compacted. The state still means
    roughly the same thing; it is no longer the same bytes.

``UNAVAILABLE``
    Does not move. Provider-side conversation handles, cached prefixes, adapter
    scratch state. These are dropped and named in the report.

What Continuum explicitly does **not** claim to move: anything inside the model.
There is no such thing here as transferring "what the model had learned" mid-
conversation. The unit of portability is the agent's *execution state* --
objective, task, memory, artifacts, capabilities, event history -- not the
inference process that consumed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .capabilities import CompatibilityReport, check_capabilities
from .compaction import CompactionResult, TokenCounter, compact_context, estimate_tokens
from .errors import ContinuumError
from .model import AgentState, Message, Provider, now_iso


class Portability(str, Enum):
    PORTABLE = "PORTABLE"
    TRANSLATED = "TRANSLATED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RoleProfile:
    """What message roles a destination accepts.

    This is a *declaration by the caller*, not a tested compatibility claim.
    Continuum ships two obvious shapes and reads the rest from whoever knows
    the destination -- the alternative is a hardcoded table of provider quirks
    that is wrong within a month.
    """

    supported: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})
    fallback: str = "user"

    def translate(self, role: str) -> str:
        return role if role in self.supported else self.fallback


ROLES_WITH_TOOLS = RoleProfile()
ROLES_CHAT_ONLY = RoleProfile(supported=frozenset({"system", "user", "assistant"}))


@dataclass(frozen=True)
class MigrationTarget:
    """Where the state is going."""

    adapter: str
    provider: str = ""
    model: str = ""
    max_context_tokens: int | None = None
    granted_capabilities: list[str] = field(default_factory=list)
    roles: RoleProfile = ROLES_WITH_TOOLS
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    section: str
    portability: Portability
    detail: str

    def render(self) -> str:
        mark = {
            Portability.PORTABLE: "OK  ",
            Portability.TRANSLATED: "~   ",
            Portability.UNAVAILABLE: "DROP",
        }[self.portability]
        return f"  {mark} {self.section:<22} {self.detail}"


@dataclass(frozen=True)
class MigrationReport:
    source: str
    target: str
    findings: list[Finding] = field(default_factory=list)
    capability_report: CompatibilityReport | None = None
    compaction: CompactionResult | None = None
    fatal: list[str] = field(default_factory=list)
    """Sections whose loss makes the migration unusable, not merely lossy."""

    @property
    def blocked(self) -> bool:
        """True when the destination cannot host this agent at all.

        Either a required capability is missing, or a section that the agent
        cannot run without could not be carried across.
        """
        return bool(self.fatal) or bool(self.capability_report and not self.capability_report.ok)

    @property
    def lossless(self) -> bool:
        return all(f.portability is Portability.PORTABLE for f in self.findings)

    def by_portability(self, level: Portability) -> list[Finding]:
        return [f for f in self.findings if f.portability is level]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "blocked": self.blocked,
            "lossless": self.lossless,
            "fatal": list(self.fatal),
            "findings": [
                {"section": f.section, "portability": f.portability.value, "detail": f.detail}
                for f in self.findings
            ],
            "capabilities": self.capability_report.to_dict() if self.capability_report else None,
            "compaction": self.compaction.to_dict() if self.compaction else None,
        }

    def render(self) -> str:
        lines = [
            "MIGRATION REPORT",
            f"  from   {self.source}",
            f"  to     {self.target}",
            "",
        ]
        for level in (Portability.PORTABLE, Portability.TRANSLATED, Portability.UNAVAILABLE):
            found = self.by_portability(level)
            if found:
                lines.append(f"{level.value}:")
                lines += [f.render() for f in found]
                lines.append("")
        if self.capability_report:
            lines.append(self.capability_report.render())
            lines.append("")
        if self.compaction and not self.compaction.lossless:
            lines.append(self.compaction.render())
            lines.append("")
        lines.append(
            "RESULT: "
            + ("BLOCKED" if self.blocked else "CLEAN" if self.lossless else "COMPLETED WITH LOSS")
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class MigrationResult:
    state: AgentState
    report: MigrationReport

    @property
    def ok(self) -> bool:
        return not self.report.blocked


def migrate(
    state: AgentState,
    target: MigrationTarget,
    *,
    token_counter: TokenCounter | None = None,
) -> MigrationResult:
    """Translate ``state`` for ``target`` and report exactly what changed.

    The returned state is always produced, even when the report is ``BLOCKED``:
    callers that want to inspect an impossible migration should be able to,
    and it is :func:`continuum.runtime.resume` -- not this function -- that
    refuses to run. Separating "what would this look like there" from "may it
    run there" keeps the diagnostic usable as a dry run.
    """
    findings: list[Finding] = []
    source_label = _label(state.provider)
    target_label = f"{target.adapter}/{target.provider or '-'}/{target.model or '-'}"

    same_provider = state.provider.provider == target.provider and bool(target.provider)
    same_adapter = state.provider.adapter == target.adapter

    findings.append(Finding("identity", Portability.PORTABLE, "carried unchanged"))
    findings.append(
        Finding(
            "objective",
            Portability.PORTABLE,
            f"goal preserved ({len(state.objective.constraints)} constraints)",
        )
    )
    findings.append(
        Finding(
            "execution",
            Portability.PORTABLE,
            f"task {state.execution.current_task!r} at step {state.execution.step}",
        )
    )
    findings.append(
        Finding("memory", Portability.PORTABLE, f"{len(state.memory)} entries, structure preserved")
    )
    findings.append(
        Finding(
            "artifacts",
            Portability.PORTABLE,
            f"{len(state.artifacts)} artifacts referenced by content address",
        )
    )
    findings.append(
        Finding(
            "events", Portability.PORTABLE, f"{len(state.events)} events, append-only history kept"
        )
    )
    findings.append(Finding("lineage", Portability.PORTABLE, "ancestry chain extended, not reset"))

    # -- context -------------------------------------------------------
    context = list(state.context)
    if same_provider:
        findings.append(
            Finding(
                "context",
                Portability.PORTABLE,
                f"{len(context)} messages, same provider representation",
            )
        )
    else:
        context, remapped = _translate_roles(context, target.roles)
        detail = f"{len(context)} messages re-encoded for {target.provider or 'destination'}"
        if remapped:
            detail += (
                f"; {remapped} messages had unsupported roles remapped to {target.roles.fallback!r}"
            )
        findings.append(Finding("context", Portability.TRANSLATED, detail))

    # -- provider-side opaque state ------------------------------------
    if state.provider.opaque:
        if same_provider and same_adapter:
            findings.append(
                Finding(
                    "provider.opaque",
                    Portability.PORTABLE,
                    "same provider and adapter, handles retained",
                )
            )
            opaque = dict(state.provider.opaque)
        else:
            findings.append(
                Finding(
                    "provider.opaque",
                    Portability.UNAVAILABLE,
                    f"{len(state.provider.opaque)} provider-side handle(s) dropped: "
                    f"{', '.join(sorted(state.provider.opaque))} "
                    "(server-held state cannot be exported)",
                )
            )
            opaque = {}
    else:
        opaque = {}

    if state.runtime_opaque and not same_adapter:
        findings.append(
            Finding(
                "runtime_opaque",
                Portability.UNAVAILABLE,
                f"{len(state.runtime_opaque)} adapter-private field(s) dropped moving "
                f"{state.provider.adapter or '(none)'} -> {target.adapter}",
            )
        )
        runtime_opaque: dict[str, Any] = {}
    else:
        runtime_opaque = dict(state.runtime_opaque)

    # -- environment ---------------------------------------------------
    findings.append(
        Finding(
            "environment",
            Portability.TRANSLATED,
            "source fingerprint retained for diagnostics; destination values differ",
        )
    )

    migrated = state.replace(
        context=context,
        provider=Provider(
            adapter=target.adapter,
            provider=target.provider,
            model=target.model,
            params=dict(target.params),
            opaque=opaque,
        ),
        runtime_opaque=runtime_opaque,
    )

    # -- context budget ------------------------------------------------
    compaction: CompactionResult | None = None
    fatal: list[str] = []
    if target.max_context_tokens is not None:
        counter = token_counter or estimate_tokens
        used = sum(counter(m.content) for m in migrated.context)
        if used > target.max_context_tokens:
            try:
                compaction = compact_context(
                    migrated, target.max_context_tokens, token_counter=counter
                )
            except ContinuumError as exc:
                # The destination is too small to hold even the agent's
                # instructions. Reporting that is far more useful than raising:
                # a caller running a compatibility matrix wants a row, not a
                # traceback.
                fatal.append("context.budget")
                findings.append(
                    Finding(
                        "context.budget",
                        Portability.UNAVAILABLE,
                        f"cannot fit {target.max_context_tokens} tokens: {exc}",
                    )
                )
            else:
                migrated = compaction.state
                findings.append(
                    Finding(
                        "context.budget",
                        Portability.TRANSLATED,
                        f"{compaction.dropped} message(s) summarized to fit "
                        f"{target.max_context_tokens} tokens",
                    )
                )
        else:
            findings.append(
                Finding(
                    "context.budget",
                    Portability.PORTABLE,
                    f"~{used} tokens fits the {target.max_context_tokens} token destination",
                )
            )

    capability_report = check_capabilities(migrated, target.granted_capabilities)
    migrated = migrated.replace(
        capabilities=migrated.capabilities.__class__(
            requires=migrated.capabilities.requires,
            optional=migrated.capabilities.optional,
            granted=sorted(set(target.granted_capabilities)),
        )
    )

    report = MigrationReport(
        source=source_label,
        target=target_label,
        findings=findings,
        capability_report=capability_report,
        compaction=compaction,
        fatal=fatal,
    )

    migrated = migrated.with_event(
        "migration.completed",
        {
            "from": source_label,
            "to": target_label,
            "lossless": report.lossless,
            "blocked": report.blocked,
            "dropped_sections": [f.section for f in report.by_portability(Portability.UNAVAILABLE)],
        },
        ts=now_iso(),
    )
    return MigrationResult(state=migrated, report=report)


def _translate_roles(messages: list[Message], profile: RoleProfile) -> tuple[list[Message], int]:
    """Remap roles the destination does not accept, recording the original."""
    translated: list[Message] = []
    remapped = 0
    for message in messages:
        new_role = profile.translate(message.role)
        if new_role == message.role:
            translated.append(message)
            continue
        remapped += 1
        metadata = dict(message.metadata)
        metadata["continuum.original_role"] = message.role
        translated.append(
            Message(
                role=new_role,
                # The original role is preserved in the visible text as well as
                # in metadata: a destination that ignores metadata would
                # otherwise lose the fact that this was a tool result.
                content=f"[{message.role}] {message.content}",
                name=message.name,
                tool_call_id=message.tool_call_id,
                pinned=message.pinned,
                metadata=metadata,
            )
        )
    return translated, remapped


def _label(provider: Provider) -> str:
    return f"{provider.adapter or '-'}/{provider.provider or '-'}/{provider.model or '-'}"
