# Security

## Reporting

Report vulnerabilities through GitHub's private advisory form
(**Security → Report a vulnerability**) rather than a public issue. Expect an
acknowledgement within a week.

## Threat model

Continuum is a local library and CLI. It has no server, opens no ports, and
makes no network calls. Two boundaries matter.

### State images may be untrusted

A `.asi` file is designed to be exchanged, which means one may arrive from
somewhere you do not control. Reading one is treated as parsing hostile input:

- entries with absolute paths or `..` components are refused
- total uncompressed size is bounded, so an image cannot be a decompression bomb
- `state.json` must match the digest in the manifest
- every blob must match its own filename
- all of the above happen **before** any content is returned to the caller

Reading an image never writes to disk outside a caller-specified path and never
executes anything from it. The format carries no code.

### State images may contain secrets

A checkpoint captures whatever the agent was holding, which can include
credentials that reached its context or memory. Before sharing one:

```bash
continuum inspect agent.asi          # summary only, prints no memory content
continuum sanitize agent.asi --out clean.asi
```

`sanitize` detects common credential shapes (cloud keys, provider tokens, JWTs,
private key blocks, credentialed URLs, assignment patterns) and replaces them
with stable, non-reversible placeholders. `--aggressive` adds a high-entropy
heuristic, and `--drop-memory-kind` / `--strip-runtime-opaque` remove whole
sections.

**This is a safety net, not a control.** A credential with no recognizable
shape, split across fields, or encoded will pass. Treat a clean scan as "no
obvious accident found", not "no secrets present". And redacting a checkpoint
does not un-leak a credential that already reached that state — rotate it.

By design, `environment` records environment variable **names** only. Values are
never captured.

## Non-goals

- **Not a sandbox.** Continuum does not confine what an adapter's runtime does.
  The capability manifest is a *declaration and compatibility check*, not an
  enforcement mechanism: it prevents an agent resuming somewhere it cannot
  function, and does nothing to stop a runtime that ignores it.
- **No image signing yet.** Integrity is verified against the manifest, which
  proves an image is internally consistent — not who produced it. An image can
  be re-signed with a recomputed manifest by anyone who can edit it. Signing is
  on the roadmap; until then, trust images the way you trust any file, on
  provenance.
- **No encryption at rest.** Store objects are compressed, not encrypted.

## Supported versions

Pre-1.0: fixes land on `main` and in the next release. There are no maintained
release branches yet.
