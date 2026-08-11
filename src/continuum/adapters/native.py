"""A deterministic reference runtime.

This adapter exists so that every Continuum feature can be demonstrated, tested
and reproduced **without an API key, a network connection, or a model
provider**. It is a real multi-step agent -- it reads a corpus, extracts
findings, ranks them under a policy, and writes artifacts that derive from one
another -- it simply reaches its decisions by rule rather than by inference.

That is a deliberate trade. A reference runtime backed by a live model would
make the test suite non-deterministic, the demo unreproducible, and the whole
project unusable to anyone evaluating it on a plane. The parts of Continuum
under test -- serialization, forking, lineage, migration diagnostics -- are the
same either way.

Where a real agent would call a model, this one consults
:class:`ReviewPolicy`, whose parameters arrive through
``AgentState.provider.params``. Two "models" with different policies genuinely
produce different artifacts from the same checkpoint, which is what makes the
fork-and-compare demonstration meaningful rather than staged.

The provider is reported as ``mock/deterministic-reviewer``. Nothing here
should ever be presented as a real model.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..canonical import digest_bytes
from ..capabilities import satisfies
from ..errors import AdapterError
from ..model import (
    AgentState,
    Artifact,
    Capabilities,
    Environment,
    Execution,
    ExecutionStatus,
    Identity,
    MemoryEntry,
    MemoryKind,
    Message,
    Objective,
    Provider,
    now_iso,
)

ADAPTER_NAME = "native-reviewer"

PLAN: tuple[str, ...] = ("scan", "extract", "rank", "report", "review")
"""The fixed plan. Each entry is one :meth:`NativeReviewAgent.step` and one
checkpointable boundary."""

REQUIRED_CAPABILITIES = ("filesystem.read", "filesystem.write")


@dataclass(frozen=True)
class ReviewPolicy:
    """The parameters that stand in for "which model is running".

    Changing these changes the agent's output, deterministically. That is the
    whole point: forking a checkpoint across two policies produces two
    genuinely different artifact sets that can be compared.
    """

    signal_terms: tuple[str, ...] = ("TODO", "FIXME", "WARNING", "risk", "fails", "insecure")
    ranking: str = "severity"
    max_findings: int = 5

    _SEVERITY: dict[str, int] = field(
        default_factory=lambda: {
            "insecure": 5,
            "FIXME": 4,
            "fails": 4,
            "WARNING": 3,
            "risk": 2,
            "TODO": 1,
        },
        repr=False,
        compare=False,
    )

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> ReviewPolicy:
        ranking = params.get("ranking", "severity")
        if ranking not in ("severity", "position", "length"):
            raise AdapterError(
                f"unknown ranking policy {ranking!r}; expected severity, position or length"
            )
        terms = params.get("signal_terms")
        max_findings = params.get("max_findings", 5)
        if not isinstance(max_findings, int) or max_findings < 1:
            raise AdapterError(f"max_findings must be a positive integer, got {max_findings!r}")
        return cls(
            signal_terms=tuple(terms) if terms else cls().signal_terms,
            ranking=ranking,
            max_findings=max_findings,
        )

    def to_params(self) -> dict[str, Any]:
        return {
            "signal_terms": list(self.signal_terms),
            "ranking": self.ranking,
            "max_findings": self.max_findings,
        }

    def severity(self, term: str) -> int:
        return self._SEVERITY.get(term, 1)

    def sort_key(self, finding: dict[str, Any]) -> tuple[Any, ...]:
        if self.ranking == "position":
            return (finding["document"], finding["line"])
        if self.ranking == "length":
            return (-len(finding["text"]), finding["document"], finding["line"])
        # Severity descending, then a stable tiebreak so ordering is total.
        return (-self.severity(finding["term"]), finding["document"], finding["line"])


class NativeReviewAgent:
    """A five-step review agent that can be stopped and resumed at any step."""

    name = ADAPTER_NAME

    def __init__(
        self,
        workspace: Path | str,
        *,
        agent_id: str = "reviewer",
        goal: str = "Review the corpus and produce a prioritized findings report",
        policy: ReviewPolicy | None = None,
        model_label: str = "deterministic-reviewer",
    ) -> None:
        self.workspace = Path(workspace)
        self.policy = policy or ReviewPolicy()
        self.model_label = model_label
        self._state = _initial_state(agent_id, goal, self.policy, model_label)

    # -- adapter protocol ----------------------------------------------

    def export_state(self) -> AgentState:
        """Return current state. Repeatable: no timestamps are minted here."""
        return self._state

    def import_state(self, state: AgentState) -> None:
        # Wildcard semantics live in continuum.capabilities; re-implementing
        # the check here would reject a legitimate `filesystem.*` grant.
        missing = [c for c in REQUIRED_CAPABILITIES if not satisfies(c, state.capabilities.granted)]
        if missing:
            raise AdapterError(
                f"{ADAPTER_NAME} cannot run without {missing}: it reads a corpus and writes "
                "artifacts, so there is no reduced mode for it to fall back to. "
                "Grant the capabilities (a namespace grant such as 'filesystem.*' works) "
                "or resume into an adapter that can degrade."
            )
        self._state = state
        self.policy = ReviewPolicy.from_params(state.provider.params)
        self.model_label = state.provider.model or self.model_label

    # -- execution -----------------------------------------------------

    def step(self) -> bool:
        """Run the next plan stage. Returns True if more work remains."""
        index = self._state.execution.step
        if index >= len(PLAN):
            return False

        stage = PLAN[index]
        handler = getattr(self, f"_stage_{stage}")
        self._state = handler(self._state)

        done = index + 1 >= len(PLAN)
        self._state = self._state.replace(
            execution=Execution(
                current_task=PLAN[index + 1] if not done else "complete",
                status=ExecutionStatus.COMPLETED if done else ExecutionStatus.RUNNING,
                step=index + 1,
                cursor={"plan": list(PLAN), "completed": PLAN[: index + 1]},
                pending_tasks=list(PLAN[index + 1 :]),
            )
        )
        self._state = self._state.with_event("task.changed", {"completed": stage})
        return not done

    def run_to_completion(self, max_steps: int = 100) -> AgentState:
        steps = 0
        while self.step():
            steps += 1
            if steps > max_steps:
                raise AdapterError("run exceeded max_steps; the plan should be finite")
        return self._state

    # -- stages --------------------------------------------------------

    def _stage_scan(self, state: AgentState) -> AgentState:
        corpus = self.workspace / "corpus"
        if not corpus.is_dir():
            raise AdapterError(
                f"no corpus at {corpus}; the reference agent expects a 'corpus' "
                "directory of .txt/.md files in its workspace"
            )
        documents = sorted(
            p for p in corpus.iterdir() if p.is_file() and p.suffix in (".txt", ".md")
        )
        if not documents:
            raise AdapterError(f"corpus at {corpus} contains no .txt or .md files")

        memory = list(state.memory)
        for path in documents:
            data = path.read_bytes()
            memory.append(
                MemoryEntry(
                    id=f"doc-{_slug(path.name)}",
                    kind=MemoryKind.EXTERNAL,
                    content=f"{path.name} ({len(data)} bytes)",
                    created_at=now_iso(),
                    source=str(path.relative_to(self.workspace)),
                    importance=0.3,
                    attributes={"digest": digest_bytes(data), "bytes": len(data)},
                )
            )
        return state.replace(
            memory=memory,
            context=[
                *state.context,
                Message(
                    role="assistant",
                    content=f"Scanned the corpus: {len(documents)} document(s) "
                    f"({', '.join(p.name for p in documents)}).",
                ),
            ],
        ).with_event("tool.called", {"tool": "filesystem.read", "documents": len(documents)})

    def _stage_extract(self, state: AgentState) -> AgentState:
        corpus = self.workspace / "corpus"
        findings: list[dict[str, Any]] = []
        for entry in state.memory:
            if entry.kind is not MemoryKind.EXTERNAL or not entry.id.startswith("doc-"):
                continue
            path = corpus / Path(entry.source).name
            for lineno, line in enumerate(
                path.read_text("utf-8", errors="replace").splitlines(), 1
            ):
                for term in self.policy.signal_terms:
                    if term in line:
                        findings.append(
                            {
                                "document": path.name,
                                "line": lineno,
                                "term": term,
                                "text": line.strip()[:200],
                            }
                        )
                        break  # one finding per line, first matching term wins

        memory = list(state.memory)
        for index, finding in enumerate(findings):
            memory.append(
                MemoryEntry(
                    id=f"finding-{index:03d}",
                    kind=MemoryKind.EPISODIC,
                    content=f"{finding['document']}:{finding['line']} [{finding['term']}] {finding['text']}",
                    created_at=now_iso(),
                    source=finding["document"],
                    importance=min(1.0, self.policy.severity(finding["term"]) / 5),
                    attributes=finding,
                )
            )
        return state.replace(
            memory=memory,
            context=[
                *state.context,
                Message(
                    role="assistant",
                    content=f"Extracted {len(findings)} candidate finding(s) matching "
                    f"{list(self.policy.signal_terms)}.",
                ),
            ],
        ).with_event("memory.written", {"kind": "episodic", "count": len(findings)})

    def _stage_rank(self, state: AgentState) -> AgentState:
        findings = [dict(m.attributes) for m in state.memory if m.id.startswith("finding-")]
        ordered = sorted(findings, key=self.policy.sort_key)[: self.policy.max_findings]
        memory = [
            *state.memory,
            MemoryEntry(
                id="ranking",
                kind=MemoryKind.PROCEDURAL,
                content=(
                    f"Selected {len(ordered)} of {len(findings)} findings using "
                    f"ranking={self.policy.ranking!r}, max_findings={self.policy.max_findings}"
                ),
                created_at=now_iso(),
                source="continuum.adapters.native",
                importance=0.8,
                pinned=True,
                attributes={"selected": ordered, "considered": len(findings)},
            ),
        ]
        return state.replace(
            memory=memory,
            context=[
                *state.context,
                Message(
                    role="assistant",
                    content=f"Ranked findings by {self.policy.ranking}; kept the top "
                    f"{len(ordered)} of {len(findings)}.",
                ),
            ],
        ).with_event("task.changed", {"ranked": len(ordered), "policy": self.policy.ranking})

    def _stage_report(self, state: AgentState) -> AgentState:
        selected = _ranking(state)["selected"]
        lines = [
            f"# Review report: {state.identity.agent_id}",
            "",
            f"Policy: ranking={self.policy.ranking}, max_findings={self.policy.max_findings}",
            f"Model:  {self.model_label}",
            "",
            "## Findings",
            "",
        ]
        if selected:
            lines += [
                f"{i}. **{f['term']}** — `{f['document']}:{f['line']}`\n   {f['text']}"
                for i, f in enumerate(selected, 1)
            ]
        else:
            lines.append("_No findings matched the configured signal terms._")
        body = "\n".join(lines) + "\n"

        artifact = self._write_artifact(
            state,
            artifact_id="report",
            relative="artifacts/report.md",
            body=body,
            media_type="text/markdown",
        )
        return artifact

    def _stage_review(self, state: AgentState) -> AgentState:
        import json

        selected = _ranking(state)["selected"]
        by_term: dict[str, int] = {}
        for finding in selected:
            by_term[finding["term"]] = by_term.get(finding["term"], 0) + 1
        summary = {
            "agent_id": state.identity.agent_id,
            "model": self.model_label,
            "policy": self.policy.to_params(),
            "selected_findings": len(selected),
            "by_term": dict(sorted(by_term.items())),
            "documents": sorted({f["document"] for f in selected}),
        }
        body = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        return self._write_artifact(
            state,
            artifact_id="review",
            relative="artifacts/review.json",
            body=body,
            media_type="application/json",
            derived_from=["report"],
        )

    # -- helpers -------------------------------------------------------

    def _write_artifact(
        self,
        state: AgentState,
        *,
        artifact_id: str,
        relative: str,
        body: str,
        media_type: str,
        derived_from: list[str] | None = None,
    ) -> AgentState:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = body.encode("utf-8")
        path.write_bytes(data)
        artifact = Artifact(
            id=artifact_id,
            path=relative,
            digest=digest_bytes(data),
            media_type=media_type,
            derived_from=derived_from or [],
            created_at=now_iso(),
        )
        artifacts = [a for a in state.artifacts if a.id != artifact_id] + [artifact]
        updated = state.replace(
            artifacts=artifacts,
            context=[
                *state.context,
                Message(role="assistant", content=f"Wrote {relative} ({len(data)} bytes)"),
            ],
        )
        return updated.with_event(
            "artifact.created",
            {"id": artifact_id, "path": relative, "digest": artifact.digest, "bytes": len(data)},
        )


def _ranking(state: AgentState) -> dict[str, Any]:
    for entry in state.memory:
        if entry.id == "ranking":
            return dict(entry.attributes)
    raise AdapterError("no ranking in state; the 'rank' stage has not run yet")


def _initial_state(agent_id: str, goal: str, policy: ReviewPolicy, model_label: str) -> AgentState:
    return AgentState(
        identity=Identity(
            agent_id=agent_id, display_name="Native reference reviewer", created_at=now_iso()
        ),
        objective=Objective(
            goal=goal,
            constraints=["read-only access to the corpus", "artifacts written under artifacts/"],
            success_criteria=["report.md exists", "review.json derives from report.md"],
        ),
        execution=Execution(
            current_task=PLAN[0],
            status=ExecutionStatus.SUSPENDED,
            step=0,
            cursor={"plan": list(PLAN), "completed": []},
            pending_tasks=list(PLAN),
        ),
        provider=Provider(
            adapter=ADAPTER_NAME,
            provider="mock",
            model=model_label,
            params=policy.to_params(),
        ),
        capabilities=Capabilities(
            requires=list(REQUIRED_CAPABILITIES),
            optional=["web.search"],
            granted=list(REQUIRED_CAPABILITIES),
        ),
        environment=Environment(
            os=platform.system().lower(),
            arch=platform.machine().lower(),
            runtime="cpython",
            runtime_version=".".join(str(p) for p in sys.version_info[:3]),
        ),
        context=[
            Message(
                role="system",
                content=f"You are a code review agent. Objective: {goal}",
                pinned=True,
            )
        ],
    )


def _slug(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return cleaned[:64] or hashlib.sha256(value.encode()).hexdigest()[:8]


def factory(workspace: Path | str, **kwargs: Any) -> NativeReviewAgent:
    """Entry-point factory for the ``native-reviewer`` adapter."""
    return NativeReviewAgent(workspace, **kwargs)
