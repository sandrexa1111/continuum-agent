"""The reference demonstration behind ``continuum demo``.

Seven scenes, each proving one claim the README makes. Everything runs offline
against the deterministic reference adapter, so the output is reproducible on
any machine and identical between runs apart from timestamps.

The scenes deliberately include the two *negative* results -- a blocked resume
and a secret found in memory -- because a demo that only shows the happy path
is not evidence that the checks exist.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ._console import enable_utf8, heading
from .adapters.native import NativeReviewAgent
from .diff import diff_states
from .errors import ResumeBlocked
from .image import read_image, write_image
from .migrate import ROLES_CHAT_ONLY, MigrationTarget, migrate
from .model import MemoryEntry, MemoryKind, now_iso
from .redact import sanitize_state
from .runtime import checkpoint, fork, resume
from .store import Store

CORPUS = {
    "auth-service.md": """# auth-service notes

Session tokens are minted in `mint_session`.
FIXME insecure default credentials still ship in the sample config
The refresh path retries without a ceiling, which is a risk under load.
Rotation is documented but never scheduled.
""",
    "ingest-worker.md": """# ingest-worker notes

TODO refactor the parser once the schema settles
WARNING: the dead-letter queue is unbounded
Batch size is fixed at 500 records.
The retry loop fails silently when the upstream returns 429.
""",
    "billing.txt": """billing pipeline

TODO reconcile partial refunds against the ledger
Invoices older than 90 days are archived.
WARNING: currency conversion uses a cached rate with no staleness check
""",
}


def _scene(number: int, title: str) -> None:
    print()
    print(heading(f"{number}. {title}"))
    print()


def run_demo(workspace: Path) -> int:
    enable_utf8()
    workspace = Path(workspace)
    corpus_dir = workspace / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for name, text in CORPUS.items():
        (corpus_dir / name).write_text(text, encoding="utf-8")

    store = Store.init(workspace)
    print(f"workspace  {workspace.resolve()}")
    print(f"corpus     {len(CORPUS)} documents")
    print("provider   mock/deterministic-reviewer (no API key, no network)")

    # -- 1 -------------------------------------------------------------
    _scene(1, "Run the agent partway, then stop")
    agent = NativeReviewAgent(workspace, agent_id="reviewer")
    start = checkpoint(agent, store, label="initial")
    agent.step()  # scan
    agent.step()  # extract
    mid = checkpoint(agent, store, label="after-extract")
    state = store.get_state(mid.digest)
    print(f"  start        {start.digest.split(':')[1][:12]}")
    print(
        f"  suspended at {mid.digest.split(':')[1][:12]}  task={state.execution.current_task!r} step={state.execution.step}/5"
    )
    print(
        f"  memory       {len(state.memory)} entries ({sum(1 for m in state.memory if m.id.startswith('finding-'))} findings extracted)"
    )

    same = checkpoint(agent, store)
    print(
        f"  re-checkpoint without stepping -> {'same digest, nothing written' if same.digest == mid.digest else 'NEW DIGEST (bug)'}"
    )

    # -- 2 -------------------------------------------------------------
    _scene(2, "Export to a portable image and move it")
    image_path = workspace / "reviewer.asi"
    write_image(state, image_path)
    size = image_path.stat().st_size
    reloaded = read_image(image_path)
    print(f"  wrote        {image_path.name} ({size} bytes)")
    print("  verified     state.json and all blobs re-hashed on read")
    print(
        f"  round trip   {'byte-identical' if reloaded.state.digest() == state.digest() else 'MISMATCH (bug)'}"
    )

    elsewhere = workspace / "elsewhere"
    (elsewhere / "corpus").mkdir(parents=True, exist_ok=True)
    for name, text in CORPUS.items():
        (elsewhere / "corpus" / name).write_text(text, encoding="utf-8")
    print(f"  transported  to {elsewhere.name}/ (a different directory, empty store)")

    # -- 3 -------------------------------------------------------------
    _scene(3, "Resume there under a different model")
    moved, report = resume(
        image_path,
        NativeReviewAgent,
        granted=["filesystem.read", "filesystem.write"],
        workspace=elsewhere,
        agent_id="reviewer",
        model_label="deterministic-reviewer-b",
    )
    print(
        f"  {report.verdict.value}: {len(report.satisfied)} capability(s) satisfied, "
        f"{len(report.missing_optional)} optional missing"
    )
    final = moved.run_to_completion()
    print(
        f"  continued from step 2 -> {final.execution.status.value} at step {final.execution.step}/5"
    )
    for artifact in final.artifacts:
        print(f"  artifact     {artifact.path}  {artifact.digest.split(':')[1][:12]}")

    # -- 4 -------------------------------------------------------------
    _scene(4, "Fork the same checkpoint across two policies")
    branches = fork(state, store, ["severity", "length"])
    outcomes = {}
    for branch in branches:
        branch_ws = workspace / f"branch-{branch.label}"
        (branch_ws / "corpus").mkdir(parents=True, exist_ok=True)
        for name, text in CORPUS.items():
            (branch_ws / "corpus" / name).write_text(text, encoding="utf-8")
        seeded = branch.state.replace(
            provider=branch.state.provider.__class__(
                adapter=branch.state.provider.adapter,
                provider="mock",
                model=f"reviewer-{branch.label}",
                params={"ranking": branch.label, "max_findings": 3},
            )
        )
        runner, _ = resume(
            seeded,
            NativeReviewAgent,
            granted=["filesystem.read", "filesystem.write"],
            workspace=branch_ws,
            agent_id="reviewer",
        )
        outcomes[branch.label] = runner.run_to_completion()
        top = (branch_ws / "artifacts" / "report.md").read_text("utf-8").splitlines()
        headline = next((line for line in top if line.startswith("1.")), "(none)")
        print(
            f"  {branch.label:<10} {branch.digest.split(':')[1][:12]}  top finding: {headline[3:80]}"
        )

    # -- 5 -------------------------------------------------------------
    _scene(5, "Diff the two branches")
    print(diff_states(outcomes["severity"], outcomes["length"]).render())

    # -- 6 -------------------------------------------------------------
    _scene(6, "Migrate to a smaller, tool-less destination")
    crowded = outcomes["severity"]
    crowded = crowded.replace(
        provider=crowded.provider.__class__(
            adapter=crowded.provider.adapter,
            provider="mock",
            model=crowded.provider.model,
            params=crowded.provider.params,
            opaque={"server_thread_id": "thread_abc123", "cached_prefix_id": "pfx_99"},
        )
    )
    result = migrate(
        crowded,
        MigrationTarget(
            adapter="other-runtime",
            provider="another-provider",
            model="small-model",
            max_context_tokens=95,
            granted_capabilities=["filesystem.read", "filesystem.write"],
            roles=ROLES_CHAT_ONLY,
        ),
    )
    print(result.report.render())
    print("  Note the two provider-side handles above: they are named, not silently dropped.")

    # -- 7 -------------------------------------------------------------
    _scene(7, "Refuse an unsafe resume, and find a leaked secret")
    blocked_state = outcomes["severity"]
    try:
        resume(
            blocked_state,
            NativeReviewAgent,
            granted=["filesystem.read"],
            workspace=workspace / "nowhere",
        )
        print("  UNEXPECTED: resume succeeded without filesystem.write")
    except ResumeBlocked as exc:
        print(exc.report.render())
        print("  -> resume refused (exit code 3), rather than running an agent that cannot finish")

    print()
    leaky = blocked_state.replace(
        memory=[
            *blocked_state.memory,
            MemoryEntry(
                id="leaked-cred",
                kind=MemoryKind.WORKING,
                # Assembled rather than written out, so no complete token-shaped
                # literal sits in the source for a repository secret scanner to
                # trip over. The value is fabricated either way.
                content=(
                    "retrying upload with token "
                    + ("ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8 ")
                    + "against postgres://svc:s3cr3t-pass@db.internal:5432/billing"
                ),
                created_at=now_iso(),
                source="tool:http",
            ),
        ]
    )
    _, redaction = sanitize_state(leaky)
    print(redaction.render())

    print()
    print("Everything above ran offline and is reproducible. Next:")
    print(f"  continuum history --store {workspace}")
    print(f"  continuum inspect {image_path}")
    print(f"  continuum verify --store {workspace}")
    return 0


def reset(workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
