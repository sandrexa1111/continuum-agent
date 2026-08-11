"""Exception hierarchy for Continuum.

Callers that only care about "did Continuum reject this?" can catch
:class:`ContinuumError`. Everything else is a refinement of that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for runtime
    from .capabilities import CompatibilityReport


class ContinuumError(Exception):
    """Base class for every error raised by Continuum."""


class FormatError(ContinuumError):
    """A state document or image is structurally invalid."""


class IntegrityError(FormatError):
    """Stored bytes do not match the digest that referenced them.

    Raised on image read and on object-store reads. This is the check that makes
    a ``.asi`` file self-verifying: a truncated download or an edited blob is
    detected before any of it reaches the caller.
    """


class VersionError(FormatError):
    """A state image declares a format version this build cannot read."""

    def __init__(self, found: str, supported: str) -> None:
        super().__init__(
            f"state image declares format_version {found!r}, "
            f"this build supports {supported!r} "
            f"(upgrade continuum-agent, or run `continuum migrate-format`)"
        )
        self.found = found
        self.supported = supported


class ResumeBlocked(ContinuumError):
    """The destination environment cannot satisfy the state's requirements.

    Carries the structured report so a caller can render it or decide to
    override with ``--allow-degraded``.
    """

    def __init__(self, report: CompatibilityReport) -> None:
        super().__init__(report.render())
        self.report = report


class AdapterError(ContinuumError):
    """A runtime adapter failed to export or import state."""


class StoreError(ContinuumError):
    """The on-disk object store is missing, unreadable, or inconsistent."""
