from __future__ import annotations

from billwatch.models import Action, Hearing
from billwatch.store import Store
from tests.conftest import make_bill

FEED = "md-substance-use"
NOW = "2026-02-01T12:00:00+00:00"


def test_schema_created_and_idempotent(tmp_path):
    path = tmp_path / "s" / "bw.db"
    with Store(path) as s:
        s.upsert_bill(FEED, make_bill(), tracked=True, reasons=["keyword: opioid"], when=NOW)
        s.commit()
    with Store(path) as s:  # reopen: schema creation must not clobber data
        assert s.count_bills(FEED) == 1


def test_bill_roundtrip_preserves_everything(store):
    b = make_bill(hearings=[Hearing(1, "2026-02-10", "House HGO", "13:00", "Room 241")])
    store.upsert_bill(FEED, b, tracked=True, reasons=["keyword: opioid"], when=NOW)
    store.replace_hearings(FEED, b.bill_id, b.hearings)
    tb = store.get_bill(FEED, 1)
    assert tb is not None and tb.tracked and tb.reasons == ["keyword: opioid"]
    assert tb.first_seen == NOW and tb.last_updated == NOW
    got = tb.bill
    assert got.title == b.title and got.synopsis == b.synopsis
    assert got.status == 1 and got.committee == b.committee
    assert got.referrals == b.referrals and got.sponsors == b.sponsors
    assert got.history == b.history
    assert got.hearings == b.hearings
    assert got.session_name == "2026 Regular Session"
    assert store.get_bill(FEED, 42) is None
    assert store.get_bill("other-feed", 1) is None  # feed-scoped


def test_upsert_updates_but_keeps_first_seen(store):
    store.upsert_bill(FEED, make_bill(status=1), tracked=True, reasons=[], when="t1")
    store.upsert_bill(
        FEED, make_bill(status=4, change_hash="h2"), tracked=True, reasons=["x"], when="t2"
    )
    tb = store.get_bill(FEED, 1)
    assert tb.bill.status == 4 and tb.bill.change_hash == "h2"
    assert tb.first_seen == "t1" and tb.last_updated == "t2"
    assert tb.reasons == ["x"]


def test_tracked_vs_watch_only_counts(store):
    store.upsert_bill(FEED, make_bill(1, "HB 1"), tracked=True, reasons=[], when=NOW)
    store.upsert_bill(FEED, make_bill(2, "HB 2"), tracked=False, reasons=[], when=NOW)
    assert store.count_bills(FEED) == 1
    assert store.count_bills(FEED, tracked_only=False) == 2
    assert [t.bill.number for t in store.tracked_bills(FEED)] == ["HB 1"]


def test_seen_hashes(store):
    assert store.seen_hash("state:MD", 1) is None
    store.mark_seen("state:MD", 1, "a", NOW)
    store.mark_seen("state:MD", 2, "b", NOW)
    store.mark_seen("state:MD", 1, "c", NOW)  # update
    assert store.seen_hashes("state:MD") == {1: "c", 2: "b"}
    assert store.seen_hashes("search:x") == {}


def test_replace_hearings_keeps_announced_flag_and_drops_removed(store):
    b = make_bill()
    store.upsert_bill(FEED, b, tracked=True, reasons=[], when=NOW)
    h1 = Hearing(1, "2026-02-10", "House HGO", "13:00")
    h2 = Hearing(1, "2026-02-20", "Senate Finance", "13:00")
    store.replace_hearings(FEED, 1, [h1, h2])
    store.mark_hearings_announced(FEED, [h1])
    # h1 changes time (same key), h2 disappears, h3 appears
    h1b = Hearing(1, "2026-02-10", "House HGO", "14:00", "Room 1")
    h3 = Hearing(1, "2026-03-01", "House HGO")
    store.replace_hearings(FEED, 1, [h1b, h3])
    all_h = store.hearings_for(FEED, 1)
    assert [h.date for h in all_h] == ["2026-02-10", "2026-03-01"]
    assert all_h[0].time == "14:00" and all_h[0].location == "Room 1"
    upcoming = store.upcoming_hearings(FEED, "2026-02-01", "2026-03-31")
    assert [h.date for h, _ in upcoming] == ["2026-03-01"]  # h1 already announced
    upcoming_all = store.upcoming_hearings(FEED, "2026-02-01", "2026-03-31", unannounced_only=False)
    assert len(upcoming_all) == 2


def test_upcoming_hearings_only_for_tracked_bills_and_within_window(store):
    store.upsert_bill(FEED, make_bill(1, "HB 1"), tracked=True, reasons=[], when=NOW)
    store.upsert_bill(FEED, make_bill(2, "HB 2"), tracked=False, reasons=[], when=NOW)
    store.replace_hearings(
        FEED,
        1,
        [
            Hearing(1, "2026-02-05", "A"),
            Hearing(1, "2026-02-25", "B"),
            Hearing(1, "2026-01-30", "C"),
        ],
    )
    store.replace_hearings(FEED, 2, [Hearing(2, "2026-02-05", "A")])
    got = store.upcoming_hearings(FEED, "2026-02-01", "2026-02-15")
    assert [(h.bill_id, h.date) for h, _ in got] == [(1, "2026-02-05")]
    assert got[0][1].bill.number == "HB 1"


def test_events_lifecycle(store):
    store.upsert_bill(FEED, make_bill(1), tracked=True, reasons=[], when=NOW)
    e1 = store.add_event(FEED, 1, "new", {"reasons": ["keyword: opioid"]}, NOW)
    e2 = store.add_event(FEED, 1, "status", {"old_status": 1, "new_status": 2, "actions": []}, NOW)
    store.add_event("other", 1, "new", {}, NOW)
    evs = store.unsent_events(FEED)
    assert [e.id for e in evs] == [e1, e2]
    assert evs[0].kind == "new" and evs[0].detail == {"reasons": ["keyword: opioid"]}
    store.mark_events_sent([e1])
    assert [e.id for e in store.unsent_events(FEED)] == [e2]
    assert store.mark_all_events_sent(FEED) == 1
    assert store.unsent_events(FEED) == []
    assert len(store.unsent_events("other")) == 1
    store.mark_events_sent([])  # no-op


def test_sent_log_upsert_accumulates_queries(store):
    store.log_run("2026-02-01", FEED, new_bills=3, hearings=1, sent=True, queries=10)
    store.log_run("2026-02-01", FEED, new_bills=3, hearings=1, sent=True, queries=2)
    store.log_run("2026-02-02", FEED, skipped=True, queries=5)
    rows = store.sent_log(FEED)
    assert [
        (r["run_date"], r["new_bills"], r["sent"], r["skipped"], r["queries"]) for r in rows
    ] == [("2026-02-01", 3, 1, 0, 12), ("2026-02-02", 0, 0, 1, 5)]
    assert len(store.sent_log()) == 2


def test_rollback_discards_uncommitted(tmp_path):
    path = tmp_path / "bw.db"
    with Store(path) as s:
        s.upsert_bill(FEED, make_bill(1), tracked=True, reasons=[], when=NOW)
        s.commit()
        s.upsert_bill(FEED, make_bill(2), tracked=True, reasons=[], when=NOW)
        s.rollback()
        assert s.count_bills(FEED) == 1
    with Store(path) as s:
        assert s.count_bills(FEED) == 1


def test_store_holds_no_pii_columns(store):
    """Design §8.1: the DB schema contains no recipient/credential fields."""
    cols = set()
    for table in ("bills", "hearings", "seen", "events", "sent_log", "meta"):
        for row in store._conn.execute(f"PRAGMA table_info({table})"):
            cols.add(row[1].lower())
    for forbidden in ("email", "recipient", "address", "password", "api_key", "token"):
        assert not any(forbidden in c for c in cols), forbidden


def test_history_roundtrip_with_chamber(store):
    b = make_bill(
        history=[Action("2026-01-01", "First Reading", "H"), Action("2026-01-05", "Hearing", None)]
    )
    store.upsert_bill(FEED, b, tracked=True, reasons=[], when=NOW)
    assert store.get_bill(FEED, 1).bill.history == b.history
