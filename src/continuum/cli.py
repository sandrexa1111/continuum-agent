"""Command-line interface.

Two kinds of command live here. Most operate on stored state and images --
``history``, ``inspect``, ``diff``, ``fork``, ``migrate``, ``sanitize``, ``verify``
-- and work regardless of which runtime produced the state. A few (``run``,
``resume``) drive the bundled reference adapter, which is what makes the
quick start runnable with nothing installed but this package.

Every command that reports a judgement uses its exit code: ``0`` success,
``1`` operation failed, ``2`` bad usage, ``3`` a check came back negative
(resume blocked, integrity failure, secrets found). Scripts and CI can act on
that without parsing output, which is the point of shipping ``--json`` too.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from ._console import enable_utf8, glyphs
from .adapters import available as available_adapters
from .adapters.native import NativeReviewAgent
from .capabilities import check_capabilities
from .diff import diff_states
from .errors import ContinuumError, ResumeBlocked
from .image import inspect_image, read_image, write_image
from .migrate import ROLES_CHAT_ONLY, ROLES_WITH_TOOLS, MigrationTarget, migrate
from .model import AgentState
from .redact import sanitize_state
from .runtime import checkout, checkpoint, fork, resume
from .store import Store

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CHECK_FAILED = 3


# ---------------------------------------------------------------- helpers


def _store(args: argparse.Namespace) -> Store:
    return Store.discover(getattr(args, "store", None) or Path.cwd())


def _emit(payload: dict[str, Any] | list[Any], text: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, default=str) if as_json else text)


def _load_state(args: argparse.Namespace, ref: str) -> AgentState:
    """Resolve a reference that may be an image path or a store reference."""
    candidate = Path(ref)
    if candidate.suffix == ".asi" or candidate.is_file():
        return read_image(candidate).state
    return checkout(_store(args), ref)


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    store = Store.init(args.path)
    print(f"initialized Continuum store at {store.root}")
    return EXIT_OK


def cmd_adapters(args: argparse.Namespace) -> int:
    names = available_adapters()
    _emit({"adapters": names}, "\n".join(names) or "(no adapters installed)", args.json)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    """Drive the reference adapter, checkpointing after every step."""
    workspace = Path(args.workspace)
    if not (workspace / "corpus").is_dir():
        print(
            f"error: {workspace / 'corpus'} does not exist.\n"
            "The reference agent reviews a corpus of .md/.txt files. "
            "Run `continuum demo --workspace <dir>` to generate one.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    store = Store.init(workspace)
    agent = NativeReviewAgent(
        workspace,
        agent_id=args.agent_id,
        model_label=args.model,
    )
    if args.ranking or args.max_findings:
        params = dict(agent.export_state().provider.params)
        if args.ranking:
            params["ranking"] = args.ranking
        if args.max_findings:
            params["max_findings"] = args.max_findings
        state = agent.export_state()
        agent.import_state(state.replace(provider=_with_params(state, params, args.model)))

    refs = [checkpoint(agent, store, label="initial")]
    steps = 0
    while agent.step():
        steps += 1
        refs.append(checkpoint(agent, store, label=f"step-{steps}"))
        if args.steps and steps >= args.steps:
            print(f"stopped after {steps} step(s) as requested")
            break
    else:
        refs.append(checkpoint(agent, store, label="final"))

    final = agent.export_state()
    print(f"agent    {final.identity.agent_id}")
    print(
        f"status   {final.execution.status.value} at step {final.execution.step}/{len(final.execution.cursor.get('plan', []))}"
    )
    print(f"head     {refs[-1].digest}")
    print(f"artifacts {', '.join(a.path for a in final.artifacts) or '(none yet)'}")
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    store = _store(args)
    refs = store.checkpoints()
    if args.agent_id:
        refs = [r for r in refs if r.agent_id == args.agent_id]
    if not refs:
        print("no checkpoints recorded")
        return EXIT_OK

    if args.json:
        print(json.dumps([r.to_dict() for r in refs], indent=2))
        return EXIT_OK

    by_digest = {r.digest: r for r in refs}
    children: dict[str | None, list[str]] = {}
    for ref in refs:
        parent = ref.parent if ref.parent in by_digest else None
        children.setdefault(parent, []).append(ref.digest)

    marks = glyphs()

    def render(digest: str, prefix: str, last: bool) -> None:
        ref = by_digest[digest]
        connector = marks["last"] if last else marks["branch"]
        forked = f" [forked from {ref.forked_from.split(':')[1][:12]}]" if ref.forked_from else ""
        print(
            f"{prefix}{connector}{digest.split(':')[1][:12]}  "
            f"{ref.label or '(unlabeled)':<16} {ref.created_at}{forked}"
        )
        kids = children.get(digest, [])
        for i, kid in enumerate(kids):
            render(kid, prefix + (marks["blank"] if last else marks["pipe"]), i == len(kids) - 1)

    roots = children.get(None, [])
    for i, root in enumerate(roots):
        render(root, "", i == len(roots) - 1)

    heads = store.heads()
    if heads:
        print("\nheads:")
        for agent_id, digest in sorted(heads.items()):
            print(f"  {agent_id:<20} {digest.split(':')[1][:12]}")
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    candidate = Path(args.ref)
    if candidate.suffix == ".asi" or candidate.is_file():
        summary = inspect_image(candidate)
    else:
        state = checkout(_store(args), args.ref)
        summary = {
            "state_digest": state.digest(),
            "format_version": state.format_version,
            "agent_id": state.identity.agent_id,
            "objective": state.objective.goal,
            "status": state.execution.status.value,
            "current_task": state.execution.current_task,
            "step": state.execution.step,
            "counts": {
                "memory": len(state.memory),
                "context_messages": len(state.context),
                "artifacts": len(state.artifacts),
                "events": len(state.events),
            },
            "capabilities": {
                "requires": state.capabilities.requires,
                "optional": state.capabilities.optional,
            },
            "provider": {
                "adapter": state.provider.adapter,
                "provider": state.provider.provider,
                "model": state.provider.model,
                "has_opaque_state": bool(state.provider.opaque),
            },
            "lineage": state.lineage.to_dict(),
        }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return EXIT_OK

    print(f"digest      {summary.get('state_digest', '')}")
    print(f"format      {summary['format_version']}")
    print(f"agent       {summary['agent_id']}")
    print(f"objective   {summary['objective']}")
    print(
        f"status      {summary['status']} (task {summary['current_task']!r}, step {summary['step']})"
    )
    counts = summary["counts"]
    print(
        f"contents    {counts['memory']} memory, {counts['context_messages']} messages, "
        f"{counts['artifacts']} artifacts, {counts['events']} events"
    )
    provider = summary["provider"]
    print(f"provider    {provider['adapter']}/{provider['provider']}/{provider['model']}")
    if provider["has_opaque_state"]:
        print("            (carries provider-side opaque state; will not survive migration)")
    caps = summary["capabilities"]
    print(f"requires    {', '.join(caps['requires']) or '(none)'}")
    if caps["optional"]:
        print(f"optional    {', '.join(caps['optional'])}")
    if summary.get("artifacts_external"):
        print(
            f"external    {len(summary['artifacts_external'])} artifact blob(s) not packed in this image"
        )
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    store = _store(args)
    state = checkout(store, args.ref)
    blobs: dict[str, bytes] = {}
    if args.include_artifacts:
        workspace = Path(args.workspace or store.root.parent)
        for artifact in state.artifacts:
            path = workspace / artifact.path
            if path.is_file():
                blobs[artifact.digest] = path.read_bytes()
    out = write_image(state, args.out, blobs)
    size = out.stat().st_size
    print(f"wrote {out} ({size} bytes, {len(blobs)} artifact blob(s) packed)")
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    store = Store.discover(getattr(args, "store", None) or Path.cwd())
    image = read_image(args.image)
    ref = store.record_checkpoint(
        image.state, label=args.label or f"imported:{Path(args.image).name}"
    )
    for data in image.blobs.values():
        store.put_bytes(data)
    print(f"imported {ref.digest} ({len(image.blobs)} blob(s))")
    return EXIT_OK


def cmd_fork(args: argparse.Namespace) -> int:
    store = _store(args)
    state = checkout(store, args.ref)
    labels = args.label or [f"branch-{i}" for i in range(1, (args.count or 2) + 1)]
    forks = fork(state, store, labels)
    if args.json:
        print(json.dumps([{"label": f.label, "digest": f.digest} for f in forks], indent=2))
        return EXIT_OK
    print(f"forked {state.digest().split(':')[1][:12]} into {len(forks)} branch(es):")
    for f in forks:
        print(f"  {f.label:<16} {f.digest.split(':')[1][:12]}")
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    state = _load_state(args, args.ref)
    workspace = Path(args.workspace)
    if args.model or args.ranking or args.max_findings:
        params = dict(state.provider.params)
        if args.ranking:
            params["ranking"] = args.ranking
        if args.max_findings:
            params["max_findings"] = args.max_findings
        state = state.replace(provider=_with_params(state, params, args.model))

    try:
        agent, report = resume(
            state,
            NativeReviewAgent,
            granted=args.grant or None,
            allow_degraded=args.allow_degraded,
            workspace=workspace,
            agent_id=state.identity.agent_id,
        )
    except ResumeBlocked as blocked:
        print(blocked.report.render(), file=sys.stderr)
        print(
            "\nRESUME REFUSED. Grant the capabilities above with --grant, "
            "or override with --allow-degraded if the agent can finish without them.",
            file=sys.stderr,
        )
        return EXIT_CHECK_FAILED

    print(report.render())
    print()
    final = agent.run_to_completion() if args.run else agent.export_state()
    store = Store.init(workspace)
    ref = checkpoint(agent, store, label=args.label or "resumed")
    print(f"status   {final.execution.status.value} at step {final.execution.step}")
    print(f"head     {ref.digest}")
    for artifact in final.artifacts:
        print(f"artifact {artifact.path}  {artifact.digest.split(':')[1][:12]}")
    return EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    left = _load_state(args, args.left)
    right = _load_state(args, args.right)
    result = diff_states(left, right)
    _emit(result.to_dict(), result.render(), args.json)
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    state = _load_state(args, args.ref)
    target = MigrationTarget(
        adapter=args.adapter,
        provider=args.provider,
        model=args.model,
        max_context_tokens=args.max_context_tokens,
        granted_capabilities=args.grant or [],
        roles=ROLES_CHAT_ONLY if args.no_tool_role else ROLES_WITH_TOOLS,
    )
    result = migrate(state, target)
    if args.json:
        print(json.dumps(result.report.to_dict(), indent=2))
    else:
        print(result.report.render())

    if args.out:
        write_image(result.state, args.out)
        if not args.json:
            print(f"\nwrote migrated state to {args.out}")
    elif not args.dry_run:
        store = _store(args)
        ref = store.record_checkpoint(
            result.state, label=args.label or f"migrated:{args.model or args.adapter}"
        )
        if not args.json:
            print(f"\nrecorded migrated state as {ref.digest}")

    return EXIT_CHECK_FAILED if result.report.blocked else EXIT_OK


def cmd_capabilities(args: argparse.Namespace) -> int:
    state = _load_state(args, args.ref)
    report = check_capabilities(state, args.grant if args.grant else None)
    _emit(report.to_dict(), report.render(), args.json)
    return EXIT_OK if report.ok else EXIT_CHECK_FAILED


def cmd_sanitize(args: argparse.Namespace) -> int:
    state = _load_state(args, args.ref)
    sanitized, report = sanitize_state(
        state,
        aggressive=args.aggressive,
        drop_memory_kinds=args.drop_memory_kind or (),
        strip_runtime_opaque=args.strip_runtime_opaque,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())

    if args.out:
        write_image(sanitized, args.out)
        if not args.json:
            print(f"\nwrote sanitized image to {args.out}")
    elif not report.clean and not args.json:
        print("\n(no --out given; nothing was written)")

    return EXIT_CHECK_FAILED if not report.clean else EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    if args.image:
        try:
            image = read_image(args.image)
        except ContinuumError as exc:
            print(f"FAIL  {args.image}: {exc}", file=sys.stderr)
            return EXIT_CHECK_FAILED
        print(
            f"OK    {args.image}: state and {len(image.blobs)} blob(s) verified against their digests"
        )
        return EXIT_OK

    store = _store(args)
    broken = store.verify()
    total = len(list(store.iter_objects()))
    if broken:
        print(f"FAIL  {len(broken)} of {total} objects failed verification:", file=sys.stderr)
        for address in broken:
            print(f"      {address}", file=sys.stderr)
        return EXIT_CHECK_FAILED
    print(f"OK    {total} object(s) verified against their content addresses")
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    """Generate a workspace and run the checkpoint -> fork -> compare story."""
    from .demo import run_demo

    workspace = Path(args.workspace)
    if workspace.exists() and args.clean:
        shutil.rmtree(workspace)
    return run_demo(workspace)


def _with_params(state: AgentState, params: dict[str, Any], model: str | None) -> Any:
    provider = state.provider
    return provider.__class__(
        adapter=provider.adapter,
        provider=provider.provider,
        model=model or provider.model,
        params=params,
        opaque=provider.opaque,
    )


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuum",
        description="Checkpoint, fork, migrate and resume long-lived AI agents.",
        epilog="Start with:  continuum demo --workspace ./demo",
    )
    parser.add_argument("--version", action="version", version=f"continuum-agent {__version__}")
    parser.add_argument(
        "--store", help="directory containing the .continuum store (default: search upward)"
    )

    # `--store` is accepted both before and after the subcommand. Users type
    # `continuum history --store X` far more naturally than the strictly
    # correct `continuum --store X history`, and refusing that is a papercut
    # with no upside. SUPPRESS keeps the subcommand's copy from overwriting an
    # earlier value with None when it is not given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    def subcommand(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    p = subcommand("init", help="create a Continuum store")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = subcommand("demo", help="generate a workspace and run the full reference story")
    p.add_argument("--workspace", default="./continuum-demo")
    p.add_argument("--clean", action="store_true", help="delete the workspace first")
    p.set_defaults(func=cmd_demo)

    p = subcommand("adapters", help="list installed runtime adapters")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_adapters)

    p = subcommand("run", help="drive the reference agent, checkpointing each step")
    p.add_argument("--workspace", required=True)
    p.add_argument("--agent-id", default="reviewer")
    p.add_argument("--model", default="deterministic-reviewer")
    p.add_argument("--ranking", choices=["severity", "position", "length"])
    p.add_argument("--max-findings", type=int)
    p.add_argument("--steps", type=int, help="stop after N steps (leaves the agent suspended)")
    p.set_defaults(func=cmd_run)

    p = subcommand("history", help="show the checkpoint graph")
    p.add_argument("--agent-id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = subcommand(
        "inspect", help="summarize a checkpoint or .asi image without printing its content"
    )
    p.add_argument("ref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = subcommand("export", help="write a checkpoint to a portable .asi image")
    p.add_argument("ref")
    p.add_argument("out")
    p.add_argument("--include-artifacts", action="store_true")
    p.add_argument("--workspace", help="where artifact files live (default: store parent)")
    p.set_defaults(func=cmd_export)

    p = subcommand("import", help="load a .asi image into the store")
    p.add_argument("image")
    p.add_argument("--label")
    p.set_defaults(func=cmd_import)

    p = subcommand("fork", help="branch a checkpoint")
    p.add_argument("ref")
    p.add_argument("--label", action="append", help="repeatable; one per branch")
    p.add_argument("--count", type=int, help="create N generically-named branches")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fork)

    p = subcommand("resume", help="rebuild the reference runtime from a checkpoint or image")
    p.add_argument("ref")
    p.add_argument("--workspace", required=True)
    p.add_argument(
        "--grant", action="append", help="repeatable capability grant, e.g. filesystem.*"
    )
    p.add_argument(
        "--allow-degraded", action="store_true", help="resume despite missing required capabilities"
    )
    p.add_argument("--run", action="store_true", help="run to completion after resuming")
    p.add_argument("--model")
    p.add_argument("--ranking", choices=["severity", "position", "length"])
    p.add_argument("--max-findings", type=int)
    p.add_argument("--label")
    p.set_defaults(func=cmd_resume)

    p = subcommand("diff", help="structured diff between two states")
    p.add_argument("left")
    p.add_argument("right")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_diff)

    p = subcommand("migrate", help="translate a state for another runtime and report what survives")
    p.add_argument("ref")
    p.add_argument("--adapter", required=True)
    p.add_argument("--provider", default="")
    p.add_argument("--model", default="")
    p.add_argument("--max-context-tokens", type=int)
    p.add_argument("--grant", action="append")
    p.add_argument("--no-tool-role", action="store_true", help="destination has no tool role")
    p.add_argument("--out", help="write the migrated state to a .asi image")
    p.add_argument("--dry-run", action="store_true", help="report only; record nothing")
    p.add_argument("--label")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_migrate)

    p = subcommand("capabilities", help="check a state against a set of grants")
    p.add_argument("ref")
    p.add_argument("--grant", action="append")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_capabilities)

    p = subcommand("sanitize", help="scan for secrets and write a redacted image")
    p.add_argument("ref")
    p.add_argument("--out")
    p.add_argument("--aggressive", action="store_true", help="also flag high-entropy strings")
    p.add_argument("--drop-memory-kind", action="append", help="repeatable, e.g. working")
    p.add_argument("--strip-runtime-opaque", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sanitize)

    p = subcommand("verify", help="re-hash stored objects or a .asi image")
    p.add_argument("--image", help="verify a single image instead of the store")
    p.set_defaults(func=cmd_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    enable_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except ContinuumError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
