"""The deterministic tag-law loader with LIFETIMES: compile ``watch.md`` into ``{slug: TagSpec}``
and compute query-time relevance via ``effective_tags``."""
import datetime
from pathlib import Path

from text_triage.triage.tags import TagSpec, active_slugs, effective_tags, load_law


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
