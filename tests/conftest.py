"""Shared fixtures.

States built here use fixed timestamps. Anything that mints ``now()`` inside a
fixture makes content addresses vary between runs, which would quietly turn the
determinism tests into no-ops that pass for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.model import (
    AgentState,
    Artifact,
    Capabilities,
    Environment,
    Event,
    Execution,
    ExecutionStatus,
    Identity,
    MemoryEntry,
    MemoryKind,
    Message,
    Objective,
    Provider,
)

FIXED_TS = "2026-01-01T00:00:00Z"

CORPUS = {
    "notes.md": "line one\nFIXME insecure default credentials\nWARNING: unbounded retry\n",
    "plan.txt": "TODO refactor parser\nrisk: silent failure on 429\n",
}


@pytest.fixture
def state() -> AgentState:
    """A representative, fully-populated state."""
    return AgentState(
        identity=Identity(agent_id="analyst-7", display_name="Analyst", created_at=FIXED_TS),
        objective=Objective(
            goal="Reconcile the ledger",
            constraints=["no writes to production"],
            success_criteria=["discrepancies enumerated"],
        ),
        execution=Execution(
            current_task="research_market",
            status=ExecutionStatus.SUSPENDED,
            step=3,
            cursor={"offset": 12},
            pending_tasks=["summarize"],
        ),
        provider=Provider(
            adapter="native-reviewer",
            provider="mock",
            model="m1",
            params={"ranking": "severity"},
            opaque={"thread_id": "t-1"},
        ),
        memory=[
            MemoryEntry(
                id="mem-1",
                kind=MemoryKind.EPISODIC,
                content="supplier invoice 4471 is short by 12.40",
                created_at=FIXED_TS,
                source="tool:ledger",
                importance=0.7,
            ),
            MemoryEntry(
                id="mem-2",
                kind=MemoryKind.SEMANTIC,
                content="invoices settle net-30",
                created_at=FIXED_TS,
                importance=0.4,
                pinned=True,
            ),
        ],
        context=[
            Message(role="system", content="You reconcile ledgers.", pinned=True),
            Message(role="user", content="Check invoice 4471."),
            Message(role="assistant", content="Pulling the invoice now."),
        ],
        capabilities=Capabilities(
            requires=["filesystem.read", "ledger.read"],
            optional=["web.search"],
            granted=["filesystem.read", "ledger.read"],
        ),
        environment=Environment(
            os="linux", arch="x86_64", runtime="cpython", runtime_version="3.11.9"
        ),
        artifacts=[
            Artifact(id="research", path="artifacts/research.md", digest="sha256:" + "a" * 64),
            Artifact(
                id="analysis",
                path="artifacts/analysis.json",
                digest="sha256:" + "b" * 64,
                derived_from=["research"],
            ),
        ],
        events=[
            Event(seq=0, ts=FIXED_TS, type="task.changed", data={"to": "research_market"}),
            Event(seq=1, ts=FIXED_TS, type="memory.written", data={"count": 2}),
        ],
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace containing a small corpus for the reference adapter."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, text in CORPUS.items():
        (corpus / name).write_text(text, encoding="utf-8")
    return tmp_path
