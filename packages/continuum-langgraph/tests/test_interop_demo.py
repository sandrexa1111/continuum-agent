"""The interoperability demonstration must keep working.

It is the artifact the README points at to justify the claim that Continuum's
adapter interface is real. A demo that has quietly rotted is worse than no demo.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1] / "examples" / "interop_demo.py"


def run_demo() -> str:
    result = subprocess.run(
        [sys.executable, str(DEMO)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        f"demo exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout


def test_demo_runs_and_proves_each_claim():
    output = run_demo()

    # Export is repeatable, so checkpointing is idempotent.
    assert "repeatable   True" in output

    # The image round-trips byte-identically.
    assert "round trip matches" in output

    # Core tooling read a LangGraph state without importing langgraph.
    assert "langgraph never imported" in output

    # Migration named what it dropped rather than dropping it silently.
    assert "UNAVAILABLE:" in output
    assert "checkpoint_id" in output
    assert "RESULT: COMPLETED WITH LOSS" in output

    # The agent continued in a fresh runtime instead of restarting.
    assert "scan output appears 1 time(s) -- not re-run" in output
    assert "status         completed" in output

    # Checkpoint identity genuinely did not survive.
    assert "same?                 False" in output

    # A mismatched graph is refused.
    assert "REFUSED: graph topology mismatch" in output


def test_demo_needs_no_network_or_credentials():
    """Nothing in the demo may reach for a provider."""
    source = DEMO.read_text(encoding="utf-8")
    for forbidden in ("openai", "anthropic", "api_key", "API_KEY", "requests.", "httpx."):
        assert forbidden not in source, f"demo references {forbidden!r}"
