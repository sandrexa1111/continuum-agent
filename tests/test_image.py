"""``.asi`` images: round trip, tamper detection, and container safety.

An image is the artifact people will actually exchange. Its verification path
is therefore treated as a security boundary, not a convenience.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from continuum.canonical import canonical_bytes, digest_bytes
from continuum.errors import FormatError, IntegrityError, VersionError
from continuum.image import inspect_image, read_image, write_image


class TestRoundTrip:
    def test_state_survives_a_round_trip_unchanged(self, state, tmp_path):
        path = write_image(state, tmp_path / "agent.asi")
        assert read_image(path).state.digest() == state.digest()

    def test_blobs_survive_a_round_trip(self, state, tmp_path):
        payload = b"# research\n\nfindings\n"
        blobs = {digest_bytes(payload): payload}
        path = write_image(state, tmp_path / "agent.asi", blobs)

        image = read_image(path)
        assert image.blobs == blobs

    def test_artifact_bytes_are_retrievable_by_id(self, state, tmp_path):
        payload = b"report body"
        artifact = state.artifacts[0].__class__(
            id="report", path="artifacts/report.md", digest=digest_bytes(payload)
        )
        packaged = state.replace(artifacts=[artifact])
        path = write_image(packaged, tmp_path / "a.asi", {digest_bytes(payload): payload})

        assert read_image(path).artifact_bytes("report") == payload

    def test_writing_is_atomic(self, state, tmp_path):
        path = write_image(state, tmp_path / "agent.asi")
        assert path.exists()
        assert not (tmp_path / "agent.asi.partial").exists()

    def test_image_is_a_plain_zip_readable_without_continuum(self, state, tmp_path):
        # A format nobody can open without our library is a format nobody will
        # trust. Verify the container is genuinely standard.
        path = write_image(state, tmp_path / "agent.asi")
        with zipfile.ZipFile(path) as archive:
            document = json.loads(archive.read("state.json"))
        assert document["identity"]["agent_id"] == state.identity.agent_id


class TestIntegrity:
    def _rewrite(self, path, member, data):
        """Rebuild an archive with one member replaced."""
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries[member] = data
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)

    def test_modified_state_is_detected(self, state, tmp_path):
        path = write_image(state, tmp_path / "agent.asi")
        document = state.to_dict()
        document["objective"]["goal"] = "something the author never wrote"
        self._rewrite(path, "state.json", canonical_bytes(document))

        with pytest.raises(IntegrityError, match="has been modified"):
            read_image(path)

    def test_modified_blob_is_detected(self, state, tmp_path):
        payload = b"original"
        address = digest_bytes(payload)
        path = write_image(state, tmp_path / "agent.asi", {address: payload})
        self._rewrite(path, "objects/" + address.split(":")[1], b"swapped")

        with pytest.raises(IntegrityError, match="not its own name"):
            read_image(path)

    def test_appended_bytes_are_rejected(self, state, tmp_path):
        """Regression: appending to a ZIP leaves it perfectly readable.

        ZIP readers scan backwards from the end of the file for the central
        directory, so trailing bytes change nothing they look at -- every digest
        still matched and verification passed. Caught by the CI first-run job,
        which appends to an image and asserts it is refused.
        """
        path = write_image(state, tmp_path / "agent.asi")
        with path.open("ab") as handle:
            handle.write(b"smuggled payload")

        with pytest.raises(IntegrityError, match="trailing data"):
            read_image(path)

    def test_untouched_image_has_no_trailing_data(self, state, tmp_path):
        path = write_image(state, tmp_path / "agent.asi")
        read_image(path)  # must not raise

    def test_truncated_file_is_rejected(self, state, tmp_path):
        path = write_image(state, tmp_path / "agent.asi")
        path.write_bytes(path.read_bytes()[: len(path.read_bytes()) // 2])
        with pytest.raises(FormatError, match="not a valid state image"):
            read_image(path)

    def test_missing_state_member_is_rejected(self, state, tmp_path):
        path = tmp_path / "broken.asi"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format_version": "0.1"}))
        with pytest.raises(FormatError, match=r"missing state\.json"):
            read_image(path)

    def test_blob_supplied_under_the_wrong_address_is_refused_at_write_time(self, state, tmp_path):
        with pytest.raises(IntegrityError, match="actually hashes to"):
            write_image(state, tmp_path / "a.asi", {"sha256:" + "0" * 64: b"mismatch"})

    def test_nonexistent_path_is_a_clear_error(self, tmp_path):
        with pytest.raises(FormatError, match="no such image"):
            read_image(tmp_path / "absent.asi")


class TestContainerSafety:
    def test_path_traversal_entries_are_rejected(self, state, tmp_path):
        path = tmp_path / "evil.asi"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format_version": "0.1"}))
            archive.writestr("state.json", canonical_bytes(state.to_dict()))
            archive.writestr("../../escaped.txt", b"nope")

        with pytest.raises(FormatError, match="unsafe entry path"):
            read_image(path)

    def test_absolute_entry_paths_are_rejected(self, state, tmp_path):
        path = tmp_path / "evil.asi"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format_version": "0.1"}))
            archive.writestr("state.json", canonical_bytes(state.to_dict()))
            archive.writestr("/etc/passwd", b"nope")

        with pytest.raises(FormatError, match="unsafe entry path"):
            read_image(path)

    def test_future_format_version_is_refused(self, state, tmp_path):
        path = write_image(state, tmp_path / "a.asi")
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(entries["manifest.json"])
        manifest["format_version"] = "9.0"
        entries["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)

        with pytest.raises(VersionError):
            read_image(path)


class TestInspect:
    def test_inspect_summarizes_without_returning_content(self, state, tmp_path):
        path = write_image(state, tmp_path / "a.asi")
        summary = inspect_image(path)

        assert summary["agent_id"] == "analyst-7"
        assert summary["counts"]["memory"] == 2
        assert summary["provider"]["has_opaque_state"] is True

        # No memory or message text may appear in a summary; `inspect` is what
        # people run on an image they do not trust.
        rendered = json.dumps(summary)
        assert "supplier invoice 4471" not in rendered
        assert "Pulling the invoice now" not in rendered

    def test_external_artifacts_are_reported(self, state, tmp_path):
        path = write_image(state, tmp_path / "a.asi")  # artifacts not packed
        assert len(inspect_image(path)["artifacts_external"]) == 2

    def test_packed_artifacts_are_not_reported_as_external(self, state, tmp_path):
        blobs = {a.digest: b"x" for a in state.artifacts}
        # Digests in the fixture are placeholders, so rebuild them honestly.
        artifacts = [
            a.__class__(
                id=a.id, path=a.path, digest=digest_bytes(b"x"), derived_from=a.derived_from
            )
            for a in state.artifacts
        ]
        packaged = state.replace(artifacts=artifacts)
        path = write_image(packaged, tmp_path / "a.asi", {digest_bytes(b"x"): b"x"})
        assert inspect_image(path)["artifacts_external"] == []
        assert blobs  # fixture sanity
