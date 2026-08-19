from __future__ import annotations

import json

import pytest
import requests

from billwatch.legiscan import (
    FixtureClient,
    LegiScanClient,
    LegiScanError,
    QuotaExceeded,
    TransportError,
    parse_bill,
    parse_master_list,
    parse_search_results,
    parse_session,
    pick_current_session,
    search_slug,
)
from billwatch.models import SessionInfo

# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


def test_parse_master_list(day1_dir):
    payload = json.loads((day1_dir / "masterlist_MD.json").read_text())
    ml = parse_master_list(payload)
    assert ml.session is not None and ml.session.session_id == 2200
    assert ml.session.name == "2026 Regular Session"
    assert len(ml.entries) == 8
    assert ml.entries[1001].number == "HB101"
    assert len(ml.entries[1001].change_hash) == 32


def test_parse_master_list_malformed():
    with pytest.raises(LegiScanError, match="masterlist"):
        parse_master_list({"status": "OK"})


def test_parse_bill_full(day1_dir):
    b = parse_bill(json.loads((day1_dir / "bill_1001.json").read_text()))
    assert b.bill_id == 1001
    assert b.state == "MD"
    assert b.number == "HB101"
    assert b.title.startswith("Public Health - Opioid")
    assert b.status == 1 and b.status_label == "Introduced"
    assert b.committee == "Health and Government Operations"
    assert b.referrals == ["Health and Government Operations"]
    assert b.session_id == 2200 and b.session_name == "2026 Regular Session"
    assert b.url.startswith("https://legiscan.com/MD/bill/HB101")
    assert "mgaleg.maryland.gov" in b.state_url
    assert b.sponsors and b.sponsors[0].startswith("Delegate")
    assert b.last_action == "First Reading Health and Government Operations"
    assert b.last_action_date == "2026-01-14"
    assert len(b.hearings) == 1
    h = b.hearings[0]
    assert h.date == "2026-02-10" and h.time == "13:00"
    assert h.committee == "House Health and Government Operations"  # " Hearing" suffix stripped
    assert h.location.startswith("Room 241")
    assert h.kind == "Hearing"


def test_parse_bill_committee_empty_list_and_missing_fields():
    payload = {
        "status": "OK",
        "bill": {
            "bill_id": 5,
            "state": "md",
            "bill_number": "SB5",
            "title": " T ",
            "description": None,
            "committee": [],  # LegiScan quirk: [] when nothing pending
            "status": "",
            "history": [],
            "calendar": [{"date": "2026-03-01", "description": ""}],
        },
    }
    b = parse_bill(payload)
    assert b.committee is None
    assert b.status is None and b.status_label == "Unknown"
    assert b.title == "T" and b.synopsis == ""
    assert b.state == "MD"
    assert b.last_action is None
    assert b.hearings[0].committee == "Hearing"  # falls back to kind when no description/committee


def test_parse_bill_history_sorted_and_malformed():
    payload = {
        "status": "OK",
        "bill": {
            "bill_id": 1,
            "history": [
                {"date": "2026-02-01", "action": "Later"},
                {"date": "2026-01-01", "action": "Earlier"},
                {"date": "2026-01-15"},
            ],
        },
    }
    b = parse_bill(payload)
    assert [a.action for a in b.history] == ["Earlier", "Later"]
    with pytest.raises(LegiScanError, match="bill"):
        parse_bill({"status": "OK"})


def test_parse_search_results_shapes():
    raw = {
        "status": "OK",
        "searchresult": {
            "summary": {"count": 2},
            "results": [
                {"relevance": 100, "bill_id": 1, "change_hash": "a"},
                {"relevance": "55%", "bill_id": 2, "change_hash": "b"},
            ],
        },
    }
    hits = parse_search_results(raw, "overdose")
    assert [(h.bill_id, h.relevance, h.query) for h in hits] == [
        (1, 100, "overdose"),
        (2, 55, "overdose"),
    ]
    paged = {
        "status": "OK",
        "searchresult": {"summary": {}, "0": {"relevance": 90, "bill_id": 9, "change_hash": "z"}},
    }
    assert parse_search_results(paged, "q")[0].bill_id == 9
    with pytest.raises(LegiScanError):
        parse_search_results({"status": "OK"}, "q")


def test_pick_current_session_prefers_regular_non_prior():
    s_old = SessionInfo(1, "2025 Regular", 2025, 2025, prior=True)
    s_reg = SessionInfo(2, "2026 Regular", 2026, 2026)
    s_spc = SessionInfo(3, "2026 Special", 2026, 2026, special=True)
    assert pick_current_session([s_spc, s_reg, s_old]) is s_reg
    assert pick_current_session([s_spc, s_old]) is s_spc  # only non-prior left
    assert pick_current_session([s_old]) is s_old
    assert pick_current_session([]) is None


def test_parse_session_tolerates_strings():
    s = parse_session(
        {"session_id": "7", "session_name": "X", "year_start": "2026", "special": "1"}
    )
    assert s.session_id == 7 and s.special is True and s.year_start == 2026


def test_search_slug():
    assert search_slug("Harm Reduction!") == "harm-reduction"


# --------------------------------------------------------------------------- #
# HTTP client (fake session)
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="{"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(responses, **kw):
    sess = FakeSession(responses)
    sleeps = []
    c = LegiScanClient("KEY", session=sess, backoff_seconds=0.5, sleep=sleeps.append, **kw)
    return c, sess, sleeps


def test_client_requires_key():
    with pytest.raises(LegiScanError):
        LegiScanClient("")


def test_client_get_bill_success_and_params(day1_dir):
    payload = json.loads((day1_dir / "bill_1001.json").read_text())
    c, sess, _ = make_client([FakeResponse(200, payload)])
    b = c.get_bill(1001)
    assert b.number == "HB101"
    url, params = sess.calls[0]
    assert params == {"key": "KEY", "op": "getBill", "id": 1001}
    assert c.query_count == 1


def test_client_master_list_by_state_and_session(day1_dir):
    payload = json.loads((day1_dir / "masterlist_MD.json").read_text())
    c, sess, _ = make_client([FakeResponse(200, payload), FakeResponse(200, payload)])
    c.get_master_list("MD")
    c.get_master_list("MD", session_id=2200)
    assert sess.calls[0][1] == {"key": "KEY", "op": "getMasterListRaw", "state": "MD"}
    assert sess.calls[1][1] == {"key": "KEY", "op": "getMasterListRaw", "id": 2200}


def test_client_search_params():
    payload = {"status": "OK", "searchresult": {"summary": {}, "results": []}}
    c, sess, _ = make_client([FakeResponse(200, payload)])
    assert c.search("MD", "opioid") == []
    assert sess.calls[0][1] == {
        "key": "KEY",
        "op": "getSearchRaw",
        "state": "MD",
        "query": "opioid",
        "year": 2,
    }


def test_client_session_list():
    payload = {
        "status": "OK",
        "sessions": [{"session_id": 1, "session_name": "S", "year_start": 2026, "year_end": 2026}],
    }
    c, _, _ = make_client([FakeResponse(200, payload)])
    assert c.get_session_list("MD")[0].session_id == 1


def test_client_retries_then_succeeds_with_backoff():
    ok = {"status": "OK", "bill": {"bill_id": 1}}
    c, sess, sleeps = make_client(
        [requests.ConnectionError("boom"), FakeResponse(503), FakeResponse(200, ok)]
    )
    assert c.get_bill(1).bill_id == 1
    assert len(sess.calls) == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff
    assert c.query_count == 1  # retries do not count against the budget


def test_client_gives_up_after_three_attempts():
    c, sess, sleeps = make_client([FakeResponse(500), FakeResponse(502), FakeResponse(429)])
    with pytest.raises(TransportError, match="after 3 attempts"):
        c.get_bill(1)
    assert len(sess.calls) == 3 and len(sleeps) == 2


def test_client_non_json_is_retryable_but_4xx_is_not():
    ok = {"status": "OK", "bill": {"bill_id": 1}}
    c, _, _ = make_client([FakeResponse(200, None), FakeResponse(200, ok)])
    assert c.get_bill(1).bill_id == 1
    c, sess, _ = make_client([FakeResponse(404)])
    with pytest.raises(LegiScanError, match="HTTP 404"):
        c.get_bill(1)
    assert len(sess.calls) == 1


def test_client_api_error_payload_not_retried():
    err = {"status": "ERROR", "alert": {"message": "Invalid API key"}}
    c, sess, _ = make_client([err and FakeResponse(200, err)])
    with pytest.raises(LegiScanError, match="Invalid API key"):
        c.get_bill(1)
    assert len(sess.calls) == 1


def test_client_quota_budget():
    ok = {"status": "OK", "bill": {"bill_id": 1}}
    c, _, _ = make_client([FakeResponse(200, ok)] * 3, max_queries=2)
    assert c.remaining() == 2
    c.get_bill(1)
    c.get_bill(1)
    assert c.remaining() == 0
    with pytest.raises(QuotaExceeded):
        c.get_bill(1)
    assert c.query_count == 2


def test_client_error_messages_never_include_key():
    c, _, _ = make_client([FakeResponse(500)] * 3)
    with pytest.raises(TransportError) as ei:
        c.get_bill(1)
    assert "KEY" not in str(ei.value)


# --------------------------------------------------------------------------- #
# Fixture client
# --------------------------------------------------------------------------- #


def test_fixture_client_roundtrip(day1_dir):
    fc = FixtureClient(day1_dir, max_queries=10)
    assert fc.get_session_list("MD")[1].session_id == 2200
    ml = fc.get_master_list("MD")
    assert 1001 in ml.entries
    assert fc.get_bill(1001).number == "HB101"
    hits = fc.search("MD", "harm reduction")
    assert hits == []
    assert fc.search("MD", "nothing-recorded") == []  # optional file → no hits
    assert fc.query_count == 5 and fc.remaining() == 5
    with pytest.raises(LegiScanError, match="fixture not found"):
        fc.get_bill(999999)


def test_fixture_client_missing_dir_and_quota(tmp_path):
    with pytest.raises(LegiScanError):
        FixtureClient(tmp_path / "nope")
    fc = FixtureClient(tmp_path, max_queries=0)
    with pytest.raises(QuotaExceeded):
        fc.get_master_list("MD")
