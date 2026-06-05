"""conditions.yaml is the single steering surface. config.py makes it the source of truth: typed,
validated, organized into the user-facing concerns (messages / engine / server; tags live in watch.md),
with defaults that equal the designed behavior."""
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
    assert c.messages.weekly_days == 7
    assert c.messages.monthly_days == 30
    assert c.messages.context_messages == 10
    assert c.messages.unresponded_lookback_days == 90
    assert c.messages.include_groups is True
    assert c.messages.min_handle_digits == 10
    assert c.messages.spam_floor == 1               # storage floor: drop sub-1-message (spam) convos
    assert c.messages.backfill_years == 3           # admission backfill reach
    assert c.messages.summarize_floor == 5          # daily skips < 5 new messages
    assert c.engine.provider == "litellm"
    assert c.engine.max_concurrency == 8
    assert c.engine.models.daily == "anthropic/claude-sonnet-4-6"
    assert c.engine.max_raw_messages.monthly == 0
    assert c.server.url == ""                       # blank = the local server on this host
    assert c.server.bind == "127.0.0.1:8787"        # where `serve` listens by default
    assert c.server.raw_store_days == 0
    assert c.server.mcp_default_lookback_days == 60  # MCP default look-back window
    assert c.server.bootstrap_limit == 0            # uncapped unattended runs by default
    assert c.server.schedule.timezone == "auto"


def test_missing_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT_TRIAGE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no conditions.yaml here
    assert load_config() == Config()


# ----------------------------------------------------------------- loading
def test_loads_overrides_from_yaml(tmp_path):
    p = write_yaml(tmp_path, "messages:\n  monthly_days: 60\n  context_messages: 0\n")
    c = load_config(p)
    assert c.messages.monthly_days == 60
    assert c.messages.context_messages == 0
    assert c.messages.weekly_days == 7  # untouched key keeps its default


def test_engine_section_overrides(tmp_path):
    p = write_yaml(tmp_path, "engine:\n  max_concurrency: 4\n  models:\n    daily: openai/gpt-x\n")
    c = load_config(p)
    assert c.engine.max_concurrency == 4
    assert c.engine.models.daily == "openai/gpt-x"
    assert c.engine.models.weekly == "anthropic/claude-opus-4-8"  # untouched default keeps


def test_server_section_overrides(tmp_path):
    p = write_yaml(tmp_path, 'server:\n  url: "https://triage.host"\n  bind: "0.0.0.0:9000"\n')
    c = load_config(p)
    assert c.server.url == "https://triage.host"    # set => push raw to this remote server
    assert c.server.bind == "0.0.0.0:9000"          # expose on LAN/VPS
    assert c.server.raw_store_days == 0             # untouched default keeps


def test_explicit_missing_path_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_env_var_path_is_used(tmp_path, monkeypatch):
    p = write_yaml(tmp_path, "messages:\n  weekly_days: 14\n")
    monkeypatch.setenv("TEXT_TRIAGE_CONFIG", str(p))
    assert load_config().messages.weekly_days == 14


# ----------------------------------------------------------------- validation / robustness
def test_malformed_yaml_raises_config_error(tmp_path):
    p = tmp_path / "conditions.yaml"
    p.write_text("messages:\n  weekly_days: : : oops\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_value_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_yaml(tmp_path, "messages:\n  weekly_days: not_a_number\n"))


def test_unknown_key_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_yaml(tmp_path, "messages:\n  bogus_knob: 5\n"))


def test_raw_store_days_zero_allowed_means_keep_forever(tmp_path):
    c = load_config(write_yaml(tmp_path, "server:\n  raw_store_days: 0\n"))
    assert c.server.raw_store_days == 0  # 0 = no pruning (keep raw forever)


def test_negative_raw_store_days_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_yaml(tmp_path, "server:\n  raw_store_days: -1\n"))


# ----------------------------------------------------------------- the committed file
def test_committed_conditions_yaml_loads():
    """The repo's conditions.yaml must always parse under the model (no stray/typo keys)."""
    c = load_config(REPO_ROOT / "conditions.yaml")
    assert isinstance(c, Config)
    assert c.server.schedule.timezone == "auto"
