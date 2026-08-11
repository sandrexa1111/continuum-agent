"""Runtime adapters.

``base`` defines the protocol every runtime implements; ``native`` is the
model-free reference implementation used by the tests and the demo.

The built-in adapter registers itself here so that ``continuum adapters``
lists something useful on a fresh install. Third-party adapters are discovered
through the ``continuum.adapters`` entry-point group and must not need any
change to this file -- that is the interface test in ``spec/adapters.md``.
"""

from __future__ import annotations

from .base import ContinuumAdapter, RunnableAdapter, available, get, register
from .native import ADAPTER_NAME, NativeReviewAgent, ReviewPolicy, factory

register(ADAPTER_NAME, factory)

__all__ = [
    "ADAPTER_NAME",
    "ContinuumAdapter",
    "NativeReviewAgent",
    "ReviewPolicy",
    "RunnableAdapter",
    "available",
    "factory",
    "get",
    "register",
]
