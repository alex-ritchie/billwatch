from __future__ import annotations

from datetime import date

from billwatch.__main__ import EXIT_OK, EXIT_USAGE, main
from billwatch.digest import build_summary_email, render_summary_html, render_summary_text
from billwatch.models import RelatedBill
from billwatch.store import Store
from billwatch.summary import bill_sort_key, build_summary, categorize
from tests.conftest import make_bill

FEED = "md-substance-use"
NOW = "2026-08-20T12:00:00+00:00"


def _mk(
    store,
    bill_id,
    number,
    *,
    status=1,
    last_action=None,
    last_action_date="2026-04-13",
    title=None,
    sasts=(),
    tracked=True,
    committee="Health and Government Operations",
):
    b = make_bill(
        bill_id, number, title=title or f"Bill {number}", status=status, committee=committee
    )
    b.last_action, b.last_action_date = last_action, last_action_date
    b.sasts = list(sasts)
    store.upsert_bill(FEED, b, tracked=tracked, reasons=["keyword: opioid"], when=NOW)
    return b


# --------------------------------------------------------------------------- #
# categorize
# --------------------------------------------------------------------------- #


def test_categorize_buckets():
    def cat(status, action):
        b = make_bill(status=status)
        b.last_action = action
        return categorize(b)

    assert cat(4, "Approved by the Governor - Chapter 12") == "enacted"
    assert cat(4, "Enacted under Article II, Section 17(c) - Chapter 903") == "enacted"
    assert cat(8, "Chaptered") == "enacted"
    assert cat(7, "Veto overridden") == "enacted"
    assert cat(5, "Vetoed by the Governor (Policy)") == "vetoed"
    assert cat(2, "Vetoed by the Governor") == "vetoed"  # action wins over stale status
    assert cat(4, "Returned Passed") == "passed"
    assert cat(3, "Enrolled") == "passed"
    assert cat(1, "Hearing 2/10 at 2:30 p.m.") == "stalled"
    assert cat(2, "Third Reading Passed (31-6)") == "stalled"
    assert cat(6, "Unfavorable Report") == "stalled"
    assert cat(None, None) == "stalled"


def test_bill_sort_key_natural_order():
    nums = ["HB1012", "HB84", "SB9", "HB222", "SB101"]
    assert sorted(nums, key=bill_sort_key) == ["HB84", "HB222", "HB1012", "SB9", "SB101"]


# --------------------------------------------------------------------------- #
# build_summary
# --------------------------------------------------------------------------- #


def _seed(store):
    _mk(
        store,
        1,
        "HB 84",
        status=4,
        last_action="Approved by the Governor - Chapter 12",
        title="Naloxone Access Act",
    )
    _mk(
        store,
        2,
        "SB 906",
        status=5,
        last_action="Vetoed by the Governor (Policy)",
        title="Fentanyl Distribution Penalties",
    )
    _mk(store, 3, "SB 244", status=4, last_action="Returned Passed", title="Medicaid Code Revision")
    _mk(
        store,
        4,
        "HB 341",
        status=1,
        last_action="Hearing 2/10 at 2:30 p.m.",
        title="Boys' and Men's Health Commission",
    )
    # cross-filed pair with mixed outcome: HB enacted, SB died on the floor
    _mk(
        store,
        5,
        "HB 417",
        status=4,
        last_action="Approved by the Governor - Chapter 640",
        title="Xylazine Schedule III",
        sasts=[RelatedBill(6, "SB435", "Crossfiled")],
    )
    _mk(
        store,
        6,
        "SB 435",
        status=2,
        last_action="Motion Special Order Adopted",
        title="Xylazine Consumer Protection",
        sasts=[RelatedBill(5, "HB417", "Crossfiled")],
    )
    # watch-only: excluded
    _mk(store, 7, "HB 999", status=1, tracked=False)


def test_build_summary_sections_and_pairing(store, config, feed):
    _seed(store)
    s = build_summary(store, config, feed, date(2026, 8, 20))
    assert s.bill_count == 6  # watch-only excluded
    assert [sec.key for sec in s.sections] == ["enacted", "vetoed", "passed", "stalled"]
    by = {sec.key: sec for sec in s.sections}
    assert [e.numbers for e in by["enacted"].entries] == ["HB 84", "HB 417 / SB 435"]
    pair = by["enacted"].entries[1]
    assert pair.is_pair and pair.mixed_outcome and pair.category == "enacted"
    assert pair.primary.number == "HB 417"  # more-advanced twin leads
    assert [e.numbers for e in by["vetoed"].entries] == ["SB 906"]
    assert [e.numbers for e in by["passed"].entries] == ["SB 244"]
    assert [e.numbers for e in by["stalled"].entries] == ["HB 341"]
    assert s.entry_count == 5
    assert s.counts_line == "2 became law · 1 vetoed · 1 passed the legislature · 1 did not advance"
    assert s.session_name == "2026 Regular Session"
    assert s.subject == (
        "[billwatch] 2026 Regular Session — session summary: "
        "Maryland substance use & overdose legislation"
    )


def test_build_summary_empty_and_session_filter(store, config, feed):
    s = build_summary(store, config, feed, date(2026, 8, 20))
    assert s.is_empty and s.bill_count == 0 and s.counts_line == "no matching bills"
    _seed(store)
    s = build_summary(store, config, feed, date(2026, 8, 20), session_id=9999)
    assert s.is_empty  # all seeded bills are session 2200


def test_render_summary_html_and_text(store, config, feed):
    _seed(store)
    s = build_summary(store, config, feed, date(2026, 8, 20))
    html = render_summary_html(s)
    text = render_summary_text(s)
    for needle in (
        "✅ Became law (2)",
        "🚫 Vetoed (1)",
        "📬 Passed the legislature (1)",
        "🛑 Did not advance (1)",
        "HB 417 / SB 435",
        "(cross-filed)",
        "Approved by the Governor - Chapter 640",
        "Motion Special Order Adopted",
        "6 bills tracked",
        "Awaiting the Governor",
        "session summary",
        "Reply to unsubscribe.",
    ):
        assert needle in html, needle
    for needle in (
        "BECAME LAW (2)",
        "VETOED (1)",
        "PASSED THE LEGISLATURE (1)",
        "DID NOT ADVANCE (1)",
        "HB 417 / SB 435 (cross-filed)",
        "SB 435: Motion Special Order Adopted",
        "Last committee: Health and Government Operations",
    ):
        assert needle in text, needle
    assert "\n\n\n" not in text
    # stalled section shows the committee where it died; enacted does not
    assert text.index("Last committee") > text.index("DID NOT ADVANCE")


def test_summary_email_structure(store, config, feed):
    _seed(store)
    s = build_summary(store, config, feed, date(2026, 8, 20))
    msg = build_summary_email(s, "bot@example.com")
    assert msg["Subject"] == s.subject
    assert "Bcc" not in msg and msg["To"] == "bot@example.com"
    assert msg.get_content_type() == "multipart/alternative"
    assert "BECAME LAW" in msg.get_body(preferencelist=("plain",)).get_content()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _seeded_db(tmp_path):
    db = tmp_path / "bw.db"
    with Store(db) as store:
        _seed(store)
        store.commit()
    return db


def test_cli_summary_renders_files(env_console, tmp_path, config_path, capsys):
    db = _seeded_db(tmp_path)
    out = tmp_path / "out"
    rc = main(
        [
            "--env-file",
            "",
            "summary",
            "--config",
            str(config_path),
            "--db",
            str(db),
            "--out",
            str(out),
        ]
    )
    assert rc == EXIT_OK
    html = (out / "md-substance-use-summary.html").read_text()
    assert "Became law (2)" in html
    assert (out / "md-substance-use-summary.txt").exists()


def test_cli_summary_send_console_and_to_override(env_console, tmp_path, config_path, capsys):
    db = _seeded_db(tmp_path)
    rc = main(
        [
            "--env-file",
            "",
            "summary",
            "--config",
            str(config_path),
            "--db",
            str(db),
            "--send",
            "--to",
            "friend@example.com",
        ]
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "session summary" in out and "BECAME LAW (2)" in out


def test_cli_summary_send_requires_recipients(env_console, monkeypatch, tmp_path, config_path):
    monkeypatch.delenv("RECIPIENTS")
    db = _seeded_db(tmp_path)
    rc = main(
        ["--env-file", "", "summary", "--config", str(config_path), "--db", str(db), "--send"]
    )
    assert rc == EXIT_USAGE


def test_cli_summary_does_not_touch_db(env_console, tmp_path, config_path):
    db = _seeded_db(tmp_path)
    before = db.read_bytes()
    main(
        [
            "--env-file",
            "",
            "summary",
            "--config",
            str(config_path),
            "--db",
            str(db),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert db.read_bytes() == before
