"""Context compaction -- fitting a state into a smaller destination.

Migrating between models routinely means moving into a smaller context window.
Something has to go, and the interesting engineering question is not *how to
summarize* but *how to lose information accountably*.

The rules here:

* Pinned messages and the system prompt are never dropped. If they alone
  exceed the budget, compaction fails loudly instead of quietly truncating the
  agent's instructions.
* Recent messages are kept, oldest dropped first, because recency is the one
  proxy for relevance available without a model.
* Whatever is dropped is replaced by a summary recorded as a
  :class:`~continuum.model.MemoryEntry` with ``transformed=True``. Downstream
  readers can always tell a summary from an observation.
* The default summarizer is **deterministic and model-free** -- it records what
  was dropped rather than paraphrasing it. A model-backed summarizer can be
  injected, but the library never requires one, so compaction stays testable
  and free.

Token counting is an estimate unless the caller supplies a real tokenizer. The
estimator is deliberately pessimistic; see :func:`estimate_tokens`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .errors import ContinuumError
from .model import AgentState, MemoryEntry, MemoryKind, Message, now_iso

TokenCounter = Callable[[str], int]
Summarizer = Callable[[Sequence[Message]], str]

# Conservative characters-per-token ratio. Real tokenizers average nearer 4 for
# English prose but fall well below it for code, JSON, and non-Latin scripts --
# exactly the content agent context is full of. Under-estimating the budget is
# recoverable; over-estimating it means the destination rejects the request.
_CHARS_PER_TOKEN = 3.0


def estimate_tokens(text: str) -> int:
    """Approximate the token count of ``text`` without a tokenizer."""
    return max(1, int(len(text) / _CHARS_PER_TOKEN) + 1)


@dataclass(frozen=True)
class CompactionResult:
    state: AgentState
    kept: int
    dropped: int
    tokens_before: int
    tokens_after: int
    summary_memory_id: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def lossless(self) -> bool:
        return self.dropped == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "kept_messages": self.kept,
            "dropped_messages": self.dropped,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "lossless": self.lossless,
            "summary_memory_id": self.summary_memory_id,
            "notes": list(self.notes),
        }

    def render(self) -> str:
        verdict = "LOSSLESS" if self.lossless else "LOSSY"
        lines = [
            f"CONTEXT COMPACTION   {verdict}",
            f"  messages   {self.kept + self.dropped} -> {self.kept} ({self.dropped} summarized)",
            f"  tokens~    {self.tokens_before} -> {self.tokens_after}",
        ]
        if self.summary_memory_id:
            lines.append(f"  summary    recorded as memory {self.summary_memory_id}")
        lines += [f"  note       {n}" for n in self.notes]
        return "\n".join(lines)


def structural_summary(messages: Sequence[Message]) -> str:
    """Describe dropped messages without paraphrasing them.

    Deterministic on purpose: the same input always produces the same summary,
    so a compacted state has a stable content address and compaction can be
    unit-tested. It records shape (who spoke, how much, about what opening
    words) rather than inventing content that was never in the transcript.
    """
    if not messages:
        return ""
    by_role: dict[str, int] = {}
    for message in messages:
        by_role[message.role] = by_role.get(message.role, 0) + 1
    roles = ", ".join(f"{count} {role}" for role, count in sorted(by_role.items()))
    first = messages[0].content.strip().splitlines()[0][:120] if messages[0].content else ""
    last = messages[-1].content.strip().splitlines()[0][:120] if messages[-1].content else ""
    return (
        f"[compacted] {len(messages)} earlier messages ({roles}) were removed to fit the "
        f"destination context budget. First: {first!r}. Last: {last!r}. "
        "Original content is not recoverable from this entry."
    )


def compact_context(
    state: AgentState,
    max_tokens: int,
    *,
    token_counter: TokenCounter | None = None,
    summarizer: Summarizer | None = None,
    keep_recent: int = 2,
) -> CompactionResult:
    """Reduce ``state``'s context to fit ``max_tokens``.

    Returns the compacted state alongside a report of what was lost. If the
    context already fits, the state is returned unchanged and
    :attr:`CompactionResult.lossless` is True.

    Raises :class:`ContinuumError` when the non-droppable messages alone exceed
    the budget -- there is no correct silent behaviour in that case.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    count = token_counter or estimate_tokens
    summarize = summarizer or structural_summary

    def cost(messages: Sequence[Message]) -> int:
        return sum(count(m.content) for m in messages)

    before = cost(state.context)
    if before <= max_tokens:
        return CompactionResult(
            state=state,
            kept=len(state.context),
            dropped=0,
            tokens_before=before,
            tokens_after=before,
            notes=["context already within budget; state returned unchanged"],
        )

    protected_idx = {i for i, m in enumerate(state.context) if m.pinned or m.role == "system"}
    protected_idx |= set(range(max(0, len(state.context) - keep_recent), len(state.context)))

    floor = cost([state.context[i] for i in sorted(protected_idx)])
    if floor > max_tokens:
        raise ContinuumError(
            f"cannot compact below {floor} tokens: pinned, system and the {keep_recent} most "
            f"recent messages already exceed the {max_tokens} token budget. Raise the budget, "
            "unpin messages, or lower keep_recent -- silently dropping instructions would "
            "change what the agent is trying to do."
        )

    # Walk newest-to-oldest, keeping what fits. Protected messages are admitted
    # unconditionally; everything else competes for the remaining budget.
    kept_idx: set[int] = set(protected_idx)
    budget = max_tokens - floor
    for i in range(len(state.context) - 1, -1, -1):
        if i in kept_idx:
            continue
        price = count(state.context[i].content)
        if price <= budget:
            kept_idx.add(i)
            budget -= price

    kept = [m for i, m in enumerate(state.context) if i in kept_idx]
    dropped = [m for i, m in enumerate(state.context) if i not in kept_idx]

    memory = list(state.memory)
    summary_id: str | None = None
    if dropped:
        summary_id = _unique_memory_id(state, "compaction")
        memory.append(
            MemoryEntry(
                id=summary_id,
                kind=MemoryKind.EPISODIC,
                content=summarize(dropped),
                created_at=now_iso(),
                source="continuum.compaction",
                importance=0.4,
                transformed=True,
                attributes={"dropped_messages": len(dropped)},
            )
        )

    compacted = state.replace(context=kept, memory=memory)
    compacted = compacted.with_event(
        "context.compacted",
        {"dropped": len(dropped), "kept": len(kept), "budget_tokens": max_tokens},
    )
    after = cost(kept)

    return CompactionResult(
        state=compacted,
        kept=len(kept),
        dropped=len(dropped),
        tokens_before=before,
        tokens_after=after,
        summary_memory_id=summary_id,
        notes=[
            "dropped messages are summarized in a memory entry marked transformed=true",
            "token counts are estimates unless a tokenizer was supplied",
        ],
    )


def _unique_memory_id(state: AgentState, stem: str) -> str:
    existing = {m.id for m in state.memory}
    n = 1
    while f"{stem}-{n}" in existing:
        n += 1
    return f"{stem}-{n}"
