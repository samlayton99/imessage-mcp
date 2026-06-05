"""``text-triage`` console entry point — a thin subcommand dispatcher.

Subcommands: ``extract`` (chat.db → export JSON), ``summarize`` (export → validated state.json),
``serve`` (the always-on server: MCP + ingest + scheduler), and ``push`` (the collector: push new raw
to the server). The collector and the server are the two halves of the split — on a Mac mini they run
side by side on one box; on a laptop+VPS the collector runs on the Mac and ``serve`` on the VPS.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

_USAGE = ("usage: text-triage {extract|summarize|serve|push} ...\n"
          "  extract/summarize  [--window {weekly,monthly} | --since ISO] [--out PATH] [--config PATH]\n"
          "  serve              [--config PATH] [--state PATH] [--raw-store PATH] [--watch PATH]\n"
          "  push               [--watch] [--config PATH] [--db PATH]")


def _serve(argv: Sequence[str]) -> int:
    import argparse

    from text_triage.config import load_config
    from text_triage.server.app import run_server

    p = argparse.ArgumentParser(prog="text-triage serve",
                                description="Run the always-on server: MCP + /ingest + scheduler.")
    p.add_argument("--config", help="path to conditions.yaml (default: auto-discover)")
    p.add_argument("--watch", help="path to watch.md tag scratchpad (default: auto-discover)")
    p.add_argument("--state", help="path to state.json (default: ~/.text-triage/state.json)")
    p.add_argument("--raw-store", dest="raw_store",
                   help="path to raw_messages.sqlite (default: ~/.text-triage/raw_messages.sqlite)")
    p.add_argument("--no-scheduler", dest="no_scheduler", action="store_true",
                   help="serve only; don't run the timed summary scheduler (useful for testing/debugging)")
    a = p.parse_args(argv)
    config = load_config(a.config)
    run_server(config, state_path=a.state, raw_path=a.raw_store, law_path=a.watch,
               config_path=a.config, start_scheduler=not a.no_scheduler)
    return 0


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
        from text_triage.collect.extract import main as extract_main

        return extract_main(args[1:])
    if args and args[0] == "summarize":
        from text_triage.triage.summarize import main as summarize_main

        return summarize_main(args[1:])
    if args and args[0] == "serve":
        return _serve(args[1:])
    if args and args[0] == "push":
        from text_triage.collect.collector import main as push_main

        return push_main(args[1:])

    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
