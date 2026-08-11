"""CLI behaviour, with emphasis on exit codes.

Scripts and CI act on exit status, so the contract is part of the interface:
0 success, 1 failure, 2 usage, 3 a check came back negative. A command that
reports a blocked resume and exits 0 is a bug even if its text is perfect.
"""

from __future__ import annotations

import json

import pytest

from continuum.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, main


@pytest.fixture
def ran(workspace, capsys):
    """A workspace with the reference agent run to completion."""
    assert main(["run", "--workspace", str(workspace)]) == EXIT_OK
    capsys.readouterr()
    return workspace


def out(capsys) -> str:
    return capsys.readouterr().out


class TestBasics:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "continuum-agent" in out(capsys)

    def test_no_command_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == EXIT_USAGE

    def test_init_creates_a_store(self, tmp_path, capsys):
        assert main(["init", str(tmp_path)]) == EXIT_OK
        assert (tmp_path / ".continuum" / "config.json").exists()

    def test_adapters_lists_the_reference_adapter(self, capsys):
        assert main(["adapters", "--json"]) == EXIT_OK
        assert "native-reviewer" in json.loads(out(capsys))["adapters"]


class TestRun:
    def test_run_completes_and_writes_artifacts(self, workspace, capsys):
        assert main(["run", "--workspace", str(workspace)]) == EXIT_OK
        assert "completed at step 5" in out(capsys)
        assert (workspace / "artifacts" / "report.md").exists()

    def test_run_without_a_corpus_is_a_usage_error(self, tmp_path, capsys):
        assert main(["run", "--workspace", str(tmp_path)]) == EXIT_USAGE
        assert "does not exist" in capsys.readouterr().err

    def test_partial_run_leaves_the_agent_mid_plan(self, workspace, capsys):
        assert main(["run", "--workspace", str(workspace), "--steps", "2"]) == EXIT_OK
        assert "stopped after 2 step(s)" in out(capsys)

    def test_ranking_policy_changes_the_output(self, workspace, tmp_path):
        other = tmp_path / "other"
        (other / "corpus").mkdir(parents=True)
        for path in (workspace / "corpus").iterdir():
            (other / "corpus" / path.name).write_bytes(path.read_bytes())

        main(["run", "--workspace", str(workspace), "--ranking", "severity"])
        main(["run", "--workspace", str(other), "--ranking", "length"])

        assert (workspace / "artifacts" / "report.md").read_text() != (
            other / "artifacts" / "report.md"
        ).read_text()


class TestHistoryAndInspect:
    def test_history_shows_the_chain(self, ran, capsys):
        assert main(["history", "--store", str(ran)]) == EXIT_OK
        text = out(capsys)
        assert "initial" in text
        assert "heads:" in text

    def test_history_json(self, ran, capsys):
        assert main(["history", "--store", str(ran), "--json"]) == EXIT_OK
        entries = json.loads(out(capsys))
        assert len(entries) > 1
        assert entries[0]["label"] == "initial"

    def test_history_on_an_empty_store(self, tmp_path, capsys):
        main(["init", str(tmp_path)])
        capsys.readouterr()
        assert main(["history", "--store", str(tmp_path)]) == EXIT_OK
        assert "no checkpoints" in out(capsys)

    def test_inspect_a_checkpoint(self, ran, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        assert main(["inspect", head, "--store", str(ran)]) == EXIT_OK
        assert "reviewer" in out(capsys)


class TestExportImportVerify:
    def test_export_then_inspect_the_image(self, ran, tmp_path, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        image = tmp_path / "agent.asi"

        assert main(["export", head, str(image), "--store", str(ran)]) == EXIT_OK
        assert image.exists()
        capsys.readouterr()  # drop the export line so only the JSON remains

        assert main(["inspect", str(image), "--json"]) == EXIT_OK
        assert json.loads(out(capsys))["agent_id"] == "reviewer"

    def test_export_with_artifacts_packs_blobs(self, ran, tmp_path, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        image = tmp_path / "full.asi"
        main(
            [
                "export",
                head,
                str(image),
                "--store",
                str(ran),
                "--workspace",
                str(ran),
                "--include-artifacts",
            ]
        )
        assert "2 artifact blob(s) packed" in out(capsys)

    def test_verify_a_clean_store(self, ran, capsys):
        assert main(["verify", "--store", str(ran)]) == EXIT_OK
        assert "verified against their content addresses" in out(capsys)

    def test_verify_detects_a_corrupt_object(self, ran, capsys):
        import zlib

        objects = list((ran / ".continuum" / "objects").rglob("*"))
        target = next(p for p in objects if p.is_file())
        target.write_bytes(zlib.compress(b"tampered"))

        assert main(["verify", "--store", str(ran)]) == EXIT_CHECK_FAILED
        assert "failed verification" in capsys.readouterr().err

    def test_verify_a_tampered_image_fails(self, ran, tmp_path, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        image = tmp_path / "a.asi"
        main(["export", head, str(image), "--store", str(ran)])
        image.write_bytes(image.read_bytes()[:200])

        assert main(["verify", "--image", str(image)]) == EXIT_CHECK_FAILED

    def test_import_round_trip(self, ran, tmp_path, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        image = tmp_path / "a.asi"
        main(["export", head, str(image), "--store", str(ran)])

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        main(["init", str(fresh)])
        capsys.readouterr()
        assert main(["import", str(image), "--store", str(fresh)]) == EXIT_OK
        assert head in out(capsys)


class TestForkDiffResume:
    def test_fork_creates_labelled_branches(self, ran, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        assert (
            main(["fork", head, "--label", "a", "--label", "b", "--store", str(ran), "--json"])
            == EXIT_OK
        )
        branches = json.loads(out(capsys))
        assert [b["label"] for b in branches] == ["a", "b"]

    def test_diff_two_checkpoints(self, ran, capsys):
        entries = json.loads(_history(ran, capsys))
        assert (
            main(["diff", entries[0]["digest"], entries[-1]["digest"], "--store", str(ran)])
            == EXIT_OK
        )
        assert "Execution" in out(capsys)

    def test_resume_from_an_image(self, workspace, tmp_path, capsys):
        main(["run", "--workspace", str(workspace), "--steps", "2"])
        capsys.readouterr()
        entries = json.loads(_history(workspace, capsys))
        image = tmp_path / "mid.asi"
        main(["export", entries[-1]["digest"], str(image), "--store", str(workspace)])
        capsys.readouterr()

        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "corpus").mkdir(parents=True)
        for path in (workspace / "corpus").iterdir():
            (elsewhere / "corpus" / path.name).write_bytes(path.read_bytes())

        assert (
            main(
                [
                    "resume",
                    str(image),
                    "--workspace",
                    str(elsewhere),
                    "--grant",
                    "filesystem.read",
                    "--grant",
                    "filesystem.write",
                    "--run",
                ]
            )
            == EXIT_OK
        )
        assert (elsewhere / "artifacts" / "report.md").exists()

    def test_resume_without_capabilities_exits_three(self, ran, tmp_path, capsys):
        entries = json.loads(_history(ran, capsys))
        image = tmp_path / "a.asi"
        main(["export", entries[-1]["digest"], str(image), "--store", str(ran)])
        capsys.readouterr()

        assert (
            main(
                [
                    "resume",
                    str(image),
                    "--workspace",
                    str(tmp_path / "x"),
                    "--grant",
                    "filesystem.read",
                ]
            )
            == EXIT_CHECK_FAILED
        )
        assert "RESUME REFUSED" in capsys.readouterr().err


class TestMigrateAndChecks:
    def test_migrate_reports_and_records(self, ran, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        assert (
            main(
                [
                    "migrate",
                    head,
                    "--store",
                    str(ran),
                    "--adapter",
                    "other",
                    "--provider",
                    "p2",
                    "--model",
                    "m2",
                    "--grant",
                    "filesystem.read",
                    "--grant",
                    "filesystem.write",
                ]
            )
            == EXIT_OK
        )
        text = out(capsys)
        assert "MIGRATION REPORT" in text
        assert "recorded migrated state" in text

    def test_migrate_blocked_exits_three(self, ran, capsys):
        head = json.loads(_history(ran, capsys))[-1]["digest"]
        assert (
            main(["migrate", head, "--store", str(ran), "--adapter", "other", "--dry-run"])
            == EXIT_CHECK_FAILED
        )
        assert "BLOCKED" in out(capsys)

    def test_migrate_dry_run_records_nothing(self, ran, capsys):
        before = len(json.loads(_history(ran, capsys)))
        main(["migrate", "final", "--store", str(ran), "--adapter", "o", "--dry-run"])
        capsys.readouterr()
        assert len(json.loads(_history(ran, capsys))) == before

    def test_capabilities_check_exit_codes(self, ran, capsys):
        assert (
            main(
                [
                    "capabilities",
                    "final",
                    "--store",
                    str(ran),
                    "--grant",
                    "filesystem.*",
                    "--grant",
                    "web.search",
                ]
            )
            == EXIT_OK
        )
        assert main(["capabilities", "final", "--store", str(ran)]) == EXIT_OK
        assert (
            main(["capabilities", "final", "--store", str(ran), "--grant", "nothing.useful"])
            == EXIT_CHECK_FAILED
        )

    def test_sanitize_clean_state_exits_zero(self, ran, capsys):
        assert main(["sanitize", "final", "--store", str(ran)]) == EXIT_OK
        assert "CLEAN" in out(capsys)

    def test_unknown_reference_is_an_error_not_a_crash(self, ran, capsys):
        from continuum.cli import EXIT_ERROR

        assert main(["inspect", "no-such-ref", "--store", str(ran)]) == EXIT_ERROR
        assert "error:" in capsys.readouterr().err


class TestDemo:
    def test_demo_runs_end_to_end(self, tmp_path, capsys):
        assert main(["demo", "--workspace", str(tmp_path / "demo")]) == EXIT_OK
        text = out(capsys)
        for expected in (
            "same digest, nothing written",
            "byte-identical",
            "MIGRATION REPORT",
            "SECRET SCAN",
            "RESUME REFUSED" if False else "CAPABILITY CHECK",
        ):
            assert expected in text

    def test_demo_is_reproducible_under_a_pinned_clock(self, tmp_path, capsys):
        """Same inputs and same clock must yield the same content addresses.

        The clock has to be pinned for this to be a real claim: timestamps feed
        into the state document, so digests legitimately vary with wall-clock
        time. What must not vary is anything else.
        """
        from continuum.model import fixed_clock

        def digests(text: str) -> list[str]:
            import re

            return re.findall(r"\b[0-9a-f]{12}\b", text)

        with fixed_clock():
            main(["demo", "--workspace", str(tmp_path / "a")])
            first = out(capsys)
            main(["demo", "--workspace", str(tmp_path / "b")])
            second = out(capsys)

        assert digests(first) == digests(second)
        assert len(digests(first)) > 5  # the demo really does print addresses


def _history(workspace, capsys) -> str:
    main(["history", "--store", str(workspace), "--json"])
    return capsys.readouterr().out
