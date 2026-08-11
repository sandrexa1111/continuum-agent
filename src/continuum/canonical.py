"""Canonical serialization and content addressing.

Everything Continuum stores is addressed by the SHA-256 of its *canonical*
encoding, so the same logical state always produces the same identifier. That
one property is what makes checkpoints deduplicate, forks cheap, and diffs
trustworthy -- and it only holds if encoding is genuinely deterministic.

Canonical form (see ``spec/state-image.md`` for the normative statement):

* UTF-8, no byte-order mark
* object keys sorted by their Unicode code points
* no insignificant whitespace
* non-ASCII characters emitted literally rather than ``\\u``-escaped
* ``NaN``/``Infinity`` rejected -- they have no JSON representation and would
  silently become ``null`` in most other parsers

Floats are emitted with Python's shortest round-tripping repr, which agrees
with ECMA-262 ``Number::toString`` for every value we can represent. Integers
outside the IEEE-754 exact range are still encoded exactly, so a JavaScript
reader can lose precision on them; state documents therefore keep counters and
timestamps as integers well inside 2**53. This is a documented boundary, not an
accident.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

DIGEST_ALGORITHM = "sha256"
_DIGEST_PREFIX = f"{DIGEST_ALGORITHM}:"

# Length of a hex-encoded SHA-256 digest.
_HEX_LEN = 64


def _reject_non_finite(value: float) -> str:
    raise ValueError(
        f"non-finite float {value!r} cannot be canonically encoded; "
        "represent it as null or a string before checkpointing"
    )


class _CanonicalEncoder(json.JSONEncoder):
    """JSON encoder that refuses anything without a stable encoding."""

    def default(self, o: Any) -> Any:
        raise TypeError(
            f"{type(o).__name__} has no canonical JSON encoding; "
            "convert it to a primitive in the model layer first"
        )


def canonical_json(obj: Any) -> str:
    """Return the canonical JSON text for ``obj``."""
    _assert_encodable(obj)
    return json.dumps(
        obj,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        cls=_CanonicalEncoder,
    )


def canonical_bytes(obj: Any) -> bytes:
    """Return the canonical UTF-8 bytes for ``obj``."""
    return canonical_json(obj).encode("utf-8")


def _assert_encodable(obj: Any, _path: str = "$") -> None:
    """Walk ``obj`` and reject anything that would encode ambiguously.

    Done as an explicit pre-pass rather than inside the encoder so the error
    message can name the offending path -- debugging "somewhere in this 4 MB
    state document there is a NaN" is otherwise miserable.
    """
    if obj is None or isinstance(obj, (str, bool)):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _reject_non_finite(obj)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"{_path}: object keys must be strings, got {type(key).__name__}; "
                    "non-string keys have no defined ordering"
                )
            _assert_encodable(value, f"{_path}.{key}")
        return
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            _assert_encodable(value, f"{_path}[{index}]")
        return
    raise TypeError(f"{_path}: {type(obj).__name__} has no canonical JSON encoding")


def digest_bytes(data: bytes) -> str:
    """Return the prefixed content address of raw ``data``."""
    return _DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    """Return the prefixed content address of ``obj``'s canonical encoding."""
    return digest_bytes(canonical_bytes(obj))


def is_digest(value: str) -> bool:
    """Return True if ``value`` is a well-formed content address."""
    if not value.startswith(_DIGEST_PREFIX):
        return False
    hexpart = value[len(_DIGEST_PREFIX) :]
    return len(hexpart) == _HEX_LEN and all(c in "0123456789abcdef" for c in hexpart)


def short(value: str, length: int = 12) -> str:
    """Abbreviate a digest for human-facing output.

    Purely cosmetic: short forms are never used to look objects up, because
    truncated addresses reintroduce the collision risk content addressing
    exists to remove.
    """
    if value.startswith(_DIGEST_PREFIX):
        return value[len(_DIGEST_PREFIX) : len(_DIGEST_PREFIX) + length]
    return value[:length]
