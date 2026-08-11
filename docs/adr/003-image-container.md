# ADR 003 — ZIP as the image container

**Status:** accepted · **Date:** 2026-08-11

## Context

A `.asi` image holds a state document plus the blobs it references. The options
were a bespoke binary container, a tar archive, a single JSON file with base64
blobs, or ZIP.

## Decision

ZIP, with `manifest.json`, `state.json`, and `objects/<hex>` entries.

## Rationale

The decisive property is that a skeptical reviewer can inspect an image without
installing anything. Unzipping the file and reading `state.json` works on every
platform, in every language, with no Continuum present. For a format asking to
be trusted with agent state, that transparency is worth more than any efficiency
a custom container would buy.

Base64-in-JSON was rejected because it inflates blobs by a third and forces the
whole image through a JSON parser. Tar was rejected because it has no central
directory, so reading the manifest of a large image means scanning it.

## Consequences

- ZIP is a well-understood attack surface, so verification is treated as a
  security boundary: path traversal and absolute paths are refused, total
  uncompressed size is bounded, and every digest is rechecked before any content
  reaches the caller.
- Images are not streamable — the container is read whole. Acceptable at the
  sizes this targets; revisit if large artifact packing becomes common.
- Deflate gives compression for free without a dependency.
