#!/usr/bin/env python3
"""Read a value out of one of the config/*.yaml files, with ${ENV} expanded.

Shell callers use this instead of parsing YAML with sed:

    MIN_BUCKET_SIZE=$(cfg.py get config/collector.yaml collector.ingestion.min_bucket_size)

Subcommands
    get   <file> <dotted.path> [--default V]   print one scalar
    json  <file> [dotted.path]                 print a subtree as JSON
    keys  <file> <dotted.path>                 print child keys, one per line

Exit 3 means "path not present and no --default given" — distinguishable from a
YAML parse error (exit 2), which matters when a script is deciding whether a
setting is absent or the file is broken.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml  (or run scripts/setup.sh)")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(node: Any) -> Any:
    """Recursively expand ${VAR} against the environment.

    An undefined variable is left as-is rather than blanked. A silently empty
    host or port produces a confusing failure much later; the literal ${FOO}
    fails loudly at the point of use.
    """
    if isinstance(node, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), node)
    if isinstance(node, dict):
        return {k: expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [expand(v) for v in node]
    return node


def load(path: str) -> Any:
    try:
        with open(path) as fh:
            return expand(yaml.safe_load(fh) or {})
    except FileNotFoundError:
        sys.exit(f"config file not found: {path}")
    except yaml.YAMLError as exc:
        sys.stderr.write(f"invalid YAML in {path}: {exc}\n")
        sys.exit(2)


_MISSING = object()


def dig(root: Any, dotted: str) -> Any:
    node = root
    if not dotted:
        return node
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("file")
    g.add_argument("path")
    g.add_argument("--default", dest="default")

    j = sub.add_parser("json")
    j.add_argument("file")
    j.add_argument("path", nargs="?", default="")

    k = sub.add_parser("keys")
    k.add_argument("file")
    k.add_argument("path")

    args = ap.parse_args()
    root = load(args.file)
    value = dig(root, args.path)

    if args.cmd == "get":
        if value is _MISSING:
            if args.default is not None:
                print(args.default)
                return 0
            sys.stderr.write(f"{args.file}: no such key: {args.path}\n")
            return 3
        print(render_scalar(value))
        return 0

    if args.cmd == "json":
        if value is _MISSING:
            sys.stderr.write(f"{args.file}: no such key: {args.path}\n")
            return 3
        print(json.dumps(value, indent=2, default=str))
        return 0

    if value is _MISSING or not isinstance(value, dict):
        sys.stderr.write(f"{args.file}: not a mapping: {args.path}\n")
        return 3
    for key in value:
        print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
