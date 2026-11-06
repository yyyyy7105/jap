"""jap - Japan trip toolkit. Each component registers its own subcommand."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; Japanese names would crash printing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    from .restaurants import cli as restaurants_cli

    parser = argparse.ArgumentParser(prog="jap", description="Japan trip toolkit")
    sub = parser.add_subparsers(dest="component", required=True)
    restaurants_cli.register(sub)
    # future components register here

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
