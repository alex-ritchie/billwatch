"""Shared test fixtures. Everything runs offline against recorded JSON."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from billwatch.config import Config, FeedConfig, GlobalSettings, parse_config
from billwatch.models import Action, Bill, Hearing
from billwatch.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DAY1 = FIXTURES / "legiscan"
DAY2 = FIXTURES / "legiscan_day2"
REPO_ROOT = FIXTURES.parent.parent


@pytest.fixture
def day1_dir() -> Path:
    return DAY1


@pytest.fixture
def day2_dir() -> Path:
    return DAY2


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def config_path() -> Path:
    return REPO_ROOT / "config" / "feeds.toml"


def make_config(**overrides) -> Config:
    """The repo's real MD feed shape, in-memory, with optional feed-level overrides."""
    feed = {
        "state": "MD",
        "title": "Maryland substance use & overdose legislation",
        "keywords": [
            "opioid",
            "overdose",
            "naloxone",
            "fentanyl",
            "xylazine",
            "harm reduction",
            "controlled dangerous substance",
            "buprenorphine",
            "methadone",
            "substance use",
            "substance abuse",
            "drug treatment",
            "recovery residence",
            "syringe",
            "prescription drug monitoring",
        ],
        "searches": ["overdose", "opioid", "harm reduction"],
        "watch_committees": ["Health and Government Operations", "Finance"],
        "exclude_keywords": [],
    }
    feed.update(overrides)
    return parse_config(
        {
            "settings": {
                "hearing_lookahead_days": 14,
                "send_empty": False,
                "subject_prefix": "[billwatch]",
                "unsubscribe_note": "Reply to unsubscribe.",
                "search_min_relevance": 50,
                "max_queries_per_run": 400,
            },
            "feeds": {"md-substance-use": feed},
        }
    )


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def feed(config: Config) -> FeedConfig:
    return config.feed("md-substance-use")


@pytest.fixture
def settings() -> GlobalSettings:
    return GlobalSettings()


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def today() -> date:
    return date(2026, 2, 1)


def make_bill(
    bill_id: int = 1,
    number: str = "HB 1",
    title: str = "Public Health - Opioid Overdose Prevention",
    synopsis: str = "Requiring naloxone access.",
    committee: str | None = "Health and Government Operations",
    status: int = 1,
    status_date: str = "2026-01-14",
    history: list[Action] | None = None,
    hearings: list[Hearing] | None = None,
    change_hash: str = "h1",
    referrals: list[str] | None = None,
) -> Bill:
    hist = history if history is not None else [Action(status_date, "First Reading", "H")]
    return Bill(
        bill_id=bill_id,
        state="MD",
        number=number,
        title=title,
        synopsis=synopsis,
        url=f"https://legiscan.com/MD/bill/{number.replace(' ', '')}/2026",
        state_url=f"https://mgaleg.maryland.gov/{number.replace(' ', '').lower()}",
        status=status,
        status_date=status_date,
        committee=committee,
        change_hash=change_hash,
        session_id=2200,
        session_name="2026 Regular Session",
        last_action=hist[-1].action if hist else None,
        last_action_date=hist[-1].date if hist else None,
        referrals=referrals if referrals is not None else ([committee] if committee else []),
        sponsors=["Delegate Ames"],
        history=hist,
        hearings=hearings or [],
    )


@pytest.fixture
def env_console(monkeypatch):
    """Minimal environment for CLI tests that should never touch the network or a mailbox."""
    for k in (
        "LEGISCAN_API_KEY",
        "SMTP_USERNAME",
        "SMTP_APP_PASSWORD",
        "SMTP_FROM",
        "RECIPIENTS",
        "BILLWATCH_MAILER",
        "BUTTONDOWN_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BILLWATCH_MAILER", "console")
    monkeypatch.setenv("RECIPIENTS", "friend@example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    return monkeypatch
