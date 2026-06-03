"""``text-triage`` console entry point — a thin subcommand dispatcher.

Milestone 1 exposes one subcommand: ``extract``. The summarizer, watcher and server land as
additional subcommands in later steps.
"""
from __future__ import annotations

import sys
from typing import Optional, Sequence

_USAGE = "usage: text-triage extract [--window {7d,30d} | --since ISO] [--out PATH] [--db PATH]"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "extract":
        from text_triage.extract import main as extract_main

        return extract_main(args[1:])

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
