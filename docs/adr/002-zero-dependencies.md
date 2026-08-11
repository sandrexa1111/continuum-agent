# ADR 002 — No runtime dependencies

**Status:** accepted · **Date:** 2026-08-11

## Context

Continuum asks other projects to adopt a state format. Adoption cost includes
whatever the library drags in behind it.

The obvious candidates were pydantic (validation), PyYAML (manifests), typer or
click (CLI), and rich (output).

## Decision

Zero required runtime dependencies. Standard library only. PyYAML is an optional
extra; everything else was written directly.

## Rationale

A format library that pulls in a validation framework, a YAML parser, and a CLI
toolkit is a dependency-resolution problem for every project that adopts it —
and agent frameworks, the intended adopters, already have crowded dependency
trees and strong opinions about pydantic versions in particular.

The cost was modest and bounded: dataclasses with explicit `to_dict`/`from_dict`
instead of pydantic, `argparse` instead of typer, hand-rolled ANSI instead of
rich. Writing the serialization by hand also turned out to be an advantage,
since canonical encoding needs exact control over field emission that a
general-purpose validator would fight.

## Consequences

- More code to maintain in `model.py`, and validation errors are constructed by
  hand. They are, in exchange, phrased for the person who has to act on them.
- No dependency CVEs to track, and installation is instant.
- Development dependencies (pytest, ruff, mypy) are unconstrained; this decision
  is about what a *consumer* installs.
