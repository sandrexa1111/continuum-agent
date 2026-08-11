"""The shipped examples must actually run.

A broken example is worse than no example: it is the first thing a new reader
tries, and it fails in a way that reflects on everything else. These run the
real scripts rather than re-implementing them.
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.py"))


def test_examples_directory_is_not_empty():
    assert EXAMPLES, "examples/ should contain at least one runnable script"


@pytest.mark.parametrize("script", EXAMPLES, ids=lambda p: p.stem)
def test_example_runs_as_a_script(script: Path):
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_custom_adapter_demonstrates_divergent_forks(capsys):
    """The example's point is that two branches genuinely differ."""
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples" / "custom_adapter.py"),
        run_name="__main__",
    )
    output = capsys.readouterr().out

    assert "same digest, nothing written" in output
    assert "defer launch" in output
    assert "launch now" in output


def test_custom_adapter_satisfies_the_protocol_structurally():
    """No inheritance, no Continuum import in the class body -- still an adapter."""
    from continuum.adapters.base import ContinuumAdapter

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples" / "custom_adapter.py")
    )
    planner = module["TinyPlanner"]()

    assert isinstance(planner, ContinuumAdapter)
    assert ContinuumAdapter not in type(planner).__mro__
