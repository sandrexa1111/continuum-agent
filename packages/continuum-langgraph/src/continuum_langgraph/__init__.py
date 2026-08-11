"""Continuum adapter for LangGraph.

Installed separately from ``continuum-agent`` on purpose. Continuum core has
zero runtime dependencies, and a framework integration must not be the thing
that changes that -- ``langgraph`` and its transitive tree live here, behind
this package boundary.

    pip install continuum-langgraph

Registered through the ``continuum.adapters`` entry point, so it appears in
``continuum adapters`` with no change to Continuum core. That is the point of
the exercise.
"""

from __future__ import annotations

from .adapter import ADAPTER_NAME, REQUIRED_CAPABILITIES, LangGraphAdapter, factory
from .binding import ExportedFields, GraphBinding, GraphFingerprint

__version__ = "0.1.0"

__all__ = [
    "ADAPTER_NAME",
    "REQUIRED_CAPABILITIES",
    "ExportedFields",
    "GraphBinding",
    "GraphFingerprint",
    "LangGraphAdapter",
    "__version__",
    "factory",
]
