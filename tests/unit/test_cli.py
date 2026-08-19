from __future__ import annotations

import sqlite3

import pytest

from billwatch.__main__ import EXIT_FAILED, EXIT_OK, EXIT_USAGE, build_parser, main
from billwatch.store import Store


@pytest.fixture
def env(monkeypatch):
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


def _args(cmd, *more, config_path, db):
    return ["--env-file", "", cmd, *more, "--config", str(config_path), "--db", str(db)]


def test_parser_subcommands():
    p = build_parser()
    ns = p.parse_args(["run", "--feed", "a", "--feed", "b", "--today", "2026-02-01"])
    assert ns.command == "run" and ns.feeds == ["a", "b"] and ns.today.year == 2026
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--today", "yesterday"])
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_run_with_fixtures_console(env, capsys, tmp_path, config_path, day1_dir, day2_dir):
    db = tmp_path / "bw.db"
    rc = main(
        [
            *_args("run", config_path=config_path, db=db),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-02-01",
        ]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "Subject: [billwatch]" in out and "NEW BILLS (3)" in out  # HB101/SB101 pair counts once
    with Store(db) as s:
        assert s.count_bills("md-substance-use") == 4
    # day 2
    rc = main(
        [
            *_args("run", config_path=config_path, db=db),
            "--fixtures",
            str(day2_dir),
            "--today",
            "2026-02-14",
        ]
    )
    assert rc == EXIT_OK
    assert "MOVEMENT (1)" in capsys.readouterr().out


def test_dry_run_writes_files_and_leaves_db_untouched(env, tmp_path, config_path, day1_dir):
    db = tmp_path / "bw.db"
    out = tmp_path / "out"
    rc = main(
        [
            *_args("dry-run", config_path=config_path, db=db),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-02-01",
            "--out",
            str(out),
        ]
    )
    assert rc == EXIT_OK
    assert not db.exists()
    html = (out / "md-substance-use.html").read_text()
    assert "🆕 New bills (3)" in html
    assert (out / "md-substance-use.txt").exists()


def test_dry_run_uses_copy_of_existing_db(env, tmp_path, config_path, day1_dir, day2_dir):
    db = tmp_path / "bw.db"
    main(
        [
            *_args("run", config_path=config_path, db=db),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-02-01",
        ]
    )
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM events WHERE sent=1").fetchone()[0]
    rc = main(
        [
            *_args("dry-run", config_path=config_path, db=db),
            "--fixtures",
            str(day2_dir),
            "--today",
            "2026-02-14",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == EXIT_OK
    txt = (tmp_path / "out" / "md-substance-use.txt").read_text()
    assert "MOVEMENT (1)" in txt and "NEW BILLS (1)" in txt
    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert after == before  # dry-run added nothing to the real DB


def test_backfill_then_run(env, capsys, tmp_path, config_path, day1_dir):
    db = tmp_path / "bw.db"
    rc = main(
        [
            *_args("backfill", config_path=config_path, db=db),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-01-20",
        ]
    )
    assert rc == EXIT_OK
    assert "Subject:" not in capsys.readouterr().out
    rc = main(
        [
            *_args("run", config_path=config_path, db=db),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-02-01",
        ]
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK and "UPCOMING HEARINGS" in out and "NEW BILLS" not in out


def test_missing_api_key_is_usage_error(env, tmp_path, config_path):
    rc = main([*_args("run", config_path=config_path, db=tmp_path / "x.db")])
    assert rc == EXIT_USAGE


def test_bad_config_is_usage_error(env, tmp_path, day1_dir):
    bad = tmp_path / "feeds.toml"
    bad.write_text('[feeds.md]\nstate = "Maryland"\n')
    rc = main([*_args("run", config_path=bad, db=tmp_path / "x.db"), "--fixtures", str(day1_dir)])
    assert rc == EXIT_USAGE


def test_missing_fixture_dir_is_failure(env, tmp_path, config_path):
    rc = main(
        [
            *_args("run", config_path=config_path, db=tmp_path / "x.db"),
            "--fixtures",
            str(tmp_path / "nope"),
        ]
    )
    assert rc == EXIT_FAILED


def test_test_email_console(env, capsys, config_path):
    rc = main(
        ["--env-file", "", "test-email", "--config", str(config_path), "--to", "me@example.com"]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "Sample Bill" in out and "HB 0000" in out


def test_test_email_requires_recipients(env, monkeypatch, config_path):
    monkeypatch.delenv("RECIPIENTS")
    rc = main(["--env-file", "", "test-email", "--config", str(config_path)])
    assert rc == EXIT_USAGE


def test_env_file_is_loaded(monkeypatch, tmp_path, config_path, day1_dir, capsys):
    for k in ("BILLWATCH_MAILER", "RECIPIENTS", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    envf = tmp_path / ".env"
    envf.write_text("BILLWATCH_MAILER=console\nRECIPIENTS=a@example.com\nSMTP_FROM=b@example.com\n")
    rc = main(
        [
            "--env-file",
            str(envf),
            "run",
            "--config",
            str(config_path),
            "--db",
            str(tmp_path / "bw.db"),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-02-01",
        ]
    )
    assert rc == EXIT_OK and "Subject:" in capsys.readouterr().out
    for k in ("BILLWATCH_MAILER", "RECIPIENTS", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)


def test_max_queries_override(env, capsys, tmp_path, config_path, day1_dir, caplog):
    """--max-queries raises/lowers the per-run budget for one run; 0 means unlimited."""
    import logging

    from billwatch.config import load_config

    fixed = 2 + len(load_config(config_path).feed("md-substance-use").searches)
    db = tmp_path / "bw.db"
    with caplog.at_level(logging.WARNING):
        rc = main(
            [
                *_args("backfill", config_path=config_path, db=db),
                "--fixtures",
                str(day1_dir),
                "--today",
                "2026-01-20",
                "--max-queries",
                str(fixed + 2),  # room for exactly 2 of the 7 detail fetches
            ]
        )
    assert rc == EXIT_OK
    assert "deferring 6 candidate" in caplog.text
    rc = main(
        [
            *_args("backfill", config_path=config_path, db=db),
            "--fixtures",
            str(day1_dir),
            "--today",
            "2026-01-21",
            "--max-queries",
            "0",
        ]
    )
    assert rc == EXIT_OK
    with Store(db) as s:
        assert s.count_bills("md-substance-use") == 4
