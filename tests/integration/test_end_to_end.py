"""End-to-end: real CLI → real HTTP (LegiScan stand-in) → real SQLite → real SMTP (stand-in)."""

from __future__ import annotations

import email
import email.policy
import logging
import socket
import sqlite3

import pytest

from billwatch.__main__ import EXIT_FAILED, EXIT_OK, main
from tests.integration.servers import LegiScanServer, SmtpServer

pytestmark = pytest.mark.integration

RECIPIENTS = ["friend@example.com", "you@example.com"]


@pytest.fixture
def legiscan(day1_dir):
    with LegiScanServer(day1_dir) as srv:
        yield srv


@pytest.fixture
def smtp():
    with SmtpServer(require_auth=True) as srv:
        yield srv


@pytest.fixture
def env(monkeypatch, legiscan, smtp):
    monkeypatch.setenv("LEGISCAN_API_KEY", legiscan.api_key)
    monkeypatch.setenv("LEGISCAN_BASE_URL", legiscan.base_url)
    monkeypatch.setenv("BILLWATCH_MAILER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(smtp.port))
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    monkeypatch.setenv("SMTP_USERNAME", "billwatch-bot@example.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "app-password-secret")
    monkeypatch.setenv("SMTP_FROM", "billwatch-bot@example.com")
    monkeypatch.setenv("RECIPIENTS", ", ".join(RECIPIENTS))
    return monkeypatch


def _run(cmd, config_path, db, *more):
    return main(["--env-file", "", cmd, "--config", str(config_path), "--db", str(db), *more])


def _parse(msg_bytes: bytes):
    return email.message_from_bytes(msg_bytes, policy=email.policy.default)


def test_full_daily_cycle(env, legiscan, smtp, tmp_path, config_path, day1_dir, day2_dir, caplog):
    db = tmp_path / "state" / "billwatch.db"

    # ---- day 1: first run, everything is new -------------------------------
    with caplog.at_level(logging.INFO):
        assert _run("run", config_path, db, "--today", "2026-02-01") == EXIT_OK
    assert len(smtp.messages) == 1
    m = smtp.messages[0]
    assert m.mail_from == "billwatch-bot@example.com"
    assert m.rcpt_to == RECIPIENTS  # BCC via envelope only
    assert m.auth == ("billwatch-bot@example.com", "app-password-secret")
    msg = _parse(m.data)
    assert "3 new, 1 hearing, 1 to review" in msg["Subject"]
    assert msg["To"] == "billwatch-bot@example.com"
    assert "Bcc" not in msg and "Cc" not in msg
    for r in RECIPIENTS:
        assert r not in m.data.decode()  # recipients never appear in the message itself
    html = msg.get_body(preferencelist=("html",)).get_content()
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "HB101" in html and "HB600" not in html
    assert "NEW BILLS (3)" in text and "COMMITTEE WATCH (1)" in text
    assert "https://mgaleg.maryland.gov" in html
    # LegiScan traffic: sessions + masterlist + 3 searches + 7 details, key sent on each
    ops = [r["op"] for r in legiscan.requests]
    assert ops.count("getSessionList") == 1 and ops.count("getMasterListRaw") == 1
    assert ops.count("getSearchRaw") == 3 and ops.count("getBill") == 7
    assert all(r["key"] == legiscan.api_key for r in legiscan.requests)
    assert legiscan.requests[1] == {"key": "test-key", "op": "getMasterListRaw", "id": "2200"}
    # log hygiene
    for secret in ("app-password-secret", *RECIPIENTS, "test-key"):
        assert secret not in caplog.text
    assert "2 recipient(s)" in caplog.text
    # persisted state
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM bills WHERE tracked=1").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM events WHERE sent=0").fetchone()[0] == 0
    row = con.execute("SELECT new_bills, hearings, watch, sent, queries FROM sent_log").fetchone()
    assert row == (3, 1, 1, 1, 12)
    con.close()

    # ---- day 1 again: nothing changed → no email, only 5 queries ------------
    n_before = len(legiscan.requests)
    assert _run("run", config_path, db, "--today", "2026-02-02") == EXIT_OK
    assert len(smtp.messages) == 1
    assert len(legiscan.requests) - n_before == 5

    # ---- day 2: switch the server to the day-2 recording --------------------
    legiscan.fixture_dir = day2_dir
    n_before = len(legiscan.requests)
    assert _run("run", config_path, db, "--today", "2026-02-14") == EXIT_OK
    assert len(smtp.messages) == 2
    text = _parse(smtp.messages[1].data).get_body(preferencelist=("plain",)).get_content()
    assert "NEW BILLS (1)" in text and "HB600" in text
    assert "MOVEMENT (1)" in text and "Introduced -> Engrossed" in text
    assert "UPCOMING HEARINGS" in text and "2026-02-24" in text and "2026-02-25" in text
    assert [r["op"] for r in legiscan.requests[n_before:]].count("getBill") == 3


def test_transient_http_errors_are_retried(env, legiscan, smtp, tmp_path, config_path):
    legiscan.fail_first = 2  # first two HTTP requests 503; client retries with backoff
    db = tmp_path / "bw.db"
    assert _run("run", config_path, db, "--today", "2026-02-01") == EXIT_OK
    assert len(smtp.messages) == 1
    assert [r["op"] for r in legiscan.requests[:3]] == ["getSessionList"] * 3


def test_invalid_api_key_fails_cleanly(
    env, legiscan, smtp, tmp_path, config_path, monkeypatch, caplog
):
    monkeypatch.setenv("LEGISCAN_API_KEY", "wrong-key")
    db = tmp_path / "bw.db"
    with caplog.at_level(logging.ERROR):
        assert _run("run", config_path, db, "--today", "2026-02-01") == EXIT_FAILED
    assert smtp.messages == []
    assert "Invalid API key" in caplog.text
    assert "wrong-key" not in caplog.text
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    con.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_smtp_outage_keeps_state_and_catches_up(
    env, legiscan, smtp, tmp_path, config_path, monkeypatch, day2_dir
):
    db = tmp_path / "bw.db"
    monkeypatch.setenv("SMTP_PORT", str(_free_port()))  # nothing listening
    assert _run("run", config_path, db, "--today", "2026-02-01") == EXIT_FAILED
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM bills WHERE tracked=1").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM events WHERE sent=0").fetchone()[0] == 4
    assert con.execute("SELECT sent, skipped FROM sent_log").fetchone() == (0, 0)
    con.close()
    # SMTP is back the next day, and day-2 changes have landed: one combined digest
    monkeypatch.setenv("SMTP_PORT", str(smtp.port))
    legiscan.fixture_dir = day2_dir
    assert _run("run", config_path, db, "--today", "2026-02-14") == EXIT_OK
    assert len(smtp.messages) == 1
    text = _parse(smtp.messages[0].data).get_body(preferencelist=("plain",)).get_content()
    assert "NEW BILLS (4)" in text and "COMMITTEE WATCH (1)" in text
    assert "UPCOMING HEARINGS — next 14 days (2)" in text
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM events WHERE sent=0").fetchone()[0] == 0
    con.close()


def test_smtp_rejects_one_recipient(env, tmp_path, config_path, day1_dir):
    with SmtpServer(reject_rcpt={"you@example.com"}) as bad_smtp:
        env.setenv("SMTP_PORT", str(bad_smtp.port))
        db = tmp_path / "bw.db"
        # smtplib raises SMTPRecipientsRefused only if *all* are refused; a partial refusal is
        # returned and billwatch treats it as a delivery failure so nothing is marked sent.
        rc = _run("run", config_path, db, "--today", "2026-02-01")
    assert rc == EXIT_FAILED
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM events WHERE sent=0").fetchone()[0] == 4
    con.close()


def test_test_email_command_delivers_sample(env, smtp, config_path):
    rc = main(["--env-file", "", "test-email", "--config", str(config_path)])
    assert rc == EXIT_OK
    assert len(smtp.messages) == 1
    assert smtp.messages[0].rcpt_to == RECIPIENTS
    msg = _parse(smtp.messages[0].data)
    assert "HB 0000" in msg.get_body(preferencelist=("plain",)).get_content()


def test_dry_run_against_live_server_writes_files_only(env, legiscan, smtp, tmp_path, config_path):
    db = tmp_path / "bw.db"
    out = tmp_path / "out"
    rc = _run("dry-run", config_path, db, "--today", "2026-02-01", "--out", str(out))
    assert rc == EXIT_OK
    assert smtp.messages == []
    assert not db.exists()
    assert "New bills (3)" in (out / "md-substance-use.html").read_text()
