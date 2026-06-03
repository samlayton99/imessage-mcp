"""conditions.yaml is the single steering surface. config.py makes it the source of truth:
typed, validated, with defaults that equal the designed behavior, and the note-array caps DERIVED
from the windows so a cadence change can never conflict with the schema's type contract."""
from pathlib import Path

import pytest

from text_triage.config import Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_yaml(tmp_path, text):
    p = tmp_path / "conditions.yaml"
    p.write_text(text)
    return p


# ----------------------------------------------------------------- defaults
def test_defaults_equal_designed_behavior():
    c = Config()
    assert c.windows.weekly_days == 7
    assert c.windows.monthly_days == 30
    assert c.windows.context_messages == 10
    assert c.windows.raw_store_days == 30
    assert c.windows.unresponded_lookback_days == 90
    assert c.conversation_filter.include_groups is True
    assert c.conversation_filter.min_handle_digits == 10
    assert c.schedule.timezone == "auto"
    assert c.engine.provider == "claude_code"


def test_missing_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT_TRIAGE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no conditions.yaml here
    assert load_config() == Config()


# ----------------------------------------------------------------- loading
def test_loads_overrides_from_yaml(tmp_path):
    p = write_yaml(tmp_path, "windows:\n  monthly_days: 60\n  context_messages: 0\n")
    c = load_config(p)
    assert c.windows.monthly_days == 60
    assert c.windows.context_messages == 0
    assert c.windows.weekly_days == 7  # untouched key keeps its default


def test_explicit_missing_path_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_env_var_path_is_used(tmp_path, monkeypatch):
    p = write_yaml(tmp_path, "windows:\n  weekly_days: 14\n")
    monkeypatch.setenv("TEXT_TRIAGE_CONFIG", str(p))
    assert load_config().windows.weekly_days == 14


# ----------------------------------------------------------------- derived caps
def test_derived_caps_default():
    c = Config()
    assert c.daily_cap == 7                 # = weekly_days
    assert c.weekly_cap == 5                # = ceil(monthly_days / 7) = ceil(30/7)


def test_derived_caps_track_windows(tmp_path):
    c = load_config(write_yaml(tmp_path, "windows:\n  weekly_days: 10\n  monthly_days: 60\n"))
    assert c.daily_cap == 10                # tracks weekly_days
    assert c.weekly_cap == 9                # ceil(60/7) — no longer conflicts with a hardcoded 5


# ----------------------------------------------------------------- validation / robustness
def test_malformed_yaml_raises_config_error(tmp_path):
    p = tmp_path / "conditions.yaml"
    p.write_text("windows:\n  weekly_days: : : oops\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_value_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_yaml(tmp_path, "windows:\n  weekly_days: not_a_number\n"))


def test_unknown_key_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_yaml(tmp_path, "windows:\n  bogus_knob: 5\n"))


def test_raw_store_days_zero_allowed_means_keep_forever(tmp_path):
    c = load_config(write_yaml(tmp_path, "windows:\n  raw_store_days: 0\n"))
    assert c.windows.raw_store_days == 0  # 0 = no pruning (keep raw forever)


def test_negative_raw_store_days_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_yaml(tmp_path, "windows:\n  raw_store_days: -1\n"))


# ----------------------------------------------------------------- the committed file
def test_committed_conditions_yaml_loads():
    """The repo's conditions.yaml must always parse under the model (no stray/typo keys)."""
    c = load_config(REPO_ROOT / "conditions.yaml")
    assert isinstance(c, Config)
    assert c.schedule.timezone == "auto"
