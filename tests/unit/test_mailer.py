from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import pytest
import requests

from billwatch.config import Settings
from billwatch.mailer import (
    ButtondownMailer,
    ConsoleMailer,
    FileMailer,
    MailError,
    SmtpMailer,
    make_mailer,
)


def _msg() -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = "Test digest"
    m["From"] = "bot@example.com"
    m["To"] = "bot@example.com"
    m["X-Billwatch-Feed"] = "md-substance-use"
    m.set_content("plain body")
    m.add_alternative("<p>html body</p>", subtype="html")
    return m


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None, *, fail=None, refused=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        self.fail = fail
        self.refused = refused or {}
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append(("quit",))

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, pw):
        self.calls.append(("login", user, pw))
        if self.fail == "login":
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append(("send", from_addr, list(to_addrs), msg))
        if self.fail == "send":
            raise smtplib.SMTPRecipientsRefused({})
        return self.refused


@pytest.fixture(autouse=True)
def _reset():
    FakeSMTP.instances.clear()


def test_smtp_mailer_happy_path_bcc_only(caplog):
    m = SmtpMailer(
        "smtp.example.com",
        587,
        "bot@example.com",
        "app-pw",
        "bot@example.com",
        smtp_factory=FakeSMTP,
    )
    msg = _msg()
    msg["Bcc"] = "leak@example.com"  # must be stripped
    with caplog.at_level(logging.INFO):
        m.send(msg, ["friend@example.com", " you@example.com "])
    smtp = FakeSMTP.instances[0]
    names = [c[0] for c in smtp.calls]
    assert names == ["ehlo", "starttls", "ehlo", "login", "send", "quit"]
    _, from_addr, rcpts, sent_msg = smtp.calls[4]
    assert from_addr == "bot@example.com"
    assert rcpts == ["friend@example.com", "you@example.com"]
    assert "Bcc" not in sent_msg
    assert sent_msg["To"] == "bot@example.com"
    # log hygiene: count only, never addresses or credentials
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "2 recipient" in joined
    for secret in ("friend@example.com", "you@example.com", "app-pw"):
        assert secret not in joined


def test_smtp_mailer_no_starttls_no_login():
    m = SmtpMailer(
        "localhost", 2525, None, None, "bot@example.com", starttls=False, smtp_factory=FakeSMTP
    )
    m.send(_msg(), ["a@example.com"])
    assert [c[0] for c in FakeSMTP.instances[0].calls] == ["ehlo", "send", "quit"]


def test_smtp_mailer_errors_do_not_leak():
    m = SmtpMailer(
        "h",
        1,
        "u",
        "SECRET",
        "bot@example.com",
        smtp_factory=lambda h, p, timeout=None: FakeSMTP(h, p, timeout, fail="login"),
    )
    with pytest.raises(MailError) as ei:
        m.send(_msg(), ["victim@example.com"])
    assert "SECRET" not in str(ei.value) and "victim" not in str(ei.value)
    assert "SMTPAuthenticationError" in str(ei.value)

    m = SmtpMailer(
        "h",
        1,
        None,
        None,
        "bot@example.com",
        starttls=False,
        smtp_factory=lambda h, p, timeout=None: FakeSMTP(
            h, p, timeout, refused={"x@example.com": (550, b"no")}
        ),
    )
    with pytest.raises(MailError, match="refused 1 of 2"):
        m.send(_msg(), ["x@example.com", "y@example.com"])


def test_smtp_mailer_validates_recipients():
    m = SmtpMailer("h", 1, None, None, "bot@example.com", smtp_factory=FakeSMTP)
    with pytest.raises(MailError, match="no recipients"):
        m.send(_msg(), [])
    with pytest.raises(MailError) as ei:
        m.send(_msg(), ["not-an-address", "ok@example.com"])
    assert "1 recipient" in str(ei.value) and "not-an-address" not in str(ei.value)
    with pytest.raises(MailError, match="sender"):
        SmtpMailer("h", 1, None, None, "")


def test_smtp_os_error_wrapped():
    def boom(h, p, timeout=None):
        raise ConnectionRefusedError()

    m = SmtpMailer("h", 1, None, None, "bot@example.com", smtp_factory=boom)
    with pytest.raises(MailError, match="ConnectionRefusedError"):
        m.send(_msg(), ["a@example.com"])


class FakeHttp:
    def __init__(self, status=201, exc=None):
        self.status, self.exc, self.calls = status, exc, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json))
        if self.exc:
            raise self.exc

        class R:
            status_code = self.status

        return R()


def test_buttondown_mailer_posts_html_and_ignores_recipients():
    http = FakeHttp()
    m = ButtondownMailer("bd-key", http=http)
    m.send(_msg(), ["ignored@example.com"])
    url, headers, body = http.calls[0]
    assert url.endswith("/v1/emails")
    assert headers["Authorization"] == "Token bd-key"
    assert body["subject"] == "Test digest" and "<p>html body</p>" in body["body"]
    assert "ignored@example.com" not in str(body)


def test_buttondown_mailer_errors():
    with pytest.raises(MailError):
        ButtondownMailer("")
    with pytest.raises(MailError, match="HTTP 401"):
        ButtondownMailer("k", http=FakeHttp(status=401)).send(_msg(), [])
    with pytest.raises(MailError, match="ConnectionError"):
        ButtondownMailer("k", http=FakeHttp(exc=requests.ConnectionError())).send(_msg(), [])


def test_file_mailer_writes_both_parts(tmp_path):
    m = FileMailer(tmp_path / "out")
    m.send(_msg(), ["a@example.com"])
    assert (tmp_path / "out" / "md-substance-use.html").read_text() == "<p>html body</p>\n"
    assert (tmp_path / "out" / "md-substance-use.txt").read_text() == "plain body\n"
    assert len(m.written) == 2


def test_console_mailer_prints_text():
    out = []
    ConsoleMailer(write=out.append).send(_msg(), [])
    assert out[0].startswith("Subject: Test digest") and "plain body" in out[0]


def test_make_mailer_dispatch():
    assert make_mailer(Settings.from_env({"BILLWATCH_MAILER": "console"})).name == "console"
    s = Settings.from_env({"SMTP_USERNAME": "u@example.com", "SMTP_APP_PASSWORD": "p"})
    smtp = make_mailer(s)
    assert isinstance(smtp, SmtpMailer) and smtp.sender == "u@example.com"
    assert (
        make_mailer(
            Settings.from_env({"BILLWATCH_MAILER": "buttondown", "BUTTONDOWN_API_KEY": "k"})
        ).name
        == "buttondown"
    )
    with pytest.raises(MailError, match="unknown mailer"):
        make_mailer(Settings.from_env({"BILLWATCH_MAILER": "carrier-pigeon"}))
    with pytest.raises(MailError):  # smtp without a sender
        make_mailer(Settings.from_env({}))
