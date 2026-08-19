"""Re-apply the current feed rules to bills already fetched (offline keyword tuning).

The daily pipeline only evaluates a bill when its LegiScan change_hash changes. That
means editing keywords/searches/watch_committees in feeds.toml would otherwise not
touch the thousands of bills already seen until they next move. `reevaluate` closes
that gap using the `bill_cache` table:

  * new matches        → tracked (full detail fetched, ~1 query each, unless --no-fetch)
  * watch-only → match → promoted to tracked
  * tracked → no rule  → demoted to watch-only, or removed if no watched committee either
  * watch → no rule    → removed
  * unchanged          → reasons refreshed to reflect current rules

Searches are re-run against the API (one query per configured search) so search-term
changes apply too. Nothing is emailed: by default the resulting events are recorded as
already sent (tuning should not spam the digest); pass announce=True to surface them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import Config, FeedConfig
from .filters import FeedFilter
from .legiscan import BillSource, LegiScanError, QuotaExceeded
from .models import Bill
from .store import Store
from .texts import sync_texts

log = logging.getLogger(__name__)


@dataclass
class Change:
    number: str
    title: str
    action: str  # added | promoted | demoted | removed | updated
    reasons: list[str]
    before: list[str] = field(default_factory=list)


@dataclass
class ReevalResult:
    feed: str
    session_id: int | None
    cached: int = 0
    added: list[Change] = field(default_factory=list)
    promoted: list[Change] = field(default_factory=list)
    demoted: list[Change] = field(default_factory=list)
    removed: list[Change] = field(default_factory=list)
    updated: list[Change] = field(default_factory=list)
    unchanged: int = 0
    refetched: int = 0
    texts_fetched: int = 0
    queries: int = 0
    dry_run: bool = False

    @property
    def changes(self) -> list[Change]:
        return self.added + self.promoted + self.demoted + self.removed + self.updated

    def summary(self) -> str:
        extra = ""
        if self.refetched:
            extra += f", {self.refetched} refetched"
        if self.texts_fetched:
            extra += f", {self.texts_fetched} text(s) fetched"
        return (
            f"{len(self.added)} added, {len(self.promoted)} promoted, {len(self.demoted)} demoted, "
            f"{len(self.removed)} removed, {len(self.updated)} reasons updated, "
            f"{self.unchanged} unchanged (of {self.cached} cached){extra}"
        )


def _search_hits(
    client: BillSource | None, feed: FeedConfig, config: Config, session_id: int | None
) -> dict[int, list[str]]:
    """bill_id → list of search queries that surfaced it (relevance-filtered)."""
    hits: dict[int, list[str]] = {}
    if client is None:
        return hits
    min_rel = config.search_min_relevance(feed)
    for query in feed.searches:
        try:
            for h in client.search(feed.state, query, session_id):
                if h.relevance >= min_rel:
                    hits.setdefault(h.bill_id, []).append(query)
        except QuotaExceeded:
            log.warning("[%s] query budget exhausted before search %r", feed.name, query)
            break
    return hits


def _full_bill(client: BillSource | None, lite: Bill, *, fetch: bool) -> Bill:
    """Prefer a fresh full fetch (history, hearings, sponsors); fall back to the cached fields."""
    if client is None or not fetch:
        return lite
    try:
        return client.get_bill(lite.bill_id)
    except (QuotaExceeded, LegiScanError) as exc:
        log.warning(
            "[%s] could not fetch detail for %s (%s); using cached fields",
            lite.state,
            lite.number,
            type(exc).__name__,
        )
        return lite


def reevaluate_feed(
    store: Store,
    config: Config,
    feed: FeedConfig,
    *,
    client: BillSource | None,
    session_id: int | None = None,
    fetch_details: bool = True,
    prune: bool = True,
    announce: bool = False,
    dry_run: bool = False,
    refetch: bool = False,
    sync_text: bool = True,
) -> ReevalResult:
    """Apply the feed's current rules to every cached bill of the chosen session.

    session_id: which session's cached bills to consider; default = the feed's pinned
    session, else the newest session present in the cache for that state.
    prune: when False, only add/promote — never demote/remove.
    refetch: re-fetch full detail for every tracked bill (1 query each) — refreshes
    history/hearings/cross-file links/text versions for bills stored before those existed.
    sync_text: fetch the latest text version for tracked bills that lack it.
    dry_run: compute and report, then roll back.
    """
    if session_id is None:
        session_id = feed.session_id
    if session_id is None:
        sessions = store.cached_sessions(feed.state)
        session_id = sessions[-1] if sessions else None
    res = ReevalResult(feed=feed.name, session_id=session_id, dry_run=dry_run)
    cached = store.cached_bills(feed.state, session_id)
    res.cached = len(cached)
    if not cached:
        log.warning(
            "[%s] bill cache is empty for %s — run `billwatch backfill` first",
            feed.name,
            feed.state,
        )
        return res

    before_q = client.query_count if client else 0
    hits = _search_hits(client, feed, config, session_id)
    flt = FeedFilter(feed)
    when = datetime.now(UTC).replace(microsecond=0).isoformat()
    sent = not announce
    by_id = {b.bill_id: b for b in cached}

    # Optional refresh of every tracked bill's full detail (brings in sasts/texts/history).
    if refetch and client is not None:
        for tb in store.tracked_bills(feed.name):
            rem = client.remaining()
            if rem is not None and rem <= 0:
                break
            try:
                full = client.get_bill(tb.bill.bill_id)
            except (QuotaExceeded, LegiScanError) as exc:
                log.warning(
                    "[%s] refetch %s failed: %s", feed.name, tb.bill.number, type(exc).__name__
                )
                continue
            store.cache_bill(full, when)
            store.upsert_bill(feed.name, full, tracked=True, reasons=tb.reasons, when=when)
            store.replace_hearings(feed.name, full.bill_id, full.hearings)
            by_id[full.bill_id] = full
            res.refetched += 1

    # Pass 1: keyword/search rules for every cached bill.
    base = {bid: flt.evaluate(b, search_hits=hits.get(bid, [])) for bid, b in by_id.items()}

    # Pass 2: cross-files of rule-matched bills are matches too (same bill, other chamber).
    crossfile_of: dict[int, list[str]] = {}
    for bid, m in base.items():
        if not m.matched:
            continue
        src = by_id[bid]
        links = src.crossfiles
        if not links:  # cache row may predate the sasts column; fall back to the stored bill
            tb = store.get_bill(feed.name, bid)
            links = tb.bill.crossfiles if tb else []
        for rel in links:
            if rel.bill_id in by_id:
                crossfile_of.setdefault(rel.bill_id, []).append(src.number)

    touched: list[Bill] = []
    for bid, lite in by_id.items():
        match = (
            flt.evaluate(lite, search_hits=hits.get(bid, []), crossfile_of=crossfile_of[bid])
            if bid in crossfile_of
            else base[bid]
        )
        existing = store.get_bill(feed.name, bid)
        ch = Change(
            lite.number, lite.title, "", match.reasons, before=existing.reasons if existing else []
        )

        if existing is None:
            if match.matched:
                bill = _full_bill(client, lite, fetch=fetch_details)
                store.upsert_bill(feed.name, bill, tracked=True, reasons=match.reasons, when=when)
                store.replace_hearings(feed.name, bill.bill_id, bill.hearings)
                store.add_event(
                    feed.name, bill.bill_id, "new", {"reasons": match.reasons}, when, sent=sent
                )
                touched.append(bill)
                ch.action = "added"
                res.added.append(ch)
            elif match.watch_only:
                store.upsert_bill(feed.name, lite, tracked=False, reasons=match.reasons, when=when)
                store.add_event(
                    feed.name, lite.bill_id, "watch", {"reasons": match.reasons}, when, sent=sent
                )
                ch.action = "added"
                res.added.append(ch)
            else:
                res.unchanged += 1
            continue

        if existing.tracked:
            if match.matched:
                if sorted(match.reasons) != sorted(existing.reasons):
                    store.upsert_bill(
                        feed.name, existing.bill, tracked=True, reasons=match.reasons, when=when
                    )
                    ch.action = "updated"
                    res.updated.append(ch)
                else:
                    res.unchanged += 1
                if sync_text:
                    touched.append(existing.bill)
            elif not prune:
                res.unchanged += 1
            elif match.watch_only:
                store.replace_hearings(feed.name, existing.bill.bill_id, [])
                store.delete_text(feed.state, existing.bill.bill_id)
                store.upsert_bill(
                    feed.name, existing.bill, tracked=False, reasons=match.reasons, when=when
                )
                ch.action = "demoted"
                res.demoted.append(ch)
            else:
                store.delete_bill(feed.name, existing.bill.bill_id)
                store.delete_text(feed.state, existing.bill.bill_id)
                ch.action = "removed"
                res.removed.append(ch)
            continue

        # watch-only row
        if match.matched:
            bill = _full_bill(client, lite, fetch=fetch_details)
            store.delete_events(feed.name, bill.bill_id, kind="watch", unsent_only=True)
            store.upsert_bill(feed.name, bill, tracked=True, reasons=match.reasons, when=when)
            store.replace_hearings(feed.name, bill.bill_id, bill.hearings)
            store.add_event(
                feed.name, bill.bill_id, "new", {"reasons": match.reasons}, when, sent=sent
            )
            touched.append(bill)
            ch.action = "promoted"
            res.promoted.append(ch)
        elif match.watch_only:
            if sorted(match.reasons) != sorted(existing.reasons):
                store.upsert_bill(
                    feed.name, existing.bill, tracked=False, reasons=match.reasons, when=when
                )
                ch.action = "updated"
                res.updated.append(ch)
            else:
                res.unchanged += 1
        elif prune:
            store.delete_bill(feed.name, existing.bill.bill_id)
            ch.action = "removed"
            res.removed.append(ch)
        else:
            res.unchanged += 1

    if sync_text and not dry_run:
        res.texts_fetched = sync_texts(store, client, touched, when=when).fetched

    res.queries = (client.query_count - before_q) if client else 0
    if dry_run:
        store.rollback()
    else:
        store.commit()
    log.info(
        "[%s] reevaluate%s: %s; %d LegiScan quer%s",
        feed.name,
        " (dry-run, rolled back)" if dry_run else "",
        res.summary(),
        res.queries,
        "y" if res.queries == 1 else "ies",
    )
    return res


def format_report(res: ReevalResult) -> str:
    lines = [
        f"== {res.feed} (session {res.session_id}) — {res.summary()}"
        + (" [DRY RUN — nothing saved]" if res.dry_run else "")
    ]
    for label, items in (
        ("ADDED", res.added),
        ("PROMOTED → tracked", res.promoted),
        ("DEMOTED → watch-only", res.demoted),
        ("REMOVED", res.removed),
        ("REASONS UPDATED", res.updated),
    ):
        if not items:
            continue
        lines.append(f"-- {label} ({len(items)})")
        for c in items:
            why = "; ".join(c.reasons) if c.reasons else "no rule matches"
            was = f"  (was: {'; '.join(c.before)})" if c.before and c.action != "added" else ""
            lines.append(f"   {c.number:8} {c.title[:70]:70} {why}{was}")
    return "\n".join(lines)


def reevaluate(
    store: Store,
    config: Config,
    *,
    client: BillSource | None,
    feed_names: Sequence[str] | None = None,
    **kwargs,
) -> list[ReevalResult]:
    feeds = [config.feed(n) for n in feed_names] if feed_names else list(config.feeds.values())
    return [reevaluate_feed(store, config, f, client=client, **kwargs) for f in feeds]
