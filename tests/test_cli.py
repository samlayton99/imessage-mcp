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
    cfg = tmp_path / "conditions.yaml"
    cfg.write_text("{}\n")  # hermetic: don't pick up the repo's conditions.yaml
    chatdb_factory(db, [conv])
    rc = cli.main(["extract", "--window", "monthly", "--db", str(db), "--addressbook", str(ab),
                   "--out", str(out), "--config", str(cfg)])
    assert rc == 0
    assert json.loads(out.read_text())["window"] == "monthly"


def test_dispatches_summarize_subcommand(monkeypatch):
    import text_triage.summarize as S
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(S, "main", fake_main)
    assert cli.main(["summarize", "--window", "monthly", "--out", "x"]) == 0
    assert seen["argv"] == ["--window", "monthly", "--out", "x"]


def test_no_subcommand_returns_usage_code(capsys):
    rc = cli.main([])
    assert rc == 2
    assert "extract" in capsys.readouterr().err


def test_unknown_subcommand_returns_usage_code():
    assert cli.main(["bogus"]) == 2
