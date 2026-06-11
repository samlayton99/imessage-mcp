"""The deterministic tag-law loader with LIFETIMES: compile ``watch.md`` into ``{slug: TagSpec}``
and compute query-time relevance via ``effective_tags``."""
import datetime
from pathlib import Path

from text_triage.state.schema import REPLY_STATUSES
from text_triage.triage.tags import (SYSTEM_LAW, TagSpec, active_slugs, effective_tags, full_law,
                                     load_law, load_watch)


def write_watch(tmp_path, text):
    p = tmp_path / "watch.md"
    p.write_text(text)
    return p


# ------------------------------------------------------------------- load_law + lifetimes
def test_loads_slugs_descriptions_and_lifetimes(tmp_path):
    p = write_watch(tmp_path,
                    "# scratch\n- family: My family. Sticky / indefinite.\n"
                    "- needs-scheduling: Set a time. Relevant for about 14 days.\n"
                    "- urgent: Expires quickly, about 2 days.\n- church: Church. Permanent.\n")
    law = load_law(p)
    assert set(law) == {"family", "needs-scheduling", "urgent", "church"}
    assert law["family"].description == "My family. Sticky / indefinite."
    assert (law["family"].lifetime, law["family"].ttl_days) == ("sticky", None)
    assert (law["needs-scheduling"].lifetime, law["needs-scheduling"].ttl_days) == ("ttl", 14)
    assert (law["urgent"].lifetime, law["urgent"].ttl_days) == ("ttl", 2)       # number wins
    assert (law["church"].lifetime, law["church"].ttl_days) == ("sticky", None)
    assert active_slugs(law) == set(law)


def test_lifetime_defaults(tmp_path):
    p = write_watch(tmp_path, "- plain: Just a description, no hints.\n"
                              "- temp: A temporary thing that expires.\n")
    law = load_law(p)
    assert (law["plain"].lifetime, law["plain"].ttl_days) == ("sticky", None)   # unclear -> sticky
    assert (law["temp"].lifetime, law["temp"].ttl_days) == ("ttl", 14)          # temp, no number -> 14


def test_ignores_comments_blanks_and_malformed(tmp_path):
    p = write_watch(tmp_path, "# header\n\n- family: ok\nnot a tag line\n"
                              "- Bad_Slug: nope\n-   : empty\n- spaces in slug: no\n")
    assert set(load_law(p)) == {"family"}


def test_missing_file_is_empty_law(tmp_path):
    assert load_law(tmp_path / "nope.md") == {}
    assert active_slugs({}) == set()


def test_committed_watch_md_seeds_the_three_tags():
    root = Path(__file__).resolve().parents[1]
    assert {"family", "needs-scheduling", "church"} <= set(load_law(root / "watch.md"))


# ------------------------------------------------------------------ watch.md sections
SECTIONED = """\
## Who am I
Sam, a grad student in Boston. I run a weekly climbing meetup.

## What to watch
- family: My family. Sticky.
- needs-scheduling: Pending plans. 5 days.

## What I care about
Surface anyone I owe a reply to; my family always matters most.
"""


def test_load_watch_splits_the_three_sections(tmp_path):
    doc = load_watch(write_watch(tmp_path, SECTIONED))
    assert "grad student in Boston" in doc.who_am_i
    assert "- family:" in doc.what_to_watch
    assert "owe a reply" in doc.what_i_care_about
    assert "## " not in doc.who_am_i                  # headers stripped from the bodies


def test_load_watch_section_headers_are_case_insensitive(tmp_path):
    doc = load_watch(write_watch(tmp_path, "## WHO AM I\nme\n## what to watch\n- a: sticky\n"))
    assert doc.who_am_i == "me"
    assert "- a:" in doc.what_to_watch


def test_load_watch_sectionless_file_is_all_what_to_watch(tmp_path):
    text = "- family: ok. Sticky.\n- urgent: 2 days.\n"
    doc = load_watch(write_watch(tmp_path, text))
    assert doc.who_am_i == "" and doc.what_i_care_about == ""
    assert "- family:" in doc.what_to_watch


def test_load_watch_missing_file_is_empty(tmp_path):
    doc = load_watch(tmp_path / "nope.md")
    assert (doc.who_am_i, doc.what_to_watch, doc.what_i_care_about) == ("", "", "")


def test_load_law_reads_only_what_to_watch(tmp_path):
    law = load_law(write_watch(tmp_path, SECTIONED))
    assert set(law) == {"family", "needs-scheduling"}
    assert (law["needs-scheduling"].lifetime, law["needs-scheduling"].ttl_days) == ("ttl", 5)


def test_load_law_law_lines_outside_what_to_watch_ignored(tmp_path):
    p = write_watch(tmp_path, "## Who am I\n- not-a-tag: I describe myself in list form\n"
                              "## What to watch\n- real: sticky\n")
    assert set(load_law(p)) == {"real"}


# ------------------------------------------------------------- system law + kinds
def test_user_law_specs_default_to_freeform_user_origin(tmp_path):
    law = load_law(write_watch(tmp_path, "- family: My family. Sticky.\n"))
    spec = law["family"]
    assert (spec.kind, spec.choices, spec.origin) == ("freeform", None, "user")


def test_system_law_holds_the_reply_status_classification():
    assert set(SYSTEM_LAW) == {"reply_status"}
    spec = SYSTEM_LAW["reply_status"]
    assert spec.kind == "choice"
    assert spec.choices == list(REPLY_STATUSES)
    assert spec.origin == "system"
    assert spec.lifetime == "sticky"
    assert spec.description  # MCP clients need prose, not just a slug


def test_full_law_unions_system_and_user(tmp_path):
    user = load_law(write_watch(tmp_path, "- family: My family. Sticky.\n"))
    law = full_law(user)
    assert set(law) == {"reply_status", "family"}
    assert law["reply_status"].origin == "system"


def test_full_law_system_wins_on_slug_collision():
    fake = {"reply_status": TagSpec("reply_status", "user impostor", "sticky", None)}
    assert full_law(fake)["reply_status"].origin == "system"


def test_active_slugs_excludes_choice_classifications(tmp_path):
    user = load_law(write_watch(tmp_path, "- family: My family. Sticky.\n"))
    # a model emitting "reply_status" inside tags must be droppable by the slug check
    assert active_slugs(full_law(user)) == {"family"}


# ----------------------------------------------------------------------- effective_tags
LAW = {
    "family": TagSpec("family", "fam", "sticky", None),
    "needs-scheduling": TagSpec("needs-scheduling", "sched", "ttl", 14),
}
NOW = datetime.datetime(2026, 6, 2, 12, 0, 0)


def _conv(tags, last_at):
    return {"tags": tags, "last_message_at": last_at}


def test_effective_sticky_always_included():
    assert effective_tags(_conv(["family"], "2020-01-01 00:00:00"), LAW, as_of=NOW) == ["family"]


def test_effective_ttl_within_window_included():
    c = _conv(["needs-scheduling"], "2026-05-30 12:00:00")          # 3 days ago, ttl 14
    assert effective_tags(c, LAW, as_of=NOW) == ["needs-scheduling"]


def test_effective_ttl_expired_excluded():
    c = _conv(["needs-scheduling"], "2026-05-01 12:00:00")          # 32 days ago, ttl 14
    assert effective_tags(c, LAW, as_of=NOW) == []


def test_effective_unparseable_date_kept():
    assert effective_tags(_conv(["needs-scheduling"], "not a date"), LAW, as_of=NOW) == ["needs-scheduling"]


def test_effective_unknown_tag_kept():
    assert effective_tags(_conv(["mystery"], "2020-01-01 00:00:00"), LAW, as_of=NOW) == ["mystery"]
