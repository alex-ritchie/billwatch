from __future__ import annotations

from datetime import date

from billwatch.__main__ import EXIT_OK, main
from billwatch.config import parse_config
from billwatch.legiscan import FixtureClient
from billwatch.models import SearchHit
from billwatch.pipeline import run_pipeline
from billwatch.reevaluate import format_report, reevaluate_feed
from billwatch.store import Store
from tests.conftest import make_bill, make_config

FEED = "md-substance-use"


def _seed(store, day1_dir):
    """Day-1 backfill: 4 tracked (HB101, HB210, HB400-by-search, SB101 cross-file), 1 watch
    (HB300), 8 cached."""
    run_pipeline(
        config=make_config(),
        client=FixtureClient(day1_dir),
        store=store,
        today=date(2026, 2, 1),
        mailer=None,
        recipients_for=lambda f: [],
        announce=False,
    )


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


def test_pipeline_caches_every_fetched_bill(store, day1_dir):
    _seed(store, day1_dir)
    assert store.cache_count("MD") == 8  # matched or not
    assert store.cached_sessions("MD") == [2200]
    lite = {b.bill_id: b for b in store.cached_bills("MD", 2200)}
    assert lite[1005].title.startswith("Vehicle Laws")  # a non-match is cached too
    assert lite[1001].committee == "Health and Government Operations"
    assert lite[1001].referrals == ["Health and Government Operations"]
    assert lite[1001].hearings == [] and lite[1001].history == []  # lightweight on purpose
    assert store.schema_version() >= 2


def test_cache_bill_roundtrip_and_update(store):
    b = make_bill(7, "HB 7", title="T1")
    store.cache_bill(b, "t1")
    store.cache_bill(make_bill(7, "HB 7", title="T2", change_hash="h2"), "t2")
    got = store.cached_bills("MD")
    assert len(got) == 1 and got[0].title == "T2" and got[0].change_hash == "h2"
    assert store.cached_bills("VA") == [] and store.cache_count() == 1


# --------------------------------------------------------------------------- #
# reevaluate
# --------------------------------------------------------------------------- #


def test_reevaluate_no_change_is_noop(store, day1_dir):
    _seed(store, day1_dir)
    res = reevaluate_feed(
        store, make_config(), make_config().feed(FEED), client=FixtureClient(day1_dir)
    )
    assert res.changes == [] and res.unchanged == 8
    assert res.cached == 8 and res.session_id == 2200
    assert res.queries == 3  # the three searches only; nothing fetched, texts already current
    assert store.count_bills(FEED) == 4


def test_reevaluate_new_keyword_adds_and_fetches_detail(store, day1_dir):
    _seed(store, day1_dir)
    cfg = make_config(
        keywords=["opioid", "overdose", "naloxone", "substance use", "speed monitoring"]
    )  # new keyword → SB120 (1005)
    client = FixtureClient(day1_dir)
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=client)
    assert [c.number for c in res.added] == ["SB120"]
    assert res.added[0].reasons == ["keyword: speed monitoring"]
    tb = store.get_bill(FEED, 1005)
    assert tb.tracked and tb.bill.history  # full detail fetched, not the lite cache row
    assert res.queries == 3 + 1 + 1  # searches + detail + its text
    assert store.get_text("MD", 1005) is not None
    # quiet by default: event recorded as already sent
    assert store.unsent_events(FEED) == []
    assert store.count_bills(FEED) == 5


def test_reevaluate_announce_and_no_fetch(store, day1_dir):
    _seed(store, day1_dir)
    cfg = make_config(
        keywords=["opioid", "overdose", "naloxone", "substance use", "speed monitoring"]
    )
    res = reevaluate_feed(
        store,
        cfg,
        cfg.feed(FEED),
        client=FixtureClient(day1_dir),
        fetch_details=False,
        announce=True,
    )
    assert [c.number for c in res.added] == ["SB120"]
    assert res.queries == 3
    assert store.get_bill(FEED, 1005).bill.history == []  # cached fields only
    assert [e.kind for e in store.unsent_events(FEED)] == ["new"]


def test_reevaluate_removed_search_demotes_or_removes(store, day1_dir):
    _seed(store, day1_dir)
    # HB400 was tracked only via search "overdose"; drop the search → no rule matches → removed
    cfg = make_config(searches=["opioid"])
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir))
    assert [c.number for c in res.removed] == ["HB400"]
    assert store.get_bill(FEED, 1006) is None
    assert store.hearings_for(FEED, 1006) == []
    # HB101 keeps keywords but loses "search: overdose" → reasons updated
    upd = {c.number: c for c in res.updated}
    assert "HB101" in upd and "search: overdose" not in upd["HB101"].reasons
    assert "search: overdose" in upd["HB101"].before


def test_reevaluate_demotes_to_watch_when_committee_watched(store, day1_dir):
    _seed(store, day1_dir)
    # Remove the keywords that matched HB210 (HGO committee) → watch-only, hearings dropped
    cfg = make_config(keywords=["opioid", "overdose", "naloxone"], searches=["overdose"])
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir))
    dem = {c.number: c for c in res.demoted}
    assert "HB210" in dem
    tb = store.get_bill(FEED, 1003)
    assert not tb.tracked and tb.reasons == ["committee: Health and Government Operations"]
    assert store.hearings_for(FEED, 1003) == []


def test_reevaluate_promotes_watch_only_and_removes_unwatched(store, day1_dir):
    _seed(store, day1_dir)
    # "licensing" keyword promotes HB300; dropping HGO from watch_committees removes nothing else
    cfg = make_config(
        keywords=["opioid", "overdose", "naloxone", "substance use", "licensing"],
        watch_committees=["Finance"],
    )
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir))
    assert [c.number for c in res.promoted] == ["HB300"]
    assert store.get_bill(FEED, 1004).tracked
    # now drop the keyword again with HGO unwatched → HB300 has no rule → removed
    cfg2 = make_config(
        keywords=["opioid", "overdose", "naloxone", "substance use"], watch_committees=["Finance"]
    )
    res2 = reevaluate_feed(store, cfg2, cfg2.feed(FEED), client=FixtureClient(day1_dir))
    assert [c.number for c in res2.removed] == ["HB300"]
    assert store.get_bill(FEED, 1004) is None


def test_reevaluate_no_prune_only_adds(store, day1_dir):
    _seed(store, day1_dir)
    cfg = make_config(keywords=["speed monitoring"], searches=[], watch_committees=[])
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir), prune=False)
    assert [c.number for c in res.added] == ["SB120"]
    assert res.removed == [] and res.demoted == []
    assert store.count_bills(FEED) == 5  # nothing taken away


def test_reevaluate_dry_run_changes_nothing(tmp_path, day1_dir):
    db = tmp_path / "bw.db"
    with Store(db) as store:
        _seed(store, day1_dir)
        cfg = make_config(keywords=["speed monitoring"], searches=[], watch_committees=[])
        res = reevaluate_feed(
            store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir), dry_run=True
        )
        # 4 tracked bills (incl. SB101, whose cross-file HB101 no longer matches) + the
        # watch-only HB300 (no watched committees left) → 5 removals
        assert res.dry_run and len(res.added) == 1 and len(res.removed) == 5
        report = format_report(res)
        assert "DRY RUN" in report and "SB120" in report and "REMOVED (5)" in report
    with Store(db) as store:
        assert store.count_bills(FEED) == 4 and store.get_bill(FEED, 1005) is None


def test_reevaluate_without_client_skips_searches(store, day1_dir):
    _seed(store, day1_dir)
    cfg = make_config()
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=None)
    # no searches possible → HB400 (search-only) has no rule → removed; keyword bills lose search reasons
    assert [c.number for c in res.removed] == ["HB400"]
    assert res.queries == 0


def test_reevaluate_search_term_change_uses_live_search(store, day1_dir):
    _seed(store, day1_dir)

    class NewSearch(FixtureClient):
        def search(self, state, query, session_id=None):
            if query == '"speed"':
                return [SearchHit(1005, "x", 95, query)]
            return super().search(state, query, session_id)

    cfg = make_config(searches=["overdose", "opioid", '"speed"'])
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=NewSearch(day1_dir))
    assert [c.number for c in res.added] == ["SB120"]
    assert res.added[0].reasons == ['search: "speed"']


def test_reevaluate_empty_cache_warns(store, caplog):
    cfg = make_config()
    res = reevaluate_feed(store, cfg, cfg.feed(FEED), client=None)
    assert res.cached == 0 and res.changes == []
    assert "cache is empty" in caplog.text


def test_reevaluate_multiple_feeds_and_pinned_session(store, day1_dir):
    _seed(store, day1_dir)
    cfg = parse_config(
        {
            "feeds": {
                FEED: {"state": "MD", "keywords": ["opioid"], "session_id": 2200},
                "md-vehicles": {"state": "MD", "keywords": ["speed monitoring"]},
            }
        }
    )
    from billwatch.reevaluate import reevaluate

    results = reevaluate(store, cfg, client=FixtureClient(day1_dir))
    by = {r.feed: r for r in results}
    assert by[FEED].session_id == 2200
    assert [c.number for c in by["md-vehicles"].added] == ["SB120"]
    assert store.count_bills("md-vehicles") == 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_reevaluate(env_console, tmp_path, config_path, day1_dir, capsys):
    db = tmp_path / "bw.db"
    base = ["--env-file", ""]
    assert (
        main(
            [
                *base,
                "backfill",
                "--config",
                str(config_path),
                "--db",
                str(db),
                "--fixtures",
                str(day1_dir),
                "--today",
                "2026-01-20",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()
    # a config that drops every search: HB400 goes away; dry-run first
    import re

    cfg = tmp_path / "feeds.toml"
    cfg.write_text(re.sub(r"^searches = .*$", "searches = []", config_path.read_text(), flags=re.M))
    assert "searches = []" in cfg.read_text()
    rc = main(
        [
            *base,
            "reevaluate",
            "--config",
            str(cfg),
            "--db",
            str(db),
            "--fixtures",
            str(day1_dir),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK and "DRY RUN" in out and "HB400" in out
    with Store(db) as s:
        assert s.get_bill(FEED, 1006) is not None
    rc = main(
        [*base, "reevaluate", "--config", str(cfg), "--db", str(db), "--fixtures", str(day1_dir)]
    )
    out = capsys.readouterr().out
    assert rc == EXIT_OK and "REMOVED (1)" in out
    with Store(db) as s:
        assert s.get_bill(FEED, 1006) is None
