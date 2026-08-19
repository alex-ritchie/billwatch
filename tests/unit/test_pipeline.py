from __future__ import annotations

import logging
from datetime import date
from email.message import EmailMessage

from billwatch.filters import FeedFilter
from billwatch.legiscan import FixtureClient, LegiScanError, TransportError
from billwatch.mailer import MailError
from billwatch.models import Action, Hearing
from billwatch.pipeline import (
    FeedRunResult,
    compute_change,
    process_bill,
    run_pipeline,
    search_scope,
    state_scope,
)
from tests.conftest import make_bill, make_config

FEED = "md-substance-use"
NOW = "2026-02-01T12:00:00+00:00"


class RecordingMailer:
    name = "recording"

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[EmailMessage, list[str]]] = []
        self.fail = fail

    def send(self, message, recipients):
        if self.fail:
            raise MailError("SMTP delivery failed: boom")
        self.sent.append((message, list(recipients)))


def run(
    client,
    store,
    *,
    config=None,
    today=date(2026, 2, 1),
    mailer=None,
    announce=True,
    recipients=("friend@example.com",),
):
    return run_pipeline(
        config=config or make_config(),
        client=client,
        store=store,
        today=today,
        mailer=mailer,
        recipients_for=lambda f: list(recipients),
        sender="bot@example.com",
        announce=announce,
    )


# --------------------------------------------------------------------------- #
# compute_change / process_bill
# --------------------------------------------------------------------------- #


def test_compute_change_detects_added_actions_status_and_committee():
    old = make_bill(status=1, history=[Action("2026-01-14", "First Reading", "H")])
    new = make_bill(
        status=2,
        committee="Finance",
        history=[
            Action("2026-01-14", "First Reading", "H"),
            Action("2026-02-01", "Third Reading Passed", "H"),
        ],
    )
    ch = compute_change(old, new)
    assert ch.status_changed and ch.committee_changed
    assert [a.action for a in ch.actions] == ["Third Reading Passed"]
    assert not ch.is_empty()
    assert compute_change(old, make_bill()).is_empty()


def test_compute_change_legacy_row_without_history():
    old = make_bill(history=[])
    old.last_action, old.last_action_date = "First Reading", "2026-01-14"
    new = make_bill(
        history=[
            Action("2026-01-14", "First Reading"),
            Action("2026-01-14", "Referred to HGO"),
            Action("2026-01-20", "Hearing"),
        ]
    )
    assert [a.action for a in compute_change(old, new).actions] == ["Referred to HGO", "Hearing"]


def _pb(store, bill, search_hits=None, config=None):
    cfg = config or make_config()
    feed = cfg.feed(FEED)
    res = FeedRunResult(feed=FEED)
    process_bill(
        store, feed, FeedFilter(feed), bill, search_hits=search_hits or [], when=NOW, result=res
    )
    return res


def test_process_bill_new_match_creates_event_and_hearings(store):
    b = make_bill(hearings=[Hearing(1, "2026-02-10", "House HGO")])
    res = _pb(store, b)
    assert res.new_bills == 1
    tb = store.get_bill(FEED, 1)
    assert tb.tracked and tb.reasons == [
        "keyword: opioid",
        "keyword: overdose",
        "keyword: naloxone",
    ]
    assert store.hearings_for(FEED, 1) == b.hearings
    ev = store.unsent_events(FEED)
    assert len(ev) == 1 and ev[0].kind == "new"


def test_process_bill_ignores_non_match(store):
    res = _pb(store, make_bill(title="Vehicle Laws", synopsis="speed", committee="Judiciary"))
    assert res.new_bills == res.watch == res.changed == 0
    assert store.get_bill(FEED, 1) is None and store.unsent_events(FEED) == []


def test_process_bill_watch_only_then_promoted(store):
    b = make_bill(title="Licensing", synopsis="renewals")  # HGO committee → watch
    assert _pb(store, b).watch == 1
    tb = store.get_bill(FEED, 1)
    assert not tb.tracked and store.unsent_events(FEED)[0].kind == "watch"
    # a text-only change: no new event
    b2 = make_bill(title="Licensing", synopsis="renewals", change_hash="h2")
    r2 = _pb(store, b2)
    assert r2.watch == r2.new_bills == 0 and len(store.unsent_events(FEED)) == 1
    # synopsis edited to mention opioids: promoted to tracked, 'new' event
    b3 = make_bill(title="Licensing", synopsis="opioid prescribing renewals", change_hash="h3")
    assert _pb(store, b3).new_bills == 1
    assert store.get_bill(FEED, 1).tracked
    assert [e.kind for e in store.unsent_events(FEED)] == ["watch", "new"]


def test_process_bill_tracked_change_emits_status_event(store):
    _pb(store, make_bill())
    changed = make_bill(
        status=2,
        change_hash="h2",
        history=[
            Action("2026-01-14", "First Reading", "H"),
            Action("2026-02-01", "Third Reading Passed", "H"),
        ],
        hearings=[Hearing(1, "2026-02-20", "Senate Finance")],
    )
    res = _pb(store, changed)
    assert res.changed == 1 and res.new_bills == 0
    evs = store.unsent_events(FEED)
    assert [e.kind for e in evs] == ["new", "status"]
    assert evs[1].detail["new_status"] == 2
    assert evs[1].detail["actions"][0]["action"] == "Third Reading Passed"
    assert store.hearings_for(FEED, 1)[0].date == "2026-02-20"
    # unchanged content but new hash (e.g. text upload): no event
    res = _pb(store, make_bill(status=2, change_hash="h3", history=changed.history))
    assert res.changed == 0 and len(store.unsent_events(FEED)) == 2


def test_process_bill_tracked_stays_tracked_and_accumulates_reasons(store):
    _pb(store, make_bill())
    _pb(
        store,
        make_bill(title="Renamed", synopsis="nothing relevant", change_hash="h2"),
        search_hits=["overdose"],
    )
    tb = store.get_bill(FEED, 1)
    assert tb.tracked
    assert "search: overdose" in tb.reasons and "keyword: opioid" in tb.reasons


# --------------------------------------------------------------------------- #
# run_pipeline against recorded fixtures
# --------------------------------------------------------------------------- #


def test_day1_run_sends_digest_and_persists(store, day1_dir):
    mailer = RecordingMailer()
    client = FixtureClient(day1_dir)
    result = run(client, store, mailer=mailer)
    assert result.ok
    r = result.feeds[FEED]
    assert (r.new_bills, r.changed, r.watch, r.hearings_announced) == (3, 0, 1, 1)
    assert r.sent and not r.skipped
    # 1 sessions + 1 masterlist + 3 searches + 7 bill details
    assert result.queries == 12 and result.candidates == 7 and result.fetched == 7
    assert r.queries == 12
    msg, rcpts = mailer.sent[0]
    assert rcpts == ["friend@example.com"]
    assert "3 new, 1 hearing, 1 to review" in msg["Subject"]
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "HB101" in html and "HB210" in html and "HB400" in html and "HB300" in html
    assert "SB55" not in html and "SB120" not in html and "SB130" not in html
    # persisted state
    assert store.count_bills(FEED) == 3
    assert store.unsent_events(FEED) == []
    assert store.seen_hashes(state_scope("MD")).keys() == {1001, 1002, 1003, 1004, 1005, 1006, 1007}
    assert set(store.seen_hashes(search_scope(FEED))) == {1001, 1003, 1006}  # relevance ≥ 50 only
    assert store.upcoming_hearings(FEED, "2026-02-01", "2026-02-15") == []  # announced
    log = store.sent_log(FEED)
    assert log[0]["sent"] == 1 and log[0]["new_bills"] == 3 and log[0]["queries"] == 12


def test_second_run_same_data_is_quiet(store, day1_dir):
    run(FixtureClient(day1_dir), store, mailer=RecordingMailer())
    mailer = RecordingMailer()
    result = run(FixtureClient(day1_dir), store, mailer=mailer, today=date(2026, 2, 2))
    r = result.feeds[FEED]
    assert r.skipped and not r.sent and mailer.sent == []
    assert result.candidates == 0 and result.fetched == 0
    assert result.queries == 5  # sessions + masterlist + 3 searches, no details
    assert store.sent_log(FEED)[-1]["skipped"] == 1


def test_send_empty_sends_minimal_note(store, day1_dir):
    run(FixtureClient(day1_dir), store, mailer=RecordingMailer())
    mailer = RecordingMailer()
    cfg = make_config(send_empty=True)
    run(FixtureClient(day1_dir), store, config=cfg, mailer=mailer, today=date(2026, 2, 2))
    assert len(mailer.sent) == 1
    assert "no changes" in mailer.sent[0][0]["Subject"]
    assert "Nothing new" in mailer.sent[0][0].get_body(preferencelist=("plain",)).get_content()


def test_day2_run_reports_movement_new_bill_and_hearings(store, day1_dir, day2_dir):
    run(FixtureClient(day1_dir), store, mailer=RecordingMailer())
    mailer = RecordingMailer()
    result = run(FixtureClient(day2_dir), store, mailer=mailer, today=date(2026, 2, 14))
    r = result.feeds[FEED]
    assert (r.new_bills, r.changed, r.watch, r.hearings_announced) == (1, 1, 0, 2)
    # HB101 hash changed, HB300 hash changed (watch-only, text edit), HB600 new → 3 details
    assert result.fetched == 3
    text = mailer.sent[0][0].get_body(preferencelist=("plain",)).get_content()
    assert "HB600" in text and "fentanyl" in text.lower()
    assert "Introduced -> Engrossed" in text
    assert "Health and Government Operations -> Finance" in text
    assert "Third Reading Passed (135-2)" in text
    assert "2026-02-24" in text and "2026-02-25" in text
    assert "COMMITTEE WATCH" not in text  # HB300 not re-flagged
    assert store.count_bills(FEED) == 4


def test_send_failure_keeps_events_unsent_and_merges_next_time(store, day1_dir, day2_dir, caplog):
    result = run(FixtureClient(day1_dir), store, mailer=RecordingMailer(fail=True))
    assert not result.ok and "send failed" in result.errors[0]
    r = result.feeds[FEED]
    assert r.error and not r.sent
    # fetch state IS committed, events pending, hearings unannounced
    assert store.count_bills(FEED) == 3
    assert len(store.unsent_events(FEED)) == 4
    assert store.sent_log(FEED)[0]["sent"] == 0 and store.sent_log(FEED)[0]["skipped"] == 0
    # next day works: everything from day 1 plus day 2 changes in one digest
    mailer = RecordingMailer()
    result2 = run(FixtureClient(day2_dir), store, mailer=mailer, today=date(2026, 2, 14))
    assert result2.ok
    text = mailer.sent[0][0].get_body(preferencelist=("plain",)).get_content()
    assert "NEW BILLS (4)" in text  # HB101, HB210, HB400 (day 1) + HB600
    assert "MOVEMENT" not in text  # HB101 is new-and-moved → folded into New
    assert "COMMITTEE WATCH (1)" in text
    assert store.unsent_events(FEED) == []
    # log hygiene: recipient addresses never logged
    assert "friend@example.com" not in caplog.text


class FlakyClient(FixtureClient):
    def __init__(self, d, fail_on: int):
        super().__init__(d)
        self.fail_on = fail_on

    def get_bill(self, bill_id):
        if bill_id == self.fail_on:
            raise TransportError("LegiScan getBill failed after 3 attempts")
        return super().get_bill(bill_id)


def test_fetch_failure_rolls_back_and_reports(store, day1_dir):
    mailer = RecordingMailer()
    result = run(FlakyClient(day1_dir, fail_on=1003), store, mailer=mailer)
    assert not result.ok and "fetch failed" in result.errors[0]
    assert mailer.sent == []
    assert store.count_bills(FEED) == 0  # partial work rolled back
    assert store.seen_hashes(state_scope("MD")) == {}
    # a healthy run afterwards catches everything up
    result = run(FixtureClient(day1_dir), store, mailer=mailer)
    assert result.ok and result.feeds[FEED].new_bills == 3


def test_query_budget_defers_candidates_to_next_run(store, day1_dir, caplog):
    mailer = RecordingMailer()
    # 5 fixed queries (sessions+masterlist+3 searches) + 3 details
    with caplog.at_level(logging.WARNING):
        result = run(FixtureClient(day1_dir, max_queries=8), store, mailer=mailer)
    assert result.ok
    assert result.fetched == 3 and result.deferred == 4
    assert "deferring 4 candidate" in caplog.text
    assert len(store.seen_hashes(state_scope("MD"))) == 3
    # next run (with budget) picks up the remaining four
    result = run(FixtureClient(day1_dir), store, mailer=mailer, today=date(2026, 2, 2))
    assert result.fetched == 4 and result.deferred == 0
    assert store.count_bills(FEED) == 3
    assert len(mailer.sent) == 2


def test_backfill_records_without_sending(store, day1_dir):
    mailer = RecordingMailer()
    result = run(FixtureClient(day1_dir), store, mailer=None, announce=False)
    r = result.feeds[FEED]
    assert r.skipped and not r.sent and mailer.sent == []
    assert store.count_bills(FEED) == 3
    assert store.unsent_events(FEED) == []
    # hearings are left unannounced so the first real digest still lists what's coming
    assert len(store.upcoming_hearings(FEED, "2026-02-01", "2026-02-15")) == 1
    result = run(FixtureClient(day1_dir), store, mailer=mailer, today=date(2026, 2, 2))
    assert result.feeds[FEED].sent
    assert "1 hearing" in mailer.sent[0][0]["Subject"]
    assert "NEW BILLS" not in mailer.sent[0][0].get_body(preferencelist=("plain",)).get_content()


def test_dry_run_mailer_none_marks_nothing(store, day1_dir):
    result = run(FixtureClient(day1_dir), store, mailer=None)
    r = result.feeds[FEED]
    assert r.digest is not None and not r.sent and not r.skipped
    assert len(store.unsent_events(FEED)) == 4  # nothing consumed


def test_pinned_session_and_search_hits_outside_masterlist(store, day1_dir):
    """A pinned session_id skips getSessionList; search hits not in the master list are ignored."""
    cfg = make_config(session_id=2200, searches=["overdose"])
    client = FixtureClient(day1_dir)
    result = run(client, store, config=cfg, mailer=RecordingMailer())
    assert result.ok
    assert result.queries == 1 + 1 + 7  # masterlist + search + details (no session list)


def test_multiple_feeds_same_state_share_fetches(store, day1_dir):
    data = make_config()
    from billwatch.config import parse_config

    cfg = parse_config(
        {
            "settings": {},
            "feeds": {
                "md-substance-use": {
                    "state": "MD",
                    "keywords": data.feed(FEED).keywords,
                    "searches": [],
                    "watch_committees": [],
                },
                "md-vehicles": {
                    "state": "MD",
                    "keywords": ["speed monitoring"],
                    "searches": [],
                    "recipients_env": "RECIPIENTS_VEHICLES",
                },
            },
        }
    )
    mailer = RecordingMailer()
    result = run(client := FixtureClient(day1_dir), store, config=cfg, mailer=mailer)
    assert result.ok
    assert client.query_count == 1 + 1 + 7  # details fetched once for both feeds
    assert result.feeds["md-substance-use"].new_bills == 2  # HB101, HB210 (no search net here)
    assert result.feeds["md-vehicles"].new_bills == 1  # SB120
    assert len(mailer.sent) == 2
    assert store.count_bills("md-vehicles") == 1


def test_selected_feed_only(store, day1_dir):
    from billwatch.config import parse_config

    cfg = parse_config(
        {
            "feeds": {
                "a": {"state": "MD", "keywords": ["opioid"]},
                "b": {"state": "MD", "keywords": ["speed monitoring"]},
            }
        }
    )
    result = run_pipeline(
        config=cfg,
        client=FixtureClient(day1_dir),
        store=store,
        today=date(2026, 2, 1),
        mailer=RecordingMailer(),
        recipients_for=lambda f: ["x@example.com"],
        feed_names=["b"],
    )
    assert list(result.feeds) == ["b"] and store.count_bills("a") == 0


def test_error_payload_from_client_is_reported_not_raised(store, day1_dir):
    class Broken(FixtureClient):
        def get_master_list(self, state, session_id=None):
            raise LegiScanError("LegiScan getMasterListRaw error: Invalid API key")

    result = run(Broken(day1_dir), store, mailer=RecordingMailer())
    assert not result.ok and "Invalid API key" in result.errors[0]
    assert result.feeds[FEED].error


def test_search_hit_promotes_watch_only_bill_without_hash_change(store, day1_dir):
    """HB300 is watch-only on day 1. If a later full-text search surfaces it (same master
    hash), it must be re-fetched and promoted to tracked."""
    from billwatch.models import SearchHit

    class SearchClient(FixtureClient):
        def search(self, state, query, session_id=None):
            hits = super().search(state, query, session_id)
            if query == "overdose":
                hits.append(
                    SearchHit(bill_id=1004, change_hash="search-h1", relevance=90, query=query)
                )
            return hits

    run(FixtureClient(day1_dir), store, mailer=RecordingMailer())
    assert not store.get_bill(FEED, 1004).tracked
    mailer = RecordingMailer()
    result = run(SearchClient(day1_dir), store, mailer=mailer, today=date(2026, 2, 2))
    assert result.fetched == 1 and result.feeds[FEED].new_bills == 1
    tb = store.get_bill(FEED, 1004)
    assert tb.tracked and tb.reasons == ["search: overdose"]
    assert "HB300" in mailer.sent[0][0].get_body(preferencelist=("plain",)).get_content()
    # same hit again tomorrow: nothing to fetch
    result = run(SearchClient(day1_dir), store, mailer=mailer, today=date(2026, 2, 3))
    assert result.fetched == 0
