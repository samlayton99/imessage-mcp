"""prompts.render fills agents/<mode>.md templates via ${placeholder} substitution. Prompt content
lives in the markdown files, not in code."""
import pytest

from text_triage.prompts import render


def test_render_substitutes_placeholders_and_leaves_braces(tmp_path):
    (tmp_path / "x.md").write_text("Hello ${who}, you have ${n} messages.\n{literal braces stay}")
    out = render("x", {"who": "Sam", "n": 3}, agents_dir=tmp_path)
    assert "Hello Sam, you have 3 messages." in out
    assert "{literal braces stay}" in out          # JSON-style braces are untouched by Template


def test_render_missing_placeholder_raises(tmp_path):
    (tmp_path / "x.md").write_text("Hi ${who} ${missing}")
    with pytest.raises(KeyError):
        render("x", {"who": "Sam"}, agents_dir=tmp_path)


def test_render_missing_template_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        render("nope", {}, agents_dir=tmp_path)


def test_golden_rule_is_auto_injected(tmp_path):
    (tmp_path / "_golden_rule.md").write_text("NEVER ASSUME.\n")
    (tmp_path / "d.md").write_text("Rule: ${golden_rule}\nBody ${x}")
    out = render("d", {"x": "hi"}, agents_dir=tmp_path)            # not passed in mapping
    assert "Rule: NEVER ASSUME." in out and "Body hi" in out


def test_committed_daily_template_renders_with_build_keys():
    # the shipped agents/daily.md must render with exactly the keys build_daily_prompt supplies
    out = render("daily", {"name": "X", "kind": "1:1", "identity": "i", "monthly": "m",
                           "weekly": "  (none)", "daily": "  (none)", "history": "  (none)",
                           "law": "  - family (sticky): kin", "messages": "  [t] X: hi"})
    assert "X (1:1)" in out and "family (sticky): kin" in out and "hi" in out
    assert "assume" in out.lower()        # the shared golden rule is injected into the daily prompt


def test_committed_weekly_and_monthly_templates_render():
    keys = {"name": "X", "kind": "1:1", "identity": "i", "monthly": "m",
            "history": "  (none)", "law": "  - family (sticky): kin", "messages": "  [t] X: hi"}
    assert "X (1:1)" in render("weekly", keys) and "assume" in render("weekly", keys).lower()
    assert "X (1:1)" in render("monthly", keys) and "assume" in render("monthly", keys).lower()
