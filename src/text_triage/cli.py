"""``text-triage`` console entry point — a thin subcommand dispatcher.

Subcommands: ``extract`` (chat.db → export JSON) and ``summarize`` (export → validated state.json
via the daily LLM pass). The watcher and server land as additional subcommands in later steps.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

_USAGE = ("usage: text-triage {extract|summarize} [--window {weekly,monthly} | --since ISO] "
          "[--out PATH] [--db PATH] [--config PATH]")


def _load_env() -> None:
    """Load secrets from ``~/.text-triage/.env`` then ``./.env`` (real values stay in .env, which is
    gitignored). Uses python-dotenv when available (it ships with litellm); a no-op otherwise."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.home() / ".text-triage" / ".env")
    load_dotenv(Path(".env"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    _load_env()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "extract":
        from text_triage.extract import main as extract_main

        return extract_main(args[1:])
    if args and args[0] == "summarize":
        from text_triage.summarize import main as summarize_main

        return summarize_main(args[1:])

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
