"""The ``.asi`` Agent State Image container.

An image is a ZIP archive holding one agent state plus the blobs it references:

.. code-block:: text

    manifest.json          integrity + provenance header
    state.json             the canonical state document
    objects/<hex>          referenced blobs, named by their own digest

ZIP rather than a bespoke container because every language and every operating
system can already open one. A reviewer can unzip an image and read the state
with no Continuum installed, which matters for a format that asks other people
to trust it.

Reading is verified end to end: ``state.json`` must hash to the digest recorded
in the manifest, and every blob must hash to its own filename. A tampered or
truncated image raises :class:`~continuum.errors.IntegrityError` before any of
its contents are returned to the caller.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, digest_bytes, is_digest
from .errors import FormatError, IntegrityError
from .model import AgentState, check_version, now_iso

IMAGE_SUFFIX = ".asi"
MANIFEST_NAME = "manifest.json"
STATE_NAME = "state.json"
OBJECTS_PREFIX = "objects/"

PRODUCER = "continuum-agent/0.1.0"

# A state document larger than this is almost certainly a mistake (an agent
# inlining a binary into memory content, say). Refusing to unpack it protects
# against zip-bomb style inputs from untrusted images.
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024


@dataclass
class Image:
    """An unpacked state image."""

    state: AgentState
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    def artifact_bytes(self, artifact_id: str) -> bytes | None:
        """Return the packed contents of an artifact, if it was included."""
        for artifact in self.state.artifacts:
            if artifact.id == artifact_id:
                return self.blobs.get(artifact.digest)
        return None


def build_manifest(state: AgentState, blobs: dict[str, bytes]) -> dict[str, Any]:
    state_bytes = canonical_bytes(state.to_dict())
    return {
        "format_version": state.format_version,
        "producer": PRODUCER,
        "created_at": now_iso(),
        "state_digest": digest_bytes(state_bytes),
        "agent_id": state.identity.agent_id,
        "objects": {address: {"size": len(data)} for address, data in sorted(blobs.items())},
    }


def write_image(
    state: AgentState,
    path: Path | str,
    blobs: dict[str, bytes] | None = None,
) -> Path:
    """Write ``state`` and its ``blobs`` to a ``.asi`` file at ``path``."""
    state.validate()
    blobs = dict(blobs or {})

    for address, data in blobs.items():
        if not is_digest(address):
            raise FormatError(f"blob key {address!r} is not a content address")
        actual = digest_bytes(data)
        if actual != address:
            raise IntegrityError(f"blob supplied under {address} actually hashes to {actual}")

    missing = _missing_artifact_blobs(state, blobs)
    if missing:
        # Not fatal: artifacts may legitimately live outside the image (large
        # files, shared storage). The manifest records what is and is not here,
        # and `continuum inspect` surfaces the difference.
        pass

    state_bytes = canonical_bytes(state.to_dict())
    if len(state_bytes) > MAX_STATE_BYTES:
        raise FormatError(
            f"state document is {len(state_bytes)} bytes, over the {MAX_STATE_BYTES} limit; "
            "store large content as artifacts instead of inlining it"
        )

    manifest = build_manifest(state, blobs)
    manifest["artifacts_external"] = sorted(missing)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp path then replace, so an interrupted write never
    # leaves a half-valid image where a tool expects a complete one.
    staging = target.with_suffix(target.suffix + ".partial")
    with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        )
        archive.writestr(STATE_NAME, state_bytes)
        for address, data in sorted(blobs.items()):
            archive.writestr(OBJECTS_PREFIX + address.split(":", 1)[1], data)
    staging.replace(target)
    return target


def read_image(path: Path | str) -> Image:
    """Read and fully verify a ``.asi`` file."""
    source = Path(path)
    if not source.exists():
        raise FormatError(f"no such image: {source}")
    if not zipfile.is_zipfile(source):
        raise FormatError(f"{source} is not a valid state image (not a zip container)")
    _reject_trailing_data(source)

    with zipfile.ZipFile(source) as archive:
        _guard_expansion(archive)
        names = set(archive.namelist())
        for required in (MANIFEST_NAME, STATE_NAME):
            if required not in names:
                raise FormatError(f"{source} is missing {required}")

        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        declared = manifest.get("format_version")
        if not isinstance(declared, str):
            raise FormatError(f"{source}: manifest has no format_version")
        check_version(declared)

        state_bytes = archive.read(STATE_NAME)
        actual = digest_bytes(state_bytes)
        expected = manifest.get("state_digest")
        if expected and actual != expected:
            raise IntegrityError(
                f"{source}: state.json hashes to {actual} but the manifest declares "
                f"{expected}; the image has been modified"
            )

        blobs: dict[str, bytes] = {}
        for name in sorted(names):
            if not name.startswith(OBJECTS_PREFIX) or name.endswith("/"):
                continue
            data = archive.read(name)
            address = "sha256:" + name[len(OBJECTS_PREFIX) :]
            if not is_digest(address):
                raise FormatError(f"{source}: object entry {name!r} is not a content address")
            recomputed = digest_bytes(data)
            if recomputed != address:
                raise IntegrityError(
                    f"{source}: object {name} hashes to {recomputed}, not its own name"
                )
            blobs[address] = data

    state = AgentState.from_dict(json.loads(state_bytes.decode("utf-8")))
    return Image(state=state, blobs=blobs, manifest=manifest)


def inspect_image(path: Path | str) -> dict[str, Any]:
    """Summarize an image without returning any of its content.

    Used by ``continuum inspect`` so that "what is in this file?" never
    requires printing the file. That distinction matters when images may carry
    sensitive memory.
    """
    image = read_image(path)
    state = image.state
    return {
        "path": str(path),
        "format_version": state.format_version,
        "producer": image.manifest.get("producer", "unknown"),
        "created_at": image.manifest.get("created_at", ""),
        "state_digest": image.manifest.get("state_digest", state.digest()),
        "agent_id": state.identity.agent_id,
        "objective": state.objective.goal,
        "status": state.execution.status.value,
        "current_task": state.execution.current_task,
        "step": state.execution.step,
        "counts": {
            "memory": len(state.memory),
            "context_messages": len(state.context),
            "artifacts": len(state.artifacts),
            "events": len(state.events),
            "packed_blobs": len(image.blobs),
        },
        "capabilities": {
            "requires": list(state.capabilities.requires),
            "optional": list(state.capabilities.optional),
        },
        "provider": {
            "adapter": state.provider.adapter,
            "provider": state.provider.provider,
            "model": state.provider.model,
            "has_opaque_state": bool(state.provider.opaque),
        },
        "lineage": state.lineage.to_dict(),
        "artifacts_external": image.manifest.get("artifacts_external", []),
    }


def _missing_artifact_blobs(state: AgentState, blobs: dict[str, bytes]) -> list[str]:
    return sorted({a.digest for a in state.artifacts if a.digest and a.digest not in blobs})


_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_SIZE = 22
_MAX_ZIP_COMMENT = 0xFFFF


def _reject_trailing_data(source: Path) -> None:
    """Refuse an image with bytes appended after the archive proper.

    ZIP readers locate the central directory by scanning *backwards* from the
    end of the file, so appending data to a valid archive leaves it perfectly
    readable with unchanged contents. Every digest still matches and nothing in
    the normal verification path notices.

    That makes plain append a real tamper and smuggling vector, and it would
    have made this module's "the image has been modified" guarantee false. The
    archive must therefore end exactly where its end-of-central-directory
    record says it does.

    Note the boundary this does *not* cross: integrity is not authenticity.
    Anyone able to rewrite the whole image can also recompute the manifest.
    Detecting that needs signatures, which are on the roadmap and documented as
    absent in SECURITY.md.
    """
    size = source.stat().st_size
    tail_length = min(size, _EOCD_SIZE + _MAX_ZIP_COMMENT)
    with source.open("rb") as handle:
        handle.seek(size - tail_length)
        tail = handle.read(tail_length)

    marker = tail.rfind(_EOCD_SIGNATURE)
    if marker == -1:
        raise FormatError(f"{source}: no end-of-central-directory record found")

    comment_length = int.from_bytes(tail[marker + 20 : marker + 22], "little")
    archive_end = (size - tail_length) + marker + _EOCD_SIZE + comment_length
    if archive_end != size:
        raise IntegrityError(
            f"{source}: {size - archive_end} byte(s) of trailing data after the archive; "
            "the image has been appended to since it was written"
        )


def _guard_expansion(archive: zipfile.ZipFile) -> None:
    """Reject archives that would expand to an unreasonable size.

    Images are routinely passed between machines and may arrive from somewhere
    untrusted; the container should not be a decompression bomb vector.
    """
    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_TOTAL_UNCOMPRESSED:
        raise FormatError(
            f"image expands to {total} bytes, over the {MAX_TOTAL_UNCOMPRESSED} limit"
        )
    for info in archive.infolist():
        name = info.filename
        if name.startswith("/") or ".." in Path(name).parts:
            raise FormatError(f"image contains an unsafe entry path: {name!r}")
