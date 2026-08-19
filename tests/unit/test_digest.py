from __future__ import annotations

from datetime import date

from billwatch.digest import (
    Digest,
    NewBillItem,
    build_digest,
    build_email,
    render_html,
    render_text,
)
from billwatch.models import Action, Hearing
from tests.conftest import make_bill

FEED = "md-substance-use"
NOW = "2026-02-01T12:00:00+00:00"


def _seed(store):
    b1 = make_bill(1, "HB 101", hearings=[Hearing(1, "2026-02-10", "House HGO", "13:00", "Rm 1")])
    b2 = make_bill(
        2,
        "HB 210",
        title="Substance Use Treatment",
        status=2,
        committee="Finance",
        history=[
            Action("2026-01-14", "First Reading", "H"),
            Action("2026-01-30", "Third Reading Passed", "H"),
        ],
        hearings=[Hearing(2, "2026-03-15", "Senate Finance")],
    )  # beyond 14 days
    b3 = make_bill(3, "HB 300", title="Licensing", synopsis="renewals")
    for b, tracked, reasons in (
        (b1, True, ["keyword: opioid"]),
        (b2, True, ["keyword: substance use"]),
        (b3, False, ["committee: Health and Government Operations"]),
    ):
        store.upsert_bill(FEED, b, tracked=tracked, reasons=reasons, when=NOW)
        store.replace_hearings(FEED, b.bill_id, b.hearings)
    store.add_event(FEED, 1, "new", {"reasons": ["keyword: opioid"]}, NOW)
    store.add_event(
        FEED,
        2,
        "status",
        {
            "old_status": 1,
            "new_status": 2,
            "old_committee": "HGO",
            "new_committee": "Finance",
            "actions": [{"date": "2026-01-30", "action": "Third Reading Passed", "chamber": "H"}],
        },
        NOW,
    )
    store.add_event(FEED, 3, "watch", {"reasons": ["committee: HGO"]}, NOW)
    return b1, b2, b3


def test_build_digest_sections(store, config, feed):
    _seed(store)
    d = build_digest(store, config, feed, date(2026, 2, 1))
    assert not d.is_empty
    assert [i.bill.number for i in d.new_bills] == ["HB 101"]
    assert d.new_bills[0].reasons == ["keyword: opioid"]
    assert [i.bill.number for i in d.movement] == ["HB 210"]
    m = d.movement[0]
    assert (
        m.status_changed
        and m.old_status_label == "Introduced"
        and m.new_status_label == "Engrossed"
    )
    assert m.committee_changed and m.new_committee == "Finance"
    assert [a.action for a in m.actions] == ["Third Reading Passed"]
    assert [(h.hearing.date, h.bill.number) for h in d.hearings] == [("2026-02-10", "HB 101")]
    assert [i.bill.number for i in d.watch] == ["HB 300"]
    assert d.tracked_count == 2
    assert d.session_name == "2026 Regular Session"
    assert d.summary == "1 new, 1 moved, 1 hearing, 1 to review"
    assert d.subject.startswith(
        "[billwatch] Maryland substance use & overdose legislation — 2026-02-01: "
    )
    assert sorted(d.event_ids) == [1, 2, 3]


def test_build_digest_empty(store, config, feed):
    d = build_digest(store, config, feed, date(2026, 2, 1))
    assert d.is_empty and d.summary == "no changes" and d.event_ids == []


def test_build_digest_merges_repeated_status_events_and_folds_new(store, config, feed):
    """A missed send leaves two status events for one bill: they merge into one movement item.
    A bill that is both 'new' and 'status' in the pending set only appears under New."""
    _seed(store)
    store.add_event(
        FEED,
        2,
        "status",
        {
            "old_status": 2,
            "new_status": 4,
            "old_committee": "Finance",
            "new_committee": "Finance",
            "actions": [{"date": "2026-02-01", "action": "Signed", "chamber": None}],
        },
        NOW,
    )
    store.add_event(FEED, 1, "status", {"old_status": 1, "new_status": 2, "actions": []}, NOW)
    store.add_event(FEED, 1, "new", {"reasons": []}, NOW)  # duplicate new
    d = build_digest(store, config, feed, date(2026, 2, 1))
    assert len(d.new_bills) == 1
    assert [m.bill.bill_id for m in d.movement] == [2]
    m = d.movement[0]
    assert m.old_status == 1 and m.new_status == 4
    assert [a.action for a in m.actions] == ["Third Reading Passed", "Signed"]
    # every pending event (including the folded/merged/duplicate ones) is consumed,
    # otherwise they would resurface in tomorrow's digest
    ids = {e.id for e in store.unsent_events(FEED)}
    assert set(d.event_ids) == ids and len(ids) == 6


def test_build_digest_ignores_events_for_missing_bills(store, config, feed):
    store.add_event(FEED, 999, "new", {}, NOW)
    d = build_digest(store, config, feed, date(2026, 2, 1))
    assert d.is_empty
    assert d.event_ids == [1]  # orphan is consumed so it does not linger forever


def test_render_html_and_text(store, config, feed):
    _seed(store)
    d = build_digest(store, config, feed, date(2026, 2, 1))
    html = render_html(d)
    text = render_text(d)
    for needle in (
        "🆕 New bills (1)",
        "🔄 Movement (1)",
        "📅 Upcoming hearings",
        "👀 Committee watch",
        "HB 101",
        "HB 210",
        "HB 300",
        "Introduced → <strong>Engrossed</strong>",
        "Third Reading Passed",
        "2026-02-10",
        "House HGO",
        "Rm 1",
        "https://legiscan.com/MD/bill/HB101/2026",
        "MGA",
        "Reply to unsubscribe.",
        "Tracked bills in this feed: 2",
    ):
        assert needle in html, needle
    for needle in (
        "NEW BILLS (1)",
        "MOVEMENT (1)",
        "UPCOMING HEARINGS",
        "COMMITTEE WATCH (1)",
        "Introduced -> Engrossed",
        "HB 210",
        "2026-02-10 13:00",
        "Reply to unsubscribe.",
    ):
        assert needle in text, needle
    assert "\n\n\n" not in text  # blank lines collapsed


def test_render_html_escapes_untrusted_content(config, feed):
    b = make_bill(title="<script>alert(1)</script> & co", synopsis="x < y")
    d = Digest(feed=feed, run_date="2026-02-01", lookahead_days=14)
    d.new_bills.append(NewBillItem(bill=b, reasons=[], event_id=1))
    html = render_html(d)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp; co" in html
    text = render_text(d)
    assert "<script>alert(1)</script> & co" in text  # plain text is not escaped


def test_render_empty_digest(config, feed):
    d = Digest(feed=feed, run_date="2026-02-01", lookahead_days=14)
    assert "Nothing new since the last digest" in render_html(d)
    assert "Nothing new since the last digest" in render_text(d)


def test_build_email_structure_and_no_recipient_headers(store, config, feed):
    _seed(store)
    d = build_digest(store, config, feed, date(2026, 2, 1))
    msg = build_email(d, "bot@example.com")
    assert msg["Subject"] == d.subject
    assert msg["From"] == "bot@example.com" and msg["To"] == "bot@example.com"
    assert "Bcc" not in msg and "Cc" not in msg
    assert msg["X-Billwatch-Feed"] == FEED
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type() for p in msg.iter_parts()}
    assert parts == {"text/plain", "text/html"}
    assert "HB 101" in msg.get_body(preferencelist=("html",)).get_content()
