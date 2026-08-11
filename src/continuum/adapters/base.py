"""The adapter interface -- how a runtime joins the Continuum ecosystem.

An adapter is the only thing a framework has to write to become checkpointable.
It answers two questions: *what is your state right now* and *here is a state,
become it*. Everything else in Continuum -- storage, images, forking, diffing,
migration -- operates on the returned :class:`~continuum.model.AgentState` and
never touches the runtime.

That boundary is the project's central design test (``spec/adapters.md``): a
third party must be able to publish an adapter as a separate package without
patching Continuum. Discovery therefore goes through the standard entry-point
group ``continuum.adapters``:

.. code-block:: toml

    [project.entry-points."continuum.adapters"]
    my-framework = "my_package.continuum_adapter:MyAdapter"

Implementations are checked structurally rather than by inheritance, so an
existing class can satisfy the protocol without importing Continuum at all --
useful for frameworks unwilling to take on a dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

from ..errors import AdapterError
from ..model import AgentState


@runtime_checkable
class ContinuumAdapter(Protocol):
    """What a checkpointable runtime must provide."""

    name: str

    def export_state(self) -> AgentState:
        """Return the runtime's current state as a portable document.

        Must be side-effect free and repeatable: calling it twice without
        running the agent in between must produce states with the same content
        address. Adapters that embed a timestamp or a random id here break
        deduplication and make every checkpoint look like a change.
        """
        ...

    def import_state(self, state: AgentState) -> None:
        """Reconstruct the runtime so it can continue from ``state``.

        Implementations should validate what they need up front and raise
        :class:`~continuum.errors.AdapterError` rather than partially applying
        a state they cannot fully honour.
        """
        ...


@runtime_checkable
class RunnableAdapter(ContinuumAdapter, Protocol):
    """An adapter that Continuum can also drive, for demos and evaluation."""

    def step(self) -> bool:
        """Advance execution by one unit of work.

        Returns True while there is more to do. One call must correspond to one
        checkpointable boundary -- that is what makes "stop, move, continue"
        meaningful rather than approximate.
        """
        ...


AdapterFactory = Callable[..., ContinuumAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register(name: str, factory: AdapterFactory) -> None:
    """Register an adapter factory under ``name``."""
    if name in _REGISTRY:
        raise AdapterError(f"adapter {name!r} is already registered")
    _REGISTRY[name] = factory


def available() -> list[str]:
    """Names of every adapter, registered in-process or installed."""
    return sorted(set(_REGISTRY) | set(_entry_points()))


def get(name: str) -> AdapterFactory:
    """Look up an adapter factory by name, loading it from entry points if needed."""
    if name in _REGISTRY:
        return _REGISTRY[name]
    entry_points = _entry_points()
    if name in entry_points:
        try:
            factory: AdapterFactory = entry_points[name].load()
        except Exception as exc:
            raise AdapterError(f"adapter {name!r} failed to load: {exc}") from exc
        _REGISTRY[name] = factory
        return factory
    raise AdapterError(f"unknown adapter {name!r}; available: {', '.join(available()) or '(none)'}")


def _entry_points() -> dict[str, Any]:
    from importlib.metadata import entry_points

    try:
        found: Iterable[Any] = entry_points(group="continuum.adapters")
    except TypeError:  # pragma: no cover - Python < 3.10 selection API
        found = entry_points().get("continuum.adapters", [])
    return {ep.name: ep for ep in found}
