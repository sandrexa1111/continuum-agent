"""On-disk content-addressed object store and checkpoint graph.

The store is the local half of Continuum: a place to keep many related agent
states cheaply and to record how they descend from each other. A ``.asi`` image
(:mod:`continuum.image`) is the transport half -- one state, self-contained,
movable between machines.

Design notes worth defending:

**Objects are immutable and addressed by the digest of their uncompressed
bytes.** Compression is a storage detail; if the address depended on it, two
stores using different zlib levels would disagree about identity.

**Writes are atomic.** Every object lands via a temporary file and a rename
within the same directory. A store interrupted mid-write is missing an object,
which is recoverable; a store containing a half-written object under a valid
address is not.

**The checkpoint graph is metadata, not truth.** Ancestry is also recorded
inside each state's ``lineage`` and is therefore verifiable from the objects
alone. ``refs/`` is an index that makes traversal fast, and it can be rebuilt.
"""

from __future__ import annotations

import json
import os
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, digest_bytes, is_digest
from .errors import IntegrityError, StoreError
from .model import AgentState, now_iso

STORE_DIRNAME = ".continuum"
_LAYOUT_VERSION = 1
_COMPRESS_LEVEL = 6


@dataclass(frozen=True)
class CheckpointRef:
    """An entry in the checkpoint index."""

    digest: str
    agent_id: str
    created_at: str
    label: str = ""
    parent: str | None = None
    forked_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "label": self.label,
            "parent": self.parent,
            "forked_from": self.forked_from,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointRef:
        return cls(
            digest=data["digest"],
            agent_id=data.get("agent_id", ""),
            created_at=data.get("created_at", ""),
            label=data.get("label", ""),
            parent=data.get("parent"),
            forked_from=data.get("forked_from"),
        )


class Store:
    """A ``.continuum`` directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.refs_dir = self.root / "refs"

    # -- lifecycle -----------------------------------------------------

    @classmethod
    def init(cls, workdir: Path | str) -> Store:
        """Create (or adopt) a store under ``workdir``."""
        root = Path(workdir) / STORE_DIRNAME
        store = cls(root)
        store.objects_dir.mkdir(parents=True, exist_ok=True)
        store.refs_dir.mkdir(parents=True, exist_ok=True)
        config = store.root / "config.json"
        if not config.exists():
            _write_atomic(
                config,
                json.dumps({"layout_version": _LAYOUT_VERSION, "created_at": now_iso()}).encode(),
            )
        return store

    @classmethod
    def open(cls, workdir: Path | str) -> Store:
        root = Path(workdir) / STORE_DIRNAME
        if not (root / "config.json").exists():
            raise StoreError(f"no Continuum store at {root} (run `continuum init`)")
        store = cls(root)
        store._check_layout()
        return store

    @classmethod
    def discover(cls, start: Path | str | None = None) -> Store:
        """Find the nearest store at or above ``start``.

        Mirrors how ``git`` locates its repository. Without this, every command
        would have to be run from exactly the right directory.
        """
        current = Path(start or Path.cwd()).resolve()
        for candidate in [current, *current.parents]:
            if (candidate / STORE_DIRNAME / "config.json").exists():
                return cls.open(candidate)
        raise StoreError(
            f"no Continuum store found in {current} or any parent (run `continuum init`)"
        )

    def _check_layout(self) -> None:
        config = json.loads((self.root / "config.json").read_text("utf-8"))
        found = config.get("layout_version", 0)
        if found > _LAYOUT_VERSION:
            raise StoreError(
                f"store layout version {found} is newer than this build supports "
                f"({_LAYOUT_VERSION}); upgrade continuum-agent"
            )

    # -- objects -------------------------------------------------------

    def _object_path(self, digest: str) -> Path:
        if not is_digest(digest):
            raise StoreError(f"{digest!r} is not a valid content address")
        hexpart = digest.split(":", 1)[1]
        # Two-character fan-out keeps directory sizes sane on filesystems that
        # degrade with very wide directories.
        return self.objects_dir / hexpart[:2] / hexpart[2:]

    def has(self, digest: str) -> bool:
        return self._object_path(digest).exists()

    def put_bytes(self, data: bytes) -> str:
        """Store ``data`` and return its content address.

        Idempotent: storing identical bytes twice is a no-op, which is exactly
        what makes forking cheap -- forks share every unchanged object.
        """
        address = digest_bytes(data)
        path = self._object_path(address)
        if path.exists():
            return address
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, zlib.compress(data, _COMPRESS_LEVEL))
        return address

    def get_bytes(self, digest: str) -> bytes:
        """Load an object, verifying it still hashes to its address."""
        path = self._object_path(digest)
        if not path.exists():
            raise StoreError(f"object {digest} is not in this store")
        try:
            data = zlib.decompress(path.read_bytes())
        except zlib.error as exc:
            raise IntegrityError(f"object {digest} is corrupt: {exc}") from exc
        actual = digest_bytes(data)
        if actual != digest:
            raise IntegrityError(
                f"object at {path} hashes to {actual}, expected {digest}; "
                "the store has been modified outside Continuum"
            )
        return data

    def iter_objects(self) -> Iterator[str]:
        if not self.objects_dir.exists():
            return
        for prefix_dir in sorted(self.objects_dir.iterdir()):
            if not prefix_dir.is_dir():
                continue
            for obj in sorted(prefix_dir.iterdir()):
                yield f"sha256:{prefix_dir.name}{obj.name}"

    # -- states --------------------------------------------------------

    def put_state(self, state: AgentState) -> str:
        state.validate()
        return self.put_bytes(canonical_bytes(state.to_dict()))

    def get_state(self, digest: str) -> AgentState:
        return AgentState.from_dict(json.loads(self.get_bytes(digest).decode("utf-8")))

    def resolve(self, prefix: str) -> str:
        """Expand an unambiguous digest prefix to a full address.

        Convenience for the CLI only. Ambiguity is an error rather than a
        best-effort guess: silently picking one of two matching checkpoints
        would be the worst possible failure mode here.
        """
        if is_digest(prefix):
            return prefix
        needle = prefix.split(":", 1)[-1]
        matches = [d for d in self.iter_objects() if d.split(":", 1)[1].startswith(needle)]
        if not matches:
            raise StoreError(f"no object matches {prefix!r}")
        if len(matches) > 1:
            raise StoreError(
                f"{prefix!r} is ambiguous, matches {len(matches)} objects; use more characters"
            )
        return matches[0]

    # -- checkpoint index ----------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.refs_dir / "checkpoints.json"

    def checkpoints(self) -> list[CheckpointRef]:
        if not self._index_path.exists():
            return []
        raw = json.loads(self._index_path.read_text("utf-8"))
        return [CheckpointRef.from_dict(entry) for entry in raw]

    def record_checkpoint(self, state: AgentState, label: str = "") -> CheckpointRef:
        """Store ``state`` and add it to the checkpoint index."""
        address = self.put_state(state)
        existing = self.checkpoints()
        for ref in existing:
            if ref.digest == address:
                # Re-checkpointing unchanged state is a no-op, not a duplicate
                # index entry. Idempotence matters for retry loops.
                return ref
        ref = CheckpointRef(
            digest=address,
            agent_id=state.identity.agent_id,
            created_at=now_iso(),
            label=label,
            parent=state.lineage.parent,
            forked_from=state.lineage.forked_from,
        )
        _write_atomic(
            self._index_path,
            json.dumps([r.to_dict() for r in [*existing, ref]], indent=2).encode("utf-8"),
        )
        self.set_head(state.identity.agent_id, address)
        return ref

    def find_checkpoint(self, digest: str) -> CheckpointRef | None:
        for ref in self.checkpoints():
            if ref.digest == digest:
                return ref
        return None

    def children_of(self, digest: str) -> list[CheckpointRef]:
        return [r for r in self.checkpoints() if r.parent == digest]

    def ancestry(self, digest: str) -> list[str]:
        """Walk parent links from ``digest`` back to a root, newest first.

        Reads ancestry from the stored states rather than the index so the
        answer stays correct even if ``refs/`` is stale or was rebuilt.
        """
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = digest
        while current:
            if current in seen:
                raise StoreError(f"lineage cycle detected at {current}")
            seen.add(current)
            chain.append(current)
            if not self.has(current):
                break
            current = self.get_state(current).lineage.parent
        return chain

    # -- heads ---------------------------------------------------------

    @property
    def _heads_path(self) -> Path:
        return self.refs_dir / "heads.json"

    def heads(self) -> dict[str, str]:
        if not self._heads_path.exists():
            return {}
        loaded: dict[str, str] = json.loads(self._heads_path.read_text("utf-8"))
        return loaded

    def head(self, agent_id: str) -> str | None:
        return self.heads().get(agent_id)

    def set_head(self, agent_id: str, digest: str) -> None:
        heads = self.heads()
        heads[agent_id] = digest
        _write_atomic(self._heads_path, json.dumps(heads, indent=2, sort_keys=True).encode("utf-8"))

    # -- maintenance ---------------------------------------------------

    def verify(self) -> list[str]:
        """Re-hash every object. Returns the addresses that failed.

        A store is not much use as a provenance record if nobody ever checks
        it, so this is exposed as ``continuum verify`` rather than buried.
        """
        broken: list[str] = []
        for address in self.iter_objects():
            try:
                self.get_bytes(address)
            except (IntegrityError, StoreError):
                broken.append(address)
        return broken


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a same-directory temp file and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Leaving a stray temp file behind is better than leaving a partial
        # object at a real address, but there is no reason to leave either.
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
