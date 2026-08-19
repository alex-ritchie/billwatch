"""Enforce the design's supply-chain and privacy rules on the repo itself (design §8, NFR7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

USES_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
SHA_PIN_RE = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def _workflow_files(repo_root: Path) -> list[Path]:
    return sorted((repo_root / ".github" / "workflows").glob("*.yml"))


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_workflows_exist(repo_root):
    names = {p.name for p in _workflow_files(repo_root)}
    assert {"digest.yml", "ci.yml"} <= names


@pytest.mark.parametrize("name", ["digest.yml", "ci.yml"])
def test_every_action_is_pinned_to_full_sha_with_version_comment(repo_root, name):
    text = (repo_root / ".github" / "workflows" / name).read_text()
    uses = USES_RE.findall(text)
    assert uses, "no `uses:` lines found"
    for ref in uses:
        if ref.startswith("./"):
            continue  # local composite actions are part of the repo
        assert SHA_PIN_RE.match(ref), f"{name}: {ref!r} is not pinned to a 40-char commit SHA"
    # each pin carries a trailing "# vX.Y.Z" comment (policy (a) in §8.3)
    for line in text.splitlines():
        if "uses:" in line and "@" in line:
            assert re.search(r"#\s*v\d+(\.\d+)*", line), f"{name}: missing version comment: {line}"


def test_no_pull_request_target_anywhere(repo_root):
    for p in _workflow_files(repo_root):
        assert "pull_request_target" not in _strip_comments(p.read_text()), p.name


def test_digest_workflow_least_privilege_and_secrets_wiring(repo_root):
    text = (repo_root / ".github" / "workflows" / "digest.yml").read_text()
    assert re.search(r"permissions:\s*\n\s*contents:\s*write\s*\n", _strip_comments(text))
    assert "id-token" not in text and "packages:" not in text
    for secret in ("LEGISCAN_API_KEY", "SMTP_USERNAME", "SMTP_APP_PASSWORD", "RECIPIENTS"):
        assert f"${{{{ secrets.{secret} }}}}" in text, secret
    assert "uv sync --frozen" in text
    assert "cron:" in text and "workflow_dispatch" in text


def test_ci_workflow_is_read_only_and_uses_fixtures(repo_root):
    text = (repo_root / ".github" / "workflows" / "ci.yml").read_text()
    assert re.search(r"permissions:\s*\n\s*contents:\s*read\s*\n", _strip_comments(text))
    assert "secrets." not in text
    assert "--fixtures tests/fixtures/legiscan" in text


def test_dependabot_covers_actions_and_uv(repo_root):
    text = (repo_root / ".github" / "dependabot.yml").read_text()
    assert 'package-ecosystem: "github-actions"' in text
    assert 'package-ecosystem: "uv"' in text


def test_gitignore_covers_env_and_scratch(repo_root):
    text = (repo_root / ".gitignore").read_text()
    for pat in (".env", "out/"):
        assert pat in text.split(), pat


def test_repo_contains_no_email_addresses_outside_examples(repo_root):
    """Design §8.1: no real addresses anywhere. Only RFC 2606 example domains are allowed."""
    email_re = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
    allowed_suffixes = ("@example.com", "@example.org", "@example.net", "@localhost")
    allowed_literals = {  # the bot's own sender identity (public — it is the From: of every digest)
        "actions@github.com",
        "od.bill.watch@gmail.com",  # the dedicated bot account (README, .env.example)
        "mdbillwatch@gmail.com",  # the design doc's original hypothetical name
    }
    skip_dirs = {".git", ".venv", "out", "__pycache__", ".pytest_cache", ".ruff_cache", "state"}
    for path in repo_root.rglob("*"):
        if any(part in skip_dirs for part in path.parts) or not path.is_file():
            continue
        if path.suffix in {".db", ".lock", ".pyc", ".png", ".jpg"}:
            continue
        # Local, gitignored credential files legitimately hold real addresses. They never
        # reach the repo, so they are out of scope here. (.env.example IS committed → scanned.)
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in email_re.findall(text):
            ok = m.endswith(allowed_suffixes) or m in allowed_literals
            assert ok, f"{path.relative_to(repo_root)}: {m}"
