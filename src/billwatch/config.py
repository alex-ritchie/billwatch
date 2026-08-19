"""Configuration: feed definitions (feeds.toml) and runtime settings (env vars).

Nothing private lives in feeds.toml. Credentials and recipient lists come
exclusively from the environment (GitHub Actions Secrets or a gitignored .env).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """Raised for invalid feed configuration or missing runtime settings."""


@dataclass
class GlobalSettings:
    hearing_lookahead_days: int = 14
    send_empty: bool = False
    subject_prefix: str = "[billwatch]"
    unsubscribe_note: str = ""
    search_min_relevance: int = 50
    max_queries_per_run: int = 400


@dataclass
class FeedConfig:
    name: str
    state: str
    keywords: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    watch_committees: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    schedule: str = "daily"
    title: str | None = None
    session_id: int | None = None
    recipients_env: str = "RECIPIENTS"
    # Per-feed overrides of global settings (None → inherit)
    hearing_lookahead_days: int | None = None
    send_empty: bool | None = None
    search_min_relevance: int | None = None

    @property
    def display_title(self) -> str:
        return self.title or f"{self.state} — {self.name}"


@dataclass
class Config:
    settings: GlobalSettings
    feeds: dict[str, FeedConfig]

    def feed(self, name: str) -> FeedConfig:
        try:
            return self.feeds[name]
        except KeyError:
            raise ConfigError(f"unknown feed {name!r}; known: {sorted(self.feeds)}") from None

    # Resolved (feed-overrides-global) accessors
    def lookahead_days(self, feed: FeedConfig) -> int:
        return (
            feed.hearing_lookahead_days
            if feed.hearing_lookahead_days is not None
            else self.settings.hearing_lookahead_days
        )

    def send_empty(self, feed: FeedConfig) -> bool:
        return feed.send_empty if feed.send_empty is not None else self.settings.send_empty

    def search_min_relevance(self, feed: FeedConfig) -> int:
        return (
            feed.search_min_relevance
            if feed.search_min_relevance is not None
            else self.settings.search_min_relevance
        )


_FEED_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_STATE_RE = re.compile(r"^[A-Z]{2}$")

_LIST_FIELDS = ("keywords", "searches", "watch_committees", "exclude_keywords")


def _str_list(raw: object, feed: str, key: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"feeds.{feed}.{key} must be a list of strings")
    return [x.strip() for x in raw if x.strip()]


def parse_config(data: dict) -> Config:
    """Build a Config from already-parsed TOML data (dict)."""
    settings_raw = data.get("settings", {}) or {}
    if not isinstance(settings_raw, dict):
        raise ConfigError("[settings] must be a table")
    known = {f for f in GlobalSettings.__dataclass_fields__}
    unknown = set(settings_raw) - known
    if unknown:
        raise ConfigError(f"unknown [settings] keys: {sorted(unknown)}")
    settings = GlobalSettings(**settings_raw)

    feeds_raw = data.get("feeds")
    if not feeds_raw or not isinstance(feeds_raw, dict):
        raise ConfigError("config must define at least one [feeds.<name>] table")

    feeds: dict[str, FeedConfig] = {}
    for name, body in feeds_raw.items():
        if not _FEED_NAME_RE.match(name):
            raise ConfigError(
                f"feed name {name!r} must be lowercase letters/digits/hyphens (e.g. md-opioids)"
            )
        if not isinstance(body, dict):
            raise ConfigError(f"feeds.{name} must be a table")
        body = dict(body)
        state = body.pop("state", None)
        if not isinstance(state, str) or not _STATE_RE.match(state):
            raise ConfigError(f"feeds.{name}.state must be a two-letter code like 'MD' or 'US'")
        lists = {k: _str_list(body.pop(k, None), name, k) for k in _LIST_FIELDS}
        if not lists["keywords"] and not lists["searches"] and not lists["watch_committees"]:
            raise ConfigError(
                f"feeds.{name} needs at least one of keywords, searches, watch_committees"
            )
        session_id = body.pop("session_id", None)
        if session_id is not None and not isinstance(session_id, int):
            raise ConfigError(f"feeds.{name}.session_id must be an integer")
        allowed = {
            "schedule",
            "title",
            "recipients_env",
            "hearing_lookahead_days",
            "send_empty",
            "search_min_relevance",
        }
        unknown = set(body) - allowed
        if unknown:
            raise ConfigError(f"unknown keys in feeds.{name}: {sorted(unknown)}")
        feeds[name] = FeedConfig(name=name, state=state, session_id=session_id, **lists, **body)
    return Config(settings=settings, feeds=feeds)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None
    return parse_config(data)


# --------------------------------------------------------------------------- #
# Runtime settings from environment
# --------------------------------------------------------------------------- #


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> int:
    """Minimal .env loader (KEY=VALUE lines, # comments). Returns count loaded.

    Deliberately dependency-free. Existing environment wins unless override=True.
    """
    path = Path(path)
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value
            loaded += 1
    return loaded


def parse_recipients(raw: str | None) -> list[str]:
    """Split a comma/semicolon/newline separated list, dedupe, keep order."""
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[,;\n]", raw):
        addr = part.strip()
        if addr and addr not in out:
            out.append(addr)
    return out


@dataclass
class Settings:
    """Runtime secrets/settings. Never logged, never persisted."""

    legiscan_api_key: str | None = None
    mailer: str = "smtp"  # smtp | buttondown | console
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    buttondown_api_key: str | None = None
    legiscan_base_url: str = "https://api.legiscan.com/"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def flag(name: str, default: bool) -> bool:
            v = e.get(name)
            if v is None or v == "":
                return default
            return v.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            legiscan_api_key=e.get("LEGISCAN_API_KEY") or None,
            mailer=(e.get("BILLWATCH_MAILER") or "smtp").strip().lower(),
            smtp_host=e.get("SMTP_HOST") or "smtp.gmail.com",
            smtp_port=int(e.get("SMTP_PORT") or 587),
            smtp_username=e.get("SMTP_USERNAME") or None,
            smtp_password=e.get("SMTP_APP_PASSWORD") or e.get("SMTP_PASSWORD") or None,
            smtp_from=e.get("SMTP_FROM") or e.get("SMTP_USERNAME") or None,
            smtp_starttls=flag("SMTP_STARTTLS", True),
            buttondown_api_key=e.get("BUTTONDOWN_API_KEY") or None,
            legiscan_base_url=e.get("LEGISCAN_BASE_URL") or "https://api.legiscan.com/",
        )

    def recipients_for(self, feed: FeedConfig, env: dict[str, str] | None = None) -> list[str]:
        e = os.environ if env is None else env
        return parse_recipients(e.get(feed.recipients_env))

    def __repr__(self) -> str:  # pragma: no cover - safety net against accidental logging
        return f"Settings(mailer={self.mailer!r}, smtp_host={self.smtp_host!r}, secrets=***)"
