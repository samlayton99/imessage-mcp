"""The deterministic tag-law loader: compile ``watch.md`` (a free-form scratchpad) into the active
tag law the summarizer/enforcement use. Step 0 is the deterministic half only — the hash-gated LLM
curator (add/retire) is Step 5. Here the whole file is the active law."""
from pathlib import Path

from text_triage.tags import active_slugs, load_law


def write_watch(tmp_path, text):
    p = tmp_path / "watch.md"
    p.write_text(text)
    return p


def test_loads_slugs_and_descriptions(tmp_path):
    p = write_watch(tmp_path, "# scratch\n- family: My family.\n"
                              "- needs-scheduling: Set a time.\n- church: Church stuff.\n")
    law = load_law(p)
    assert law == {"family": "My family.", "needs-scheduling": "Set a time.", "church": "Church stuff."}
    assert active_slugs(law) == {"family", "needs-scheduling", "church"}


def test_ignores_comments_blanks_and_malformed(tmp_path):
    p = write_watch(tmp_path, "# header\n\n- family: ok\nnot a tag line\n"
                              "- Bad_Slug: nope\n-   : empty\n- spaces in slug: no\n")
    assert load_law(p) == {"family": "ok"}


def test_missing_file_is_empty_law(tmp_path):
    assert load_law(tmp_path / "nope.md") == {}
    assert active_slugs({}) == set()


def test_committed_watch_md_seeds_the_three_step0_tags():
    root = Path(__file__).resolve().parents[1]
    law = load_law(root / "watch.md")
    assert {"family", "needs-scheduling", "church"} <= set(law)
