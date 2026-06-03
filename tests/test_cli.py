"""The `text-triage` entry point. Milestone 1 exposes one subcommand: `extract`."""
import json

import pytest

from text_triage import cli


def test_dispatches_extract_subcommand(tmp_path, chatdb_factory):
    conv = {
        "identifier": "+15550000001",
        "display_name": None,
        "handles": ["+15550000001"],
        "messages": [{"date": 801606400000000000, "from_me": False, "handle": "+15550000001",
                      "text": "hi"}],
    }
    db = tmp_path / "chat.db"
    ab = tmp_path / "ab"
    out = tmp_path / "export.json"
    chatdb_factory(db, [conv])
    rc = cli.main(["extract", "--window", "monthly", "--db", str(db), "--addressbook", str(ab),
                   "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text())["window"] == "monthly"


def test_no_subcommand_returns_usage_code(capsys):
    rc = cli.main([])
    assert rc == 2
    assert "extract" in capsys.readouterr().err


def test_unknown_subcommand_returns_usage_code():
    assert cli.main(["bogus"]) == 2
