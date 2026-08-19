from __future__ import annotations

import pytest

from billwatch.filters import FeedFilter, compile_keyword
from tests.conftest import make_bill, make_config


@pytest.mark.parametrize(
    "keyword, text, expected",
    [
        ("opioid", "An Act concerning opioids", True),
        ("opioid", "The opioid crisis", True),
        ("opioid", "OPIOID Response", True),
        ("overdose", "overdoses are rising", True),
        ("overdose", "underdosed", False),
        ("harm reduction", "harm-reduction services", True),
        ("harm reduction", "harm   reduction", True),
        ("harm reduction", "harmful reduction", False),
        ("syringe", "syringes", True),
        ("syringe", "syringeless", False),
        ("substance use", "Substance Use Disorder", True),
        ("substance use", "substance used", False),
        ("methadone", "methadone", True),
        ("controlled dangerous substance", "Controlled Dangerous Substances - Penalties", True),
        ("drug", "drugstore", False),
        ("drug", "prescription drugs", True),
    ],
)
def test_compile_keyword(keyword, text, expected):
    assert bool(compile_keyword(keyword).search(text)) is expected


def test_compile_keyword_rejects_empty():
    with pytest.raises(ValueError):
        compile_keyword("   ")


def test_keyword_match_reasons_and_flags():
    flt = FeedFilter(make_config().feed("md-substance-use"))
    r = flt.evaluate(make_bill(title="Opioid Overdose Prevention", synopsis="naloxone access"))
    assert r.matched and not r.watch_only and not r.excluded
    assert r.keyword_hits == ["opioid", "overdose", "naloxone"]
    assert r.reasons == ["keyword: opioid", "keyword: overdose", "keyword: naloxone"]
    # committee reason omitted when keywords already explain the match
    assert not any(x.startswith("committee") for x in r.reasons)


def test_watch_only_when_no_keyword_but_watched_committee():
    flt = FeedFilter(make_config().feed("md-substance-use"))
    r = flt.evaluate(
        make_bill(
            title="Health Occupations - Licensing",
            synopsis="renewals",
            committee="Health and Government Operations",
        )
    )
    assert not r.matched
    assert r.watch_only
    assert r.reasons == ["committee: Health and Government Operations"]


def test_watch_matches_past_referrals_and_partial_names():
    flt = FeedFilter(make_config().feed("md-substance-use"))
    b = make_bill(
        title="Vehicle Laws",
        synopsis="speed cameras",
        committee="Judiciary",
        referrals=["Judiciary", "Senate Finance Committee"],
    )
    r = flt.evaluate(b)
    assert r.watch_hits == ["Finance"]
    assert r.watch_only


def test_no_match_at_all():
    flt = FeedFilter(make_config().feed("md-substance-use"))
    r = flt.evaluate(
        make_bill(title="Vehicle Laws", synopsis="speed cameras", committee="Judiciary")
    )
    assert not r.matched and not r.watch_only and r.reasons == []


def test_search_hit_counts_as_match():
    flt = FeedFilter(make_config().feed("md-substance-use"))
    r = flt.evaluate(
        make_bill(title="Emergency Response", synopsis="teams", committee="Judiciary"),
        search_hits=["overdose"],
    )
    assert r.matched
    assert r.reasons == ["search: overdose"]


def test_exclude_keywords_veto_everything():
    cfg = make_config(exclude_keywords=["veterinary"])
    flt = FeedFilter(cfg.feed("md-substance-use"))
    b = make_bill(
        title="Veterinary Opioid Dispensing",
        synopsis="for animals",
        committee="Health and Government Operations",
    )
    r = flt.evaluate(b, search_hits=["opioid"])
    assert r.excluded and r.excluded_by == ["veterinary"]
    assert not r.matched and not r.watch_only
    assert r.keyword_hits == ["opioid"]  # still recorded, for debugging


def test_committee_names_are_normalised():
    cfg = make_config(watch_committees=["  health and   government operations "])
    flt = FeedFilter(cfg.feed("md-substance-use"))
    r = flt.evaluate(
        make_bill(title="x", synopsis="y", committee="Health and Government Operations")
    )
    assert r.watch_only
