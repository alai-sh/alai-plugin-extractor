"""Command line entry point: extract a local collection and print the result."""

import argparse
import dataclasses
import json
import pathlib
import sys
import tarfile
import typing as _t

from ._extract import extract
from ._types import Collection, SourceType


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alai-plugin-extractor",
        description="Statically extract plugin metadata from an Ansible collection. Nothing in it is executed.",
    )
    _ = parser.add_argument(
        "path",
        type=pathlib.Path,
        help="An extracted collection directory or a collection tarball.",
    )
    _ = parser.add_argument(
        "--type",
        choices=_t.get_args(SourceType),
        default="collection",
        help="What PATH holds: collection (default), or ansible for an ansible-core source checkout, "
        + "extracted as ansible.builtin.",
    )
    _ = parser.add_argument(
        "--docs",
        action="store_true",
        help="Include each plugin's raw docs and the collection's doc fragments (JSON only).",
    )
    _ = parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="json (default) prints every field; text prints a table.",
    )
    return parser


def _text(result: Collection) -> str:
    lines: list[str] = []
    if result.namespace or result.name:
        lines.append(f"{result.namespace}.{result.name} {result.version}")
    width = max((len(p.type) for p in result.plugins), default=0)
    for p in result.plugins:
        desc = f"  {p.short_description}" if p.short_description else ""
        lines.append(f"{p.type:<{width}}  {p.name}{desc}")
    lines.extend(f"routing {r.type}:{r.name} {json.dumps(r.entry, sort_keys=True)}" for r in result.routing)
    return "".join(f"{line}\n" for line in lines)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.docs and args.format != "json":
        parser.error("--docs is only available with --format json")

    try:
        result = extract(args.path, type=args.type, docs=args.docs)
    except (OSError, tarfile.TarError, ValueError) as e:
        parser.error(f"can't extract {args.path}: {e}")

    if args.format == "json":
        print(json.dumps(dataclasses.asdict(result), indent=2))
    else:
        print(_text(result), end="")

    for issue in result.issues:
        detail = f" ({issue.detail})" if issue.detail else ""
        print(f"warning: {issue.kind}: {issue.path}{detail}", file=sys.stderr)
    return 0
