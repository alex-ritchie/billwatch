from __future__ import annotations

import pytest

from billwatch.config import (
    ConfigError,
    Settings,
    load_config,
    load_dotenv,
    parse_config,
    parse_recipients,
)


def test_load_repo_config(config_path):
    cfg = load_config(config_path)
    feed = cfg.feed("md-substance-use")
    assert feed.state == "MD"
    assert "naloxone" in feed.keywords
    assert "harm reduction" not in feed.searches and feed.searches[:2] == ["overdose", "opioid"]
    assert "Finance" in feed.watch_committees
    assert feed.recipients_env == "RECIPIENTS"
    assert cfg.settings.hearing_lookahead_days == 14
    assert cfg.settings.send_empty is False
    assert cfg.lookahead_days(feed) == 14


def test_feed_overrides_global_settings():
    cfg = parse_config(
        {
            "settings": {"hearing_lookahead_days": 14, "send_empty": False},
            "feeds": {
                "va-x": {
                    "state": "VA",
                    "keywords": ["a"],
                    "hearing_lookahead_days": 30,
                    "send_empty": True,
                    "search_min_relevance": 80,
                }
            },
        }
    )
    f = cfg.feed("va-x")
    assert cfg.lookahead_days(f) == 30
    assert cfg.send_empty(f) is True
    assert cfg.search_min_relevance(f) == 80
    assert f.display_title == "VA — va-x"


@pytest.mark.parametrize(
    "feeds, msg",
    [
        ({}, "at least one"),
        ({"Bad Name": {"state": "MD", "keywords": ["x"]}}, "feed name"),
        ({"md": {"state": "Maryland", "keywords": ["x"]}}, "two-letter"),
        ({"md": {"state": "MD"}}, "at least one of keywords"),
        ({"md": {"state": "MD", "keywords": "opioid"}}, "list of strings"),
        ({"md": {"state": "MD", "keywords": ["x"], "session_id": "abc"}}, "session_id"),
        ({"md": {"state": "MD", "keywords": ["x"], "bogus": 1}}, "unknown keys"),
    ],
)
def test_invalid_feed_config(feeds, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({"feeds": feeds})


def test_unknown_settings_key_rejected():
    with pytest.raises(ConfigError, match="unknown \\[settings\\] keys"):
        parse_config({"settings": {"nope": 1}, "feeds": {"md": {"state": "MD", "keywords": ["x"]}}})


def test_unknown_feed_lookup(config):
    with pytest.raises(ConfigError, match="unknown feed"):
        config.feed("nope")


def test_missing_and_invalid_toml(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[feeds\nstate = ")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(bad)


def test_parse_recipients_dedupes_and_splits():
    assert parse_recipients("a@example.org, b@example.org;a@example.org\nc@example.org ,") == [
        "a@example.org",
        "b@example.org",
        "c@example.org",
    ]
    assert parse_recipients("") == []
    assert parse_recipients(None) == []


def test_settings_from_env_and_recipients(config):
    env = {
        "LEGISCAN_API_KEY": "k",
        "SMTP_USERNAME": "bot@example.com",
        "SMTP_APP_PASSWORD": "pw",
        "RECIPIENTS": "friend@example.com, you@example.com",
        "SMTP_STARTTLS": "false",
        "BILLWATCH_MAILER": "SMTP",
    }
    s = Settings.from_env(env)
    assert s.legiscan_api_key == "k"
    assert s.smtp_from == "bot@example.com"
    assert s.smtp_starttls is False
    assert s.mailer == "smtp"
    assert s.recipients_for(config.feed("md-substance-use"), env) == [
        "friend@example.com",
        "you@example.com",
    ]
    # per-feed recipient env var
    cfg2 = __import__("billwatch.config", fromlist=["parse_config"]).parse_config(
        {"feeds": {"va": {"state": "VA", "keywords": ["x"], "recipients_env": "RECIPIENTS_VA"}}}
    )
    assert s.recipients_for(cfg2.feed("va"), {**env, "RECIPIENTS_VA": "v@example.org"}) == [
        "v@example.org"
    ]
    assert s.recipients_for(cfg2.feed("va"), env) == []


def test_settings_repr_never_shows_secrets():
    s = Settings.from_env({"LEGISCAN_API_KEY": "SECRET-KEY", "SMTP_APP_PASSWORD": "SECRET-PW"})
    r = repr(s)
    assert "SECRET" not in r


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nFOO_A=1\nexport FOO_B=\"two words\"\nFOO_C='x'\nBROKEN\n")
    monkeypatch.delenv("FOO_A", raising=False)
    monkeypatch.setenv("FOO_B", "preset")
    monkeypatch.delenv("FOO_C", raising=False)
    n = load_dotenv(env)
    assert n == 2
    import os

    assert os.environ["FOO_A"] == "1"
    assert os.environ["FOO_B"] == "preset"  # existing env wins
    assert os.environ["FOO_C"] == "x"
    assert load_dotenv(tmp_path / "nope") == 0
    for k in ("FOO_A", "FOO_C"):
        monkeypatch.delenv(k, raising=False)
