#!/usr/bin/env python3
"""Generate the committed test fixtures by running the REAL extractor against a crafted,
PII-free temp chat.db. Output never drifts from the extractor's real shape, and contains no real
contacts or messages, so it is safe to commit (unlike real exports — see CONTEXT "Privacy").

Writes:
  tests/fixtures/synthetic_export.json            — extractor output (skeleton builder input)
  tests/fixtures/synthetic_export.expected_state.json  — golden build_skeleton(state) output

Run:  ~/.venvs/text-triage/bin/python scripts/make_synthetic_fixture.py
Re-run to refresh after an extractor/skeleton shape change, then eyeball the diff before committing.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_chatdb  # noqa: E402  (test-support factory, reused for fixtures)
from text_triage.collect.extract import MAC_EPOCH_OFFSET, extract  # noqa: E402
from text_triage.triage.skeleton import build_skeleton  # noqa: E402

NOW = 1_780_000_000.0  # fixed "now" so the fixture is fully deterministic
DAY = 86400
NS = 1_000_000_000


def d(days_ago, secs=0):
    return int((NOW - days_ago * DAY + secs - MAC_EPOCH_OFFSET) * NS)


# Curated, fully fake dataset covering every shape the skeleton must handle. Order matters:
# message ROWIDs are assigned in spec order, so the last conversation (Dana, a same-second pair)
# holds the highest ROWIDs and the newest date — giving the watermark tie-break real teeth.
CONVERSATIONS = [
    {  # named 1:1, last from them -> needs_reply
        "identifier": "+15550000101", "display_name": None, "handles": ["+15550000101"],
        "messages": [
            {"date": d(5), "from_me": True, "handle": "+15550000101", "text": "want to grab dinner this week?"},
            {"date": d(4), "from_me": False, "handle": "+15550000101", "text": "yes! thursday works for me"},
        ],
    },
    {  # named 1:1, last from me -> responded
        "identifier": "+15550000102", "display_name": None, "handles": ["+15550000102"],
        "messages": [
            {"date": d(6), "from_me": False, "handle": "+15550000102", "text": "did you see the game?"},
            {"date": d(3), "from_me": True, "handle": "+15550000102", "text": "yeah what a finish"},
        ],
    },
    {  # UNNAMED 1:1 (raw phone), last from them
        "identifier": "+15550000103", "display_name": None, "handles": ["+15550000103"],
        "messages": [
            {"date": d(7), "from_me": True, "handle": "+15550000103", "text": "is this still your number?"},
            {"date": d(2), "from_me": False, "handle": "+15550000103", "text": "yep it's me"},
        ],
    },
    {  # group chat
        "identifier": "chat-trail-crew", "display_name": "Trail Crew",
        "handles": ["+15550000110", "+15550000111"],
        "messages": [
            {"date": d(8), "from_me": False, "handle": "+15550000110", "text": "hike saturday?"},
            {"date": d(6), "from_me": False, "handle": "+15550000111", "text": "i'm in"},
        ],
    },
    {  # STALE unresponded (45 days ago, last from them) -> unresponded list, NOT in window
        "identifier": "+15550000104", "display_name": None, "handles": ["+15550000104"],
        "messages": [
            {"date": d(46), "from_me": True, "handle": "+15550000104", "text": "good to see you!"},
            {"date": d(45), "from_me": False, "handle": "+15550000104", "text": "likewise, let's catch up soon"},
        ],
    },
    {  # same-second pair, newest -> watermark tie-break teeth
        "identifier": "+15550000105", "display_name": None, "handles": ["+15550000105"],
        "messages": [
            {"date": d(1), "from_me": False, "handle": "+15550000105", "text": "two texts"},
            {"date": d(1), "from_me": False, "handle": "+15550000105", "text": "same second"},
        ],
    },
]

CONTACTS = {
    "+15550000101": {"first": "Avery", "last": "Quinn"},
    "+15550000102": {"first": "Blair", "last": "Stone", "org": "Globex", "job": "Designer",
                     "emails": ["blair@example.com"]},
    "+15550000104": {"first": "Casey", "last": "Vale"},
    "+15550000105": {"first": "Dana", "last": "West"},
}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "chat.db"
        ab = Path(tmp) / "ab"
        make_chatdb(db, CONVERSATIONS, contacts=CONTACTS, addressbook_dir=str(ab))
        export = extract(db_path=str(db), addressbook_dir=str(ab), window="monthly", now=NOW)

    state = build_skeleton(export)

    fixtures = ROOT / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "synthetic_export.json").write_text(
        json.dumps(export, indent=2, ensure_ascii=False) + "\n"
    )
    (fixtures / "synthetic_export.expected_state.json").write_text(
        state.model_dump_json(indent=2) + "\n"
    )
    print(
        f"wrote fixtures: {len(export['conversations'])} conversations, "
        f"{len(export['unresponded'])} unresponded, watermark={export['watermark']}"
    )


if __name__ == "__main__":
    main()
