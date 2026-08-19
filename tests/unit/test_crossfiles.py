from __future__ import annotations

import json
from datetime import date

from billwatch.digest import build_digest, render_html, render_text
from billwatch.filters import FeedFilter
from billwatch.legiscan import FixtureClient, parse_bill
from billwatch.models import RelatedBill
from billwatch.pipeline import adopt_crossfiles, run_pipeline
from billwatch.reevaluate import reevaluate_feed
from tests.conftest import make_bill, make_config

FEED = "md-substance-use"
NOW = "2026-02-01T12:00:00+00:00"


def test_parse_sasts_and_crossfile_property(day1_dir):
    b = parse_bill(json.loads((day1_dir / "bill_1001.json").read_text()))
    assert b.sasts == [RelatedBill(bill_id=1009, number="SB101", relation="Crossfiled")]
    assert b.crossfiles == b.sasts
    assert RelatedBill(1, "X", "Same As").is_crossfile
    assert RelatedBill(1, "X", "Similar To").is_crossfile is False
    assert RelatedBill(1, "X", "Replaced by").is_crossfile is False
    assert b.latest_text is not None and b.latest_text.doc_id == 6001


def test_filter_crossfile_reason_counts_as_match():
    flt = FeedFilter(make_config().feed(FEED))
    r = flt.evaluate(
        make_bill(title="Pharmacies - Dispensing", synopsis="x", committee="Finance"),
        crossfile_of=["HB101"],
    )
    assert r.matched and r.reasons == ["crossfile: HB101"]
    assert not flt.evaluate(
        make_bill(title="Pharmacies - Dispensing", synopsis="x", committee="Finance")
    ).matched


def test_store_roundtrips_sasts_and_texts(store, day1_dir):
    b = parse_bill(json.loads((day1_dir / "bill_1001.json").read_text()))
    store.upsert_bill(FEED, b, tracked=True, reasons=[], when=NOW)
    store.cache_bill(b, NOW)
    got = store.get_bill(FEED, 1001).bill
    assert got.sasts == b.sasts and got.texts == b.texts
    assert store.cached_bill("MD", 1001).sasts == b.sasts


def _seed(store, day1_dir, announce=False):
    return run_pipeline(
        config=make_config(),
        client=FixtureClient(day1_dir),
        store=store,
        today=date(2026, 2, 1),
        mailer=None,
        recipients_for=lambda f: [],
        announce=announce,
    )


def test_pipeline_adopts_crossfile_and_drops_watch_flag(store, day1_dir):
    res = _seed(store, day1_dir, announce=True)
    r = res.feeds[FEED]
    # SB101 alone would be Finance watch-only; as HB101's cross-file it is tracked instead,
    # and its superseded watch event is removed so it does not appear in both sections.
    assert r.new_bills == 4 and r.watch == 1  # watch = HB300 only
    sb = store.get_bill(FEED, 1009)
    assert sb.tracked and sb.reasons == ["crossfile: HB101"]
    kinds = [(e.kind, e.bill_id) for e in store.unsent_events(FEED)]
    assert ("watch", 1009) not in kinds and ("new", 1009) in kinds
    # adoption reused the bill fetched this run: no extra getBill for SB101
    assert res.queries == 1 + 1 + 3 + 8 + 4


def test_digest_groups_crossfiled_pair(store, day1_dir, config, feed):
    _seed(store, day1_dir, announce=True)
    d = build_digest(store, config, feed, date(2026, 2, 1))
    numbers = [i.numbers for i in d.new_bills]
    assert "HB101 / SB101" in numbers and len(d.new_bills) == 3
    pair = next(i for i in d.new_bills if i.partners)
    assert [b.number for b in pair.bills] == ["HB101", "SB101"]
    assert "keyword: opioid" in pair.reasons and not any(
        r.startswith("crossfile") for r in pair.reasons
    )
    # all five events (4 new + 1 watch) are consumed even though the pair renders once
    assert len(d.event_ids) == 5
    html, text = render_html(d), render_text(d)
    assert "HB101 / SB101" in html and "(cross-filed)" in html
    assert "SB101</strong> · Status: Introduced" in html and "Committee: Finance" in html
    assert "* HB101 / SB101 (cross-filed) —" in text and "SB101: Status: Introduced" in text
    assert d.summary.startswith("3 new")


def test_digest_annotates_movement_and_hearings_with_partner(
    store, day1_dir, day2_dir, config, feed
):
    _seed(store, day1_dir)
    run_pipeline(
        config=make_config(),
        client=FixtureClient(day2_dir),
        store=store,
        today=date(2026, 2, 14),
        mailer=None,
        recipients_for=lambda f: [],
    )
    d = build_digest(store, config, feed, date(2026, 2, 14))
    mv = next(m for m in d.movement if m.bill.number == "HB101")
    assert mv.partners == ["SB101"]
    hr = next(h for h in d.hearings if h.bill.number == "HB101")
    assert hr.partners == ["SB101"]
    text = render_text(d)
    assert (
        "HB101 — Public Health - Opioid Overdose Prevention - Naloxone Access (cross-filed with SB101)"
        in text
    )


def test_adopt_when_partner_not_yet_fetched_then_later(store, day1_dir):
    """If the partner is missing from cache and budget is gone, adoption waits; it happens
    on a later call once the partner can be fetched."""
    b = parse_bill(json.loads((day1_dir / "bill_1001.json").read_text()))
    store.upsert_bill(FEED, b, tracked=True, reasons=["keyword: opioid"], when=NOW)
    feed = make_config().feed(FEED)
    assert adopt_crossfiles(store, feed, FixtureClient(day1_dir, max_queries=0), when=NOW) == []
    assert store.get_bill(FEED, 1009) is None
    adopted = adopt_crossfiles(store, feed, FixtureClient(day1_dir), when=NOW, announce=False)
    assert [x.number for x in adopted] == ["SB101"]
    assert store.get_bill(FEED, 1009).reasons == ["crossfile: HB101"]
    assert store.unsent_events(FEED) == []  # announce=False → recorded as sent
    # idempotent
    assert adopt_crossfiles(store, feed, FixtureClient(day1_dir), when=NOW) == []


def test_adopt_uses_cache_when_no_client(store, day1_dir):
    b = parse_bill(json.loads((day1_dir / "bill_1001.json").read_text()))
    sb = parse_bill(json.loads((day1_dir / "bill_1009.json").read_text()))
    store.upsert_bill(FEED, b, tracked=True, reasons=["keyword: opioid"], when=NOW)
    store.cache_bill(sb, NOW)
    adopted = adopt_crossfiles(store, make_config().feed(FEED), None, when=NOW)
    assert [x.number for x in adopted] == ["SB101"]
    assert store.get_bill(FEED, 1009).bill.history == []  # cache is lightweight


def test_reevaluate_prunes_crossfile_when_source_stops_matching(store, day1_dir):
    _seed(store, day1_dir)
    assert store.get_bill(FEED, 1009).tracked
    # remove every rule that matched HB101 → both HB101 and its cross-file SB101 go
    cfg = make_config(keywords=["substance use"], searches=[], watch_committees=["Judiciary"])
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir))
    removed = {c.number for c in res.removed}
    assert {"HB101", "SB101"} <= removed
    assert store.get_bill(FEED, 1009) is None


def test_reevaluate_adopts_crossfile_of_newly_matched(store, day1_dir):
    # start with a config that matches nothing; then add a keyword that matches HB101 only
    cfg0 = make_config(keywords=["zzz"], searches=[], watch_committees=[])
    run_pipeline(
        config=cfg0,
        client=FixtureClient(day1_dir),
        store=store,
        today=date(2026, 2, 1),
        mailer=None,
        recipients_for=lambda f: [],
        announce=False,
    )
    assert store.count_bills(FEED) == 0
    cfg = make_config(keywords=["naloxone"], searches=[], watch_committees=[])
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir))
    added = {c.number: c for c in res.added}
    assert set(added) == {"HB101", "SB101"}
    assert added["SB101"].reasons == ["crossfile: HB101"]
    assert store.get_bill(FEED, 1009).tracked


def test_reevaluate_refetch_refreshes_tracked_bills(store, day1_dir, day2_dir):
    _seed(store, day1_dir)
    before = store.get_bill(FEED, 1001).bill
    assert before.status == 1
    cfg = make_config()
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day2_dir), refetch=True)
    assert res.refetched == 4  # HB101, HB210, HB400, SB101
    after = store.get_bill(FEED, 1001).bill
    assert after.status == 2 and len(after.history) > len(before.history)
    assert store.get_text("MD", 1001)["version"] == "Engrossed"
    assert res.texts_fetched == 1
