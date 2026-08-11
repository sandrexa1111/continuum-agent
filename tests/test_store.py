"""Object store: content addressing, integrity, and the checkpoint graph."""

from __future__ import annotations

import zlib

import pytest

from continuum.errors import IntegrityError, StoreError
from continuum.model import Lineage
from continuum.store import Store


@pytest.fixture
def store(tmp_path):
    return Store.init(tmp_path)


class TestObjects:
    def test_put_then_get_returns_identical_bytes(self, store):
        address = store.put_bytes(b"hello world")
        assert store.get_bytes(address) == b"hello world"

    def test_identical_content_shares_one_object(self, store):
        first = store.put_bytes(b"same")
        second = store.put_bytes(b"same")
        assert first == second
        assert len(list(store.iter_objects())) == 1

    def test_different_content_yields_different_addresses(self, store):
        assert store.put_bytes(b"a") != store.put_bytes(b"b")

    def test_missing_object_is_an_error(self, store):
        with pytest.raises(StoreError, match="not in this store"):
            store.get_bytes("sha256:" + "0" * 64)

    def test_malformed_address_is_rejected(self, store):
        with pytest.raises(StoreError, match="not a valid content address"):
            store.get_bytes("nonsense")

    def test_tampered_object_is_detected_on_read(self, store):
        address = store.put_bytes(b"trustworthy")
        path = store._object_path(address)
        path.write_bytes(zlib.compress(b"tampered-with"))

        # Content addressing is only a guarantee if it is actually checked.
        with pytest.raises(IntegrityError, match="modified outside Continuum"):
            store.get_bytes(address)

    def test_corrupt_compression_is_detected(self, store):
        address = store.put_bytes(b"payload")
        store._object_path(address).write_bytes(b"not-zlib-at-all")
        with pytest.raises(IntegrityError, match="corrupt"):
            store.get_bytes(address)

    def test_verify_reports_broken_objects(self, store):
        good = store.put_bytes(b"good")
        bad = store.put_bytes(b"bad")
        store._object_path(bad).write_bytes(zlib.compress(b"swapped"))

        broken = store.verify()
        assert broken == [bad]
        assert good not in broken

    def test_verify_is_clean_on_an_untouched_store(self, store):
        for payload in (b"a", b"b", b"c"):
            store.put_bytes(payload)
        assert store.verify() == []

    def test_atomic_write_leaves_no_temp_files(self, store):
        store.put_bytes(b"x")
        leftovers = list(store.root.rglob(".tmp-*"))
        assert leftovers == []


class TestResolve:
    def test_unambiguous_prefix_resolves(self, store):
        address = store.put_bytes(b"resolve me")
        assert store.resolve(address.split(":")[1][:10]) == address

    def test_full_address_passes_through(self, store):
        address = store.put_bytes(b"x")
        assert store.resolve(address) == address

    def test_unknown_prefix_is_an_error(self, store):
        store.put_bytes(b"x")
        with pytest.raises(StoreError, match="no object matches"):
            store.resolve("ffffffffff")

    def test_ambiguous_prefix_is_refused_rather_than_guessed(self, store):
        # Silently picking one of two matching checkpoints would be the worst
        # available behaviour, so an empty prefix must fail loudly.
        store.put_bytes(b"one")
        store.put_bytes(b"two")
        with pytest.raises(StoreError, match="ambiguous"):
            store.resolve("")


class TestStates:
    def test_state_round_trips_through_the_store(self, store, state):
        address = store.put_state(state)
        assert store.get_state(address).digest() == state.digest()

    def test_record_checkpoint_indexes_and_moves_the_head(self, store, state):
        ref = store.record_checkpoint(state, label="first")
        assert ref.label == "first"
        assert store.head(state.identity.agent_id) == ref.digest
        assert [r.digest for r in store.checkpoints()] == [ref.digest]

    def test_recording_the_same_state_twice_does_not_duplicate(self, store, state):
        first = store.record_checkpoint(state, label="a")
        second = store.record_checkpoint(state, label="b")
        assert first.digest == second.digest
        assert len(store.checkpoints()) == 1

    def test_ancestry_walks_parent_links(self, store, state):
        root = store.put_state(state)
        child = state.replace(lineage=Lineage(parent=root, root=root, generation=1))
        child_digest = store.put_state(child)
        grandchild = child.replace(lineage=Lineage(parent=child_digest, root=root, generation=2))
        grandchild_digest = store.put_state(grandchild)

        assert store.ancestry(grandchild_digest) == [grandchild_digest, child_digest, root]

    def test_ancestry_of_a_root_is_just_itself(self, store, state):
        address = store.put_state(state)
        assert store.ancestry(address) == [address]

    def test_children_of_finds_forks(self, store, state):
        parent = store.record_checkpoint(state)
        for label in ("a", "b"):
            branch = state.replace(
                lineage=Lineage(parent=parent.digest, forked_from=parent.digest, fork_label=label)
            )
            store.record_checkpoint(branch, label=f"fork:{label}")
        assert len(store.children_of(parent.digest)) == 2


class TestDiscovery:
    def test_open_requires_an_initialized_store(self, tmp_path):
        with pytest.raises(StoreError, match="no Continuum store"):
            Store.open(tmp_path)

    def test_discover_walks_upward(self, tmp_path):
        Store.init(tmp_path)
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert Store.discover(nested).root == (tmp_path / ".continuum")

    def test_discover_fails_outside_any_store(self, tmp_path):
        with pytest.raises(StoreError, match="no Continuum store found"):
            Store.discover(tmp_path / "empty")

    def test_init_is_idempotent(self, tmp_path):
        first = Store.init(tmp_path)
        created = (first.root / "config.json").read_text()
        second = Store.init(tmp_path)
        assert (second.root / "config.json").read_text() == created

    def test_future_layout_version_is_refused(self, tmp_path):
        store = Store.init(tmp_path)
        (store.root / "config.json").write_text('{"layout_version": 99}')
        with pytest.raises(StoreError, match="newer than this build"):
            Store.open(tmp_path)
