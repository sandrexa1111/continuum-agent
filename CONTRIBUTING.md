# Contributing

Continuum is experimental and pre-1.0. Contributions are welcome; the bar is
that a change leaves the project easier to trust, not just larger.

## Setup

```bash
git clone https://github.com/sandrexa1111/continuum-agent
cd continuum-agent
pip install -e ".[dev]"
pytest
```

No API keys, no network, no services. If a change makes that untrue, it needs to
be argued for first.

## Before opening a pull request

```bash
ruff check src tests
ruff format src tests
mypy src/continuum
pytest --cov=continuum
```

CI runs the same commands across Python 3.10–3.13 on Linux, macOS and Windows,
plus a first-run job that installs the built wheel and walks the README quick
start. Windows is not decorative — a cp1252 console once crashed the CLI on its
own box-drawing output.

## The four design tests

Any change touching the format or the public interfaces has to still pass all
four. They are the project's actual acceptance criteria and are restated in
[spec/compatibility.md](spec/compatibility.md).

1. **Adapter independence.** Can another framework implement an adapter without
   modifying Continuum core?
2. **Specification independence.** Can someone implement the format from `spec/`
   without reading `src/`?
3. **Forward evolution.** Can the format grow without invalidating existing
   images?
4. **Honest loss.** Does every piece of dropped information appear in a report?

If a change fails one of these, the change is not ready — even if the tests are
green.

## What good tests look like here

The suite targets properties, not lines. A useful new test usually asserts one
of:

- a **round trip** is a fixed point
- the **same input yields the same content address**
- **tampering is detected** before content reaches the caller
- **one fork does not affect its siblings**
- something is **refused** that should be refused

Tests that only exercise a code path to raise coverage are not worth the
maintenance. Tests that would have caught a real bug are; several in this repo
exist for exactly that reason and say so in their docstrings.

## Changing the format

Format changes need more than code:

- update `spec/` in the same PR — the spec is normative, not documentation
- bump `FORMAT_VERSION` if the wire format changed at all
- confirm the change is legal for a MINOR under
  [spec/compatibility.md](spec/compatibility.md) §3, or explain why it is a MAJOR
- add a compatibility test showing an existing document still reads

Never change the canonical encoding without a MAJOR bump. It invalidates every
content address ever issued.

## Adding an adapter

Adapters for real frameworks are the most valuable contribution right now.

They should live in **their own package**, not in this repository, and register
through the `continuum.adapters` entry point. If publishing an adapter requires
a change to Continuum core, that is a bug in the interface — please open an
issue describing what you needed.

The conformance checklist is in [spec/adapters.md](spec/adapters.md) §4.

**Do not claim compatibility that has not been tested.** An adapter's README
should say which framework versions it was actually run against.

## Commits and scope

Coherent units of work with a conventional prefix (`feat:`, `fix:`, `test:`,
`docs:`, `refactor:`, `ci:`). One concern per pull request. A refactor bundled
with a behaviour change is hard to review and harder to revert.

## Reporting bugs

The most useful bug report includes a `.asi` image that reproduces it — run
`continuum sanitize <ref> --out clean.asi` first, and check what the report says
before attaching anything.
