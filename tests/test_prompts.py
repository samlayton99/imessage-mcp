"""prompts.py builds each call's (system, user) from agents/*.md. system = _global.md (the shared
frame, with ${law}) + <mode>.md (role); user = <mode>.user.md (this conversation's data). Prompt
content lives in the markdown files, not in code."""
import pytest

from text_triage.triage.prompts import build_system, build_user, render


# ---- render (low-level) ------------------------------------------------------
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


def test_render_reads_compound_stem(tmp_path):
    (tmp_path / "daily.user.md").write_text("data for ${who}")
    assert render("daily.user", {"who": "X"}, agents_dir=tmp_path) == "data for X"


# ---- build_system: shared global frame + per-agent role (committed templates) -
def test_build_system_has_global_frame_law_and_role():
    sysp = build_system("daily", law="  - family (sticky): kin")
    assert "memory-keeper" in sysp                       # mission (global)
    assert "GOLDEN RULE" in sysp and "assume" in sysp.lower()   # golden rule (global)
    assert "family (sticky): kin" in sysp                # law injected into system
    assert "JSON" in sysp                                # output-format contract (global)
    assert "DAILY" in sysp                               # the per-agent role


def test_build_system_global_frame_is_identical_across_agents():
    a = build_system("daily", law="  - x (sticky): y")
    b = build_system("weekly", law="  - x (sticky): y")
    glob_a = a.rsplit("Your role:", 1)[0]                # everything before the role section
    glob_b = b.rsplit("Your role:", 1)[0]
    assert glob_a == glob_b and "memory-keeper" in glob_a   # the shared frame is byte-identical


def test_build_system_renders_all_three_committed_roles():
    for mode in ("daily", "weekly", "monthly"):
        assert "memory-keeper" in build_system(mode, law="  - family (sticky): kin")


def test_build_system_injects_today_and_who_am_i():
    sysp = build_system("daily", law="  - x (sticky): y",
                        who_am_i="Sam, a grad student in Boston.", today="2026-06-10 09:00")
    assert "2026-06-10 09:00" in sysp
    assert "grad student in Boston" in sysp


def test_build_system_defaults_read_sensibly_when_omitted():
    sysp = build_system("daily", law="  - x (sticky): y")   # the pre-M6 call shape still works
    assert "${today}" not in sysp and "${who_am_i}" not in sysp
    assert "Today is" in sysp                               # the frame line survives with a fallback


# ---- build_user: per-conversation data --------------------------------------
def test_build_user_daily_has_name_count_and_messages():
    user = build_user("daily", {"name": "Avery", "kind": "1:1", "identity": "i", "summary": "s",
                                "monthly": "m", "weekly": "(none)", "daily": "(none)",
                                "history": "(none)", "msg_count": 1, "messages": "  [t] Avery: hi"})
    assert "Avery (1:1)" in user and "hi" in user and "1 message" in user
