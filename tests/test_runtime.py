"""The three verbs, exercised against the reference adapter.

These are the tests that decide whether the headline claim -- stop an agent,
move it, continue -- is true or marketing.
"""

from __future__ import annotations

import pytest

from continuum.adapters.native import PLAN, NativeReviewAgent
from continuum.errors import AdapterError, ResumeBlocked
from continuum.model import ExecutionStatus, fixed_clock
from continuum.runtime import checkout, checkpoint, fork, resume
from continuum.store import Store

GRANTS = ["filesystem.read", "filesystem.write"]


@pytest.fixture
def agent(workspace):
    return NativeReviewAgent(workspace, agent_id="reviewer")


@pytest.fixture
def store(workspace):
    return Store.init(workspace)


class TestReferenceAdapter:
    def test_export_state_is_repeatable(self, agent):
        # If exporting mints a timestamp or an id, every checkpoint looks like
        # a change and deduplication silently stops working.
        assert agent.export_state().digest() == agent.export_state().digest()

    def test_runs_the_whole_plan(self, agent):
        final = agent.run_to_completion()
        assert final.execution.status is ExecutionStatus.COMPLETED
        assert final.execution.step == len(PLAN)

    def test_produces_artifacts_with_a_derivation_edge(self, agent, workspace):
        final = agent.run_to_completion()
        ids = {a.id for a in final.artifacts}
        assert ids == {"report", "review"}

        review = next(a for a in final.artifacts if a.id == "review")
        assert review.derived_from == ["report"]
        assert (workspace / "artifacts" / "report.md").exists()

    def test_artifact_digests_match_the_files_on_disk(self, agent, workspace):
        from continuum.canonical import digest_bytes

        final = agent.run_to_completion()
        for artifact in final.artifacts:
            assert digest_bytes((workspace / artifact.path).read_bytes()) == artifact.digest

    def test_is_deterministic_across_runs(self, workspace):
        """Same corpus and same clock must produce the same content address.

        The clock has to be pinned. The agent stamps ``created_at`` on memory
        entries as it runs, so two runs that straddle a second boundary
        legitimately differ. Without the pin this passes on a fast machine and
        fails on a slow one -- which is exactly what it did, green on Linux and
        Windows and red on one macOS runner.
        """
        with fixed_clock():
            first = NativeReviewAgent(workspace, agent_id="r").run_to_completion()
            second = NativeReviewAgent(workspace, agent_id="r").run_to_completion()
        assert first.core_digest() == second.core_digest()

    def test_only_timestamps_vary_between_unpinned_runs(self, workspace):
        """Everything the agent actually decides is clock-independent."""
        first = NativeReviewAgent(workspace, agent_id="r").run_to_completion()
        second = NativeReviewAgent(workspace, agent_id="r").run_to_completion()

        assert [m.content for m in first.memory] == [m.content for m in second.memory]
        assert [a.digest for a in first.artifacts] == [a.digest for a in second.artifacts]
        assert [e.type for e in first.events] == [e.type for e in second.events]

    def test_missing_corpus_is_a_clear_adapter_error(self, tmp_path):
        with pytest.raises(AdapterError, match="no corpus"):
            NativeReviewAgent(tmp_path).step()

    def test_import_requires_granted_capabilities(self, agent, workspace):
        state = agent.export_state()
        stripped = state.replace(
            capabilities=state.capabilities.__class__(
                requires=state.capabilities.requires, granted=[]
            )
        )
        with pytest.raises(AdapterError, match="cannot run without"):
            NativeReviewAgent(workspace).import_state(stripped)

    def test_import_accepts_a_namespace_grant(self, agent, workspace):
        state = agent.export_state()
        wildcarded = state.replace(
            capabilities=state.capabilities.__class__(
                requires=state.capabilities.requires, granted=["filesystem.*"]
            )
        )
        NativeReviewAgent(workspace).import_state(wildcarded)  # must not raise


class TestCheckpoint:
    def test_first_checkpoint_is_a_root(self, agent, store):
        ref = checkpoint(agent, store, label="start")
        assert ref.parent is None
        assert store.get_state(ref.digest).lineage.generation == 0

    def test_checkpointing_an_idle_agent_is_a_no_op(self, agent, store):
        first = checkpoint(agent, store)
        second = checkpoint(agent, store)
        assert first.digest == second.digest
        assert len(store.checkpoints()) == 1

    def test_checkpoints_chain_as_the_agent_advances(self, agent, store):
        first = checkpoint(agent, store, label="a")
        agent.step()
        second = checkpoint(agent, store, label="b")
        agent.step()
        third = checkpoint(agent, store, label="c")

        assert second.parent == first.digest
        assert third.parent == second.digest
        assert store.ancestry(third.digest) == [third.digest, second.digest, first.digest]

    def test_generation_increments_along_the_chain(self, agent, store):
        checkpoint(agent, store)
        agent.step()
        second = checkpoint(agent, store)
        assert store.get_state(second.digest).lineage.generation == 1

    def test_head_follows_the_latest_checkpoint(self, agent, store):
        checkpoint(agent, store)
        agent.step()
        latest = checkpoint(agent, store)
        assert store.head("reviewer") == latest.digest


class TestFork:
    def test_creates_one_branch_per_label(self, agent, store):
        checkpoint(agent, store)
        agent.step()
        state = agent.export_state()

        branches = fork(state, store, ["a", "b", "c"])
        assert [b.label for b in branches] == ["a", "b", "c"]
        assert len({b.digest for b in branches}) == 3

    def test_branches_record_their_origin(self, agent, store):
        state = agent.export_state()
        parent = store.put_state(state)
        for branch in fork(state, store, ["x", "y"]):
            assert branch.state.lineage.forked_from == parent
            assert branch.state.lineage.parent == parent
            assert branch.state.lineage.fork_label == branch.label

    def test_branches_are_isolated(self, workspace, agent, store):
        """Running one branch must not change another."""
        agent.step()
        agent.step()
        state = agent.export_state()
        branches = fork(state, store, ["severity", "length"])

        before = {b.label: b.state.digest() for b in branches}
        first = branches[0]
        runner, _ = resume(first.state, NativeReviewAgent, granted=GRANTS, workspace=workspace)
        runner.run_to_completion()

        after = store.get_state(branches[1].digest).digest()
        assert after == before["length"]

    def test_forking_shares_storage_with_the_parent(self, agent, store):
        """A fork should cost a delta, not a full copy."""
        state = agent.export_state()
        store.put_state(state)
        objects_before = len(list(store.iter_objects()))

        fork(state, store, [f"b{i}" for i in range(8)])
        objects_after = len(list(store.iter_objects()))

        # Eight branches add eight small state objects, not eight corpora.
        assert objects_after - objects_before == 8

    def test_duplicate_labels_are_refused(self, agent, store):
        with pytest.raises(ValueError, match="unique"):
            fork(agent.export_state(), store, ["same", "same"])

    def test_empty_label_list_is_refused(self, agent, store):
        with pytest.raises(ValueError, match="at least one"):
            fork(agent.export_state(), store, [])

    def test_head_returns_to_the_fork_point(self, agent, store):
        checkpoint(agent, store)
        state = agent.export_state()
        parent = store.put_state(state)
        fork(state, store, ["a", "b"])
        # After a fan-out there is no single current branch.
        assert store.head("reviewer") == parent


class TestResume:
    def test_resumes_and_finishes_the_remaining_plan(self, workspace, agent, tmp_path):
        agent.step()
        agent.step()
        mid = agent.export_state()

        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "corpus").mkdir(parents=True)
        for path in (workspace / "corpus").iterdir():
            (elsewhere / "corpus" / path.name).write_bytes(path.read_bytes())

        moved, report = resume(mid, NativeReviewAgent, granted=GRANTS, workspace=elsewhere)
        assert report.ok
        final = moved.run_to_completion()

        assert final.execution.status is ExecutionStatus.COMPLETED
        assert (elsewhere / "artifacts" / "report.md").exists()

    def test_resume_from_an_image_file(self, workspace, agent, tmp_path):
        from continuum.image import write_image

        agent.step()
        path = write_image(agent.export_state(), tmp_path / "a.asi")
        moved, report = resume(path, NativeReviewAgent, granted=GRANTS, workspace=workspace)
        assert report.ok
        assert moved.export_state().execution.step == 1

    def test_missing_required_capability_blocks_resume(self, workspace, agent):
        with pytest.raises(ResumeBlocked) as exc:
            resume(
                agent.export_state(),
                NativeReviewAgent,
                granted=["filesystem.read"],
                workspace=workspace,
            )
        assert exc.value.report.missing_required == ["filesystem.write"]

    def test_allow_degraded_lifts_the_core_gate_but_not_the_adapter(self, workspace, agent):
        """The override is not a magic wand.

        Continuum stops refusing, but an adapter with no reduced mode still
        refuses -- and says so, rather than starting an agent that would fail
        at its first write.
        """
        with pytest.raises(AdapterError, match="cannot give a runtime a capability"):
            resume(
                agent.export_state(),
                NativeReviewAgent,
                granted=["filesystem.read"],
                allow_degraded=True,
                workspace=workspace,
            )

    def test_allow_degraded_succeeds_for_an_adapter_that_can_degrade(self, workspace, agent):
        class TolerantAdapter:
            name = "tolerant"

            def __init__(self, **_: object) -> None:
                self.state = None

            def export_state(self):
                return self.state

            def import_state(self, state) -> None:
                self.state = state  # accepts whatever it is given

        adapter, report = resume(
            agent.export_state(),
            TolerantAdapter,
            granted=["filesystem.read"],
            allow_degraded=True,
            workspace=workspace,
        )
        assert not report.ok
        assert report.missing_required == ["filesystem.write"]
        assert adapter.state is not None

    def test_missing_optional_capability_does_not_block(self, workspace, agent):
        _, report = resume(
            agent.export_state(), NativeReviewAgent, granted=GRANTS, workspace=workspace
        )
        assert report.ok
        assert report.missing_optional == ["web.search"]

    def test_wildcard_grant_satisfies_the_namespace(self, workspace, agent):
        _, report = resume(
            agent.export_state(),
            NativeReviewAgent,
            granted=["filesystem.*"],
            workspace=workspace,
        )
        assert report.ok

    def test_resume_records_an_event(self, workspace, agent):
        moved, _ = resume(
            agent.export_state(), NativeReviewAgent, granted=GRANTS, workspace=workspace
        )
        assert moved.export_state().events[-1].type == "checkpoint.resumed"

    def test_suspended_state_becomes_running(self, workspace, agent):
        moved, _ = resume(
            agent.export_state(), NativeReviewAgent, granted=GRANTS, workspace=workspace
        )
        assert moved.export_state().execution.status is ExecutionStatus.RUNNING


class TestCheckout:
    def test_by_full_digest(self, agent, store):
        ref = checkpoint(agent, store, label="start")
        assert checkout(store, ref.digest).digest() == ref.digest

    def test_by_label(self, agent, store):
        ref = checkpoint(agent, store, label="milestone")
        assert checkout(store, "milestone").digest() == ref.digest

    def test_by_fork_label(self, agent, store):
        state = agent.export_state()
        branches = fork(state, store, ["candidate"])
        assert checkout(store, "candidate").digest() == branches[0].digest

    def test_unknown_reference_is_a_clear_error(self, agent, store):
        checkpoint(agent, store)
        from continuum.errors import StoreError

        with pytest.raises(StoreError, match="not a known checkpoint"):
            checkout(store, "no-such-thing")
