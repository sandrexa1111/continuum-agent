"""Continuum -- portable execution state for long-lived AI agents.

Checkpoint a running agent, move the resulting image somewhere else, and
resume it there. The pieces:

* :mod:`continuum.model` -- what "agent state" means, as versioned data
* :mod:`continuum.image` -- the ``.asi`` container, verified on read
* :mod:`continuum.store` -- content-addressed local storage and the fork graph
* :mod:`continuum.migrate` -- moving state between providers, with an honest
  report of what survived
* :mod:`continuum.adapters` -- the interface a runtime implements to take part

API stability: **experimental**. Everything below 1.0 may change; the on-disk
format is versioned separately and is the part we intend to keep readable.
See ``spec/compatibility.md``.
"""

from __future__ import annotations

from .canonical import canonical_bytes, canonical_json, digest
from .capabilities import CompatibilityReport, check_capabilities
from .compaction import CompactionResult, compact_context
from .diff import StateDiff, diff_states
from .errors import (
    AdapterError,
    ContinuumError,
    FormatError,
    IntegrityError,
    ResumeBlocked,
    StoreError,
    VersionError,
)
from .image import IMAGE_SUFFIX, Image, inspect_image, read_image, write_image
from .migrate import MigrationReport, MigrationTarget, migrate
from .model import (
    FORMAT_VERSION,
    AgentState,
    Artifact,
    Capabilities,
    Environment,
    Event,
    Execution,
    ExecutionStatus,
    Identity,
    Lineage,
    MemoryEntry,
    MemoryKind,
    Message,
    Objective,
    Provider,
)
from .redact import RedactionReport, sanitize_state
from .runtime import checkpoint, fork, resume
from .store import CheckpointRef, Store

__version__ = "0.1.0"

__all__ = [
    "FORMAT_VERSION",
    "IMAGE_SUFFIX",
    "AdapterError",
    "AgentState",
    "Artifact",
    "Capabilities",
    "CheckpointRef",
    "CompactionResult",
    "CompatibilityReport",
    "ContinuumError",
    "Environment",
    "Event",
    "Execution",
    "ExecutionStatus",
    "FormatError",
    "Identity",
    "Image",
    "IntegrityError",
    "Lineage",
    "MemoryEntry",
    "MemoryKind",
    "Message",
    "MigrationReport",
    "MigrationTarget",
    "Objective",
    "Provider",
    "RedactionReport",
    "ResumeBlocked",
    "StateDiff",
    "Store",
    "StoreError",
    "VersionError",
    "__version__",
    "canonical_bytes",
    "canonical_json",
    "check_capabilities",
    "checkpoint",
    "compact_context",
    "diff_states",
    "digest",
    "fork",
    "inspect_image",
    "migrate",
    "read_image",
    "resume",
    "sanitize_state",
    "write_image",
]
