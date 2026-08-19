"""Delivery backends.

Log hygiene rule (design §8.4): nothing here ever logs an address or a
credential — only recipient *counts* and backend names.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

import requests

from .config import Settings

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    """Delivery failed. Message text must never contain addresses/credentials."""


class Mailer(Protocol):
    name: str

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> None: ...


def _check_recipients(recipients: Sequence[str]) -> list[str]:
    cleaned = [r.strip() for r in recipients if r and r.strip()]
    bad = [r for r in cleaned if "@" not in r or any(c.isspace() for c in r)]
    if bad:
        # count only — do not echo the offending strings
        raise MailError(f"{len(bad)} recipient address(es) are malformed")
    return cleaned


class SmtpMailer:
    """SMTP with STARTTLS + LOGIN (Gmail app-password flow). Recipients go as BCC envelope only."""

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        sender: str,
        *,
        starttls: bool = True,
        timeout: float = 30.0,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        if not sender:
            raise MailError("SMTP sender address (SMTP_FROM / SMTP_USERNAME) is not set")
        self.host, self.port = host, port
        self._username, self._password = username, password
        self.sender = sender
        self._starttls = starttls
        self._timeout = timeout
        self._factory = smtp_factory

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> None:
        rcpts = _check_recipients(recipients)
        if not rcpts:
            raise MailError("no recipients configured")
        if "Bcc" in message:
            del message["Bcc"]  # never leak the list into headers
        try:
            with self._factory(self.host, self.port, timeout=self._timeout) as smtp:
                smtp.ehlo()
                if self._starttls:
                    smtp.starttls()
                    smtp.ehlo()
                if self._username and self._password:
                    smtp.login(self._username, self._password)
                refused = smtp.send_message(message, from_addr=self.sender, to_addrs=rcpts)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"SMTP delivery failed: {type(exc).__name__}") from exc
        if refused:
            raise MailError(f"SMTP server refused {len(refused)} of {len(rcpts)} recipients")
        log.info("sent digest via SMTP to %d recipient(s)", len(rcpts))


class ButtondownMailer:
    """Phase-3 backend: post the rendered digest to Buttondown; they own the audience."""

    name = "buttondown"
    api_url = "https://api.buttondown.com/v1/emails"

    def __init__(
        self, api_key: str, *, http: requests.Session | None = None, timeout: float = 30.0
    ) -> None:
        if not api_key:
            raise MailError("BUTTONDOWN_API_KEY is not set")
        self._key = api_key
        self._http = http or requests.Session()
        self._timeout = timeout

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> None:
        # `recipients` is ignored on purpose: the subscriber list lives in Buttondown.
        html_part = message.get_body(preferencelist=("html",))
        body = html_part.get_content() if html_part else message.get_content()
        try:
            resp = self._http.post(
                self.api_url,
                headers={"Authorization": f"Token {self._key}"},
                json={"subject": message["Subject"], "body": body, "status": "about_to_send"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise MailError(f"Buttondown request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise MailError(f"Buttondown returned HTTP {resp.status_code}")
        log.info("posted digest to Buttondown")


class FileMailer:
    """Writes the message to a directory instead of sending (dry-run / local dev)."""

    name = "file"

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.written: list[Path] = []

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> None:
        feed = message.get("X-Billwatch-Feed", "digest")
        stem = self.out_dir / f"{feed}"
        html_part = message.get_body(preferencelist=("html",))
        text_part = message.get_body(preferencelist=("plain",))
        paths = []
        if html_part is not None:
            p = stem.with_suffix(".html")
            p.write_text(html_part.get_content(), encoding="utf-8")
            paths.append(p)
        if text_part is not None:
            p = stem.with_suffix(".txt")
            p.write_text(text_part.get_content(), encoding="utf-8")
            paths.append(p)
        self.written.extend(paths)
        log.info(
            "wrote digest to %s (%d recipient(s) would be BCC'd)", self.out_dir, len(recipients)
        )


class ConsoleMailer:
    """Prints the plain-text digest to stdout. Handy for local runs."""

    name = "console"

    def __init__(self, write: Callable[[str], object] = print) -> None:
        self._write = write

    def send(self, message: EmailMessage, recipients: Sequence[str]) -> None:
        text_part = message.get_body(preferencelist=("plain",))
        body = text_part.get_content() if text_part is not None else message.get_content()
        self._write(f"Subject: {message['Subject']}\n\n{body}")
        log.info("printed digest to console (%d recipient(s) would be BCC'd)", len(recipients))


def make_mailer(settings: Settings) -> Mailer:
    """Build the configured backend from runtime settings."""
    kind = settings.mailer
    if kind == "smtp":
        return SmtpMailer(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password,
            settings.smtp_from or "",
            starttls=settings.smtp_starttls,
        )
    if kind == "buttondown":
        return ButtondownMailer(settings.buttondown_api_key or "")
    if kind == "console":
        return ConsoleMailer()
    raise MailError(f"unknown mailer backend {kind!r} (expected smtp|buttondown|console)")
