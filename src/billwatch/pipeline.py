"""The daily run (design §5.4): fetch → filter → diff → digest → send → persist.

Feeds are grouped by (state, session) so a state's master list and bill details
are fetched once and shared across every topic feed for that state — the
Phase-4 "batch feeds per state" idea, which costs nothing to do from day one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from .config import Config, FeedConfig
from .digest import Digest, build_digest, build_email
from .filters import FeedFilter, MatchResult
from .legiscan import BillSource, LegiScanError, QuotaExceeded, pick_current_session
from .mailer import Mailer, MailError
from .models import Bill, StatusChange, TrackedBill
from .store import Store

log = logging.getLogger(__name__)


def state_scope(state: str) -> str:
    return f"state:{state}"


def search_scope(feed_name: str) -> str:
    return f"search:{feed_name}"


@dataclass
class FeedRunResult:
    feed: str
    new_bills: int = 0
    changed: int = 0
    watch: int = 0
    hearings_announced: int = 0
    digest: Digest | None = None
    sent: bool = False
    skipped: bool = False
    error: str | None = None
    queries: int = 0


@dataclass
class RunResult:
    feeds: dict[str, FeedRunResult] = field(default_factory=dict)
    queries: int = 0
    candidates: int = 0
    fetched: int = 0
    deferred: int = 0  # candidates not fetched because the query budget ran out
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #


def compute_change(old: Bill, new: Bill) -> StatusChange:
    """What a reader would care about between two fetches of the same bill."""
    known = {(a.date, a.action) for a in old.history}
    if known:
        added = [a for a in new.history if (a.date, a.action) not in known]
    else:  # legacy rows without history: fall back to last-action comparison
        added = [
            a
            for a in new.history
            if old.last_action_date is None
            or a.date > old.last_action_date
            or (a.date == old.last_action_date and a.action != old.last_action)
        ]
    return StatusChange(
        old_status=old.status,
        new_status=new.status,
        old_committee=old.committee,
        new_committee=new.committee,
        actions=added,
    )


# --------------------------------------------------------------------------- #
# Per-bill processing for one feed
# --------------------------------------------------------------------------- #


def process_bill(
    store: Store,
    feed: FeedConfig,
    flt: FeedFilter,
    bill: Bill,
    *,
    search_hits: list[str],
    when: str,
    result: FeedRunResult,
) -> None:
    existing: TrackedBill | None = store.get_bill(feed.name, bill.bill_id)
    match: MatchResult = flt.evaluate(bill, search_hits=search_hits)

    if existing is None:
        if match.matched:
            store.upsert_bill(feed.name, bill, tracked=True, reasons=match.reasons, when=when)
            store.replace_hearings(feed.name, bill.bill_id, bill.hearings)
            store.add_event(feed.name, bill.bill_id, "new", {"reasons": match.reasons}, when)
            result.new_bills += 1
            log.info("[%s] new match %s (%s)", feed.name, bill.number, "; ".join(match.reasons))
        elif match.watch_only:
            store.upsert_bill(feed.name, bill, tracked=False, reasons=match.reasons, when=when)
            store.add_event(feed.name, bill.bill_id, "watch", {"reasons": match.reasons}, when)
            result.watch += 1
            log.info(
                "[%s] committee-watch %s (%s)", feed.name, bill.number, "; ".join(match.reasons)
            )
        return

    if existing.tracked:
        change = compute_change(existing.bill, bill)
        # keep the original reasons, add any new ones
        reasons = list(existing.reasons)
        reasons += [r for r in match.reasons if r not in reasons and not r.startswith("committee")]
        store.upsert_bill(feed.name, bill, tracked=True, reasons=reasons, when=when)
        store.replace_hearings(feed.name, bill.bill_id, bill.hearings)
        if not change.is_empty():
            store.add_event(
                feed.name,
                bill.bill_id,
                "status",
                {
                    "old_status": change.old_status,
                    "new_status": change.new_status,
                    "old_committee": change.old_committee,
                    "new_committee": change.new_committee,
                    "actions": [
                        {"date": a.date, "action": a.action, "chamber": a.chamber}
                        for a in change.actions
                    ],
                },
                when,
            )
            result.changed += 1
            log.info(
                "[%s] movement on %s: %d new action(s)", feed.name, bill.number, len(change.actions)
            )
        return

    # watch-only bill: promote if it now matches, otherwise just refresh
    if match.matched:
        store.upsert_bill(feed.name, bill, tracked=True, reasons=match.reasons, when=when)
        store.replace_hearings(feed.name, bill.bill_id, bill.hearings)
        store.add_event(feed.name, bill.bill_id, "new", {"reasons": match.reasons}, when)
        result.new_bills += 1
    else:
        store.upsert_bill(
            feed.name, bill, tracked=False, reasons=match.reasons or existing.reasons, when=when
        )


# --------------------------------------------------------------------------- #
# Fetch phase for one (state, session) group of feeds
# --------------------------------------------------------------------------- #


def fetch_state(
    client: BillSource,
    store: Store,
    config: Config,
    feeds: Sequence[FeedConfig],
    *,
    when: str,
    run: RunResult,
    results: dict[str, FeedRunResult],
) -> None:
    state = feeds[0].state
    pinned = feeds[0].session_id
    session_id = pinned
    if session_id is None:
        sessions = client.get_session_list(state)
        current = pick_current_session(sessions)
        if current is not None:
            session_id = current.session_id
            log.info("[%s] tracking session %s (%s)", state, current.session_id, current.name)
        else:
            log.info("[%s] no session list available; using LegiScan default session", state)

    master = client.get_master_list(state, session_id)
    if master.session and session_id is None:
        log.info("[%s] master list session: %s", state, master.session.name)
    scope = state_scope(state)
    seen = store.seen_hashes(scope)
    candidates: dict[int, str] = {}  # bill_id → master-list change_hash
    for bill_id, entry in master.entries.items():
        if seen.get(bill_id) != entry.change_hash:
            candidates[bill_id] = entry.change_hash
    log.info(
        "[%s] master list: %d bills, %d new/changed", state, len(master.entries), len(candidates)
    )

    # Secondary net: full-text searches per feed
    search_hits: dict[str, dict[int, list[str]]] = {f.name: {} for f in feeds}
    search_hash: dict[str, dict[int, str]] = {f.name: {} for f in feeds}
    for feed in feeds:
        min_rel = config.search_min_relevance(feed)
        sscope = search_scope(feed.name)
        seen_search = store.seen_hashes(sscope)
        for query in feed.searches:
            try:
                hits = client.search(state, query, session_id)
            except QuotaExceeded:
                log.warning("[%s] query budget exhausted before search %r", feed.name, query)
                break
            for hit in hits:
                if hit.relevance < min_rel:
                    continue
                search_hits[feed.name].setdefault(hit.bill_id, []).append(query)
                search_hash[feed.name][hit.bill_id] = hit.change_hash
                if hit.bill_id in candidates:
                    continue  # already being fetched because its master-list hash changed
                if hit.bill_id not in master.entries:
                    continue  # different session / not in this master list: ignore
                if seen_search.get(hit.bill_id) == hit.change_hash:
                    continue  # evaluated this exact search hit before
                existing = store.get_bill(feed.name, hit.bill_id)
                if existing is None or not existing.tracked:  # untracked or watch-only
                    candidates[hit.bill_id] = master.entries[hit.bill_id].change_hash

    run.candidates += len(candidates)
    filters = {f.name: FeedFilter(f) for f in feeds}
    ordered = sorted(candidates.items())
    fetched = 0
    for idx, (bill_id, mhash) in enumerate(ordered):
        rem = client.remaining()
        if rem is not None and rem <= 0:
            deferred = len(ordered) - idx
            run.deferred += deferred
            log.warning(
                "[%s] query budget exhausted; deferring %d candidate(s) to next run",
                state,
                deferred,
            )
            break
        try:
            bill = client.get_bill(bill_id)
        except QuotaExceeded:
            run.deferred += len(ordered) - idx
            log.warning(
                "[%s] query budget exhausted mid-fetch; deferring %d", state, len(ordered) - idx
            )
            break
        fetched += 1
        store.cache_bill(bill, when)
        for feed in feeds:
            process_bill(
                store,
                feed,
                filters[feed.name],
                bill,
                search_hits=search_hits[feed.name].get(bill_id, []),
                when=when,
                result=results[feed.name],
            )
            if bill_id in search_hash[feed.name]:
                store.mark_seen(
                    search_scope(feed.name), bill_id, search_hash[feed.name][bill_id], when
                )
        store.mark_seen(scope, bill_id, mhash, when)
    run.fetched += fetched
    log.info("[%s] fetched %d bill detail(s)", state, fetched)


# --------------------------------------------------------------------------- #
# Delivery phase for one feed
# --------------------------------------------------------------------------- #


def deliver_feed(
    store: Store,
    config: Config,
    feed: FeedConfig,
    *,
    today: date,
    mailer: Mailer | None,
    recipients: Sequence[str],
    sender: str,
    result: FeedRunResult,
    announce: bool = True,
) -> Digest:
    digest = build_digest(store, config, feed, today)
    result.digest = digest
    run_date = today.isoformat()

    if not announce:
        # backfill: swallow pending events silently, leave upcoming hearings for the next digest
        n = store.mark_all_events_sent(feed.name)
        result.skipped = True
        store.log_run(
            run_date,
            feed.name,
            new_bills=len(digest.new_bills),
            changed=len(digest.movement),
            watch=len(digest.watch),
            skipped=True,
            sent=False,
            queries=result.queries,
        )
        log.info("[%s] backfill: %d event(s) recorded without sending", feed.name, n)
        return digest

    if digest.is_empty and not config.send_empty(feed):
        result.skipped = True
        store.mark_events_sent(digest.event_ids)  # only orphan events can be pending here
        store.log_run(run_date, feed.name, skipped=True, sent=False, queries=result.queries)
        log.info("[%s] nothing to report; digest skipped", feed.name)
        return digest

    if mailer is None:
        log.info("[%s] dry-run: digest rendered, not sent (%s)", feed.name, digest.summary)
        return digest

    message = build_email(digest, sender)
    try:
        mailer.send(message, recipients)
    except MailError as exc:
        # Fetch state stays committed; events remain unsent and merge into the next digest.
        store.log_run(
            run_date,
            feed.name,
            new_bills=len(digest.new_bills),
            changed=len(digest.movement),
            hearings=len(digest.hearings),
            watch=len(digest.watch),
            skipped=False,
            sent=False,
            queries=result.queries,
        )
        result.error = f"send failed: {exc}"
        log.error("[%s] %s", feed.name, result.error)
        return digest

    store.mark_events_sent(digest.event_ids)
    store.mark_hearings_announced(feed.name, [h.hearing for h in digest.hearings])
    result.sent = True
    result.hearings_announced = len(digest.hearings)
    store.log_run(
        run_date,
        feed.name,
        new_bills=len(digest.new_bills),
        changed=len(digest.movement),
        hearings=len(digest.hearings),
        watch=len(digest.watch),
        skipped=False,
        sent=True,
        queries=result.queries,
    )
    log.info("[%s] digest sent via %s: %s", feed.name, mailer.name, digest.summary)
    return digest


# --------------------------------------------------------------------------- #
# Whole run
# --------------------------------------------------------------------------- #


def run_pipeline(
    *,
    config: Config,
    client: BillSource,
    store: Store,
    today: date,
    mailer: Mailer | None,
    recipients_for: Callable[[FeedConfig], Sequence[str]],
    sender: str = "",
    feed_names: Sequence[str] | None = None,
    announce: bool = True,
) -> RunResult:
    """Run every (or the selected) feed(s).

    Failures are recorded in RunResult.errors rather than raised: a fetch failure rolls
    back that state's partial work (its feeds are skipped), and a delivery failure leaves
    the fetched state committed with its events unsent (NFR3). Callers decide the exit code.
    """
    feeds = [config.feed(n) for n in feed_names] if feed_names else list(config.feeds.values())
    run = RunResult()
    results = {f.name: FeedRunResult(feed=f.name) for f in feeds}
    when = _now_iso()

    # Group by (state, pinned session) so shared fetches happen once.
    groups: dict[tuple[str, int | None], list[FeedConfig]] = {}
    for f in feeds:
        groups.setdefault((f.state, f.session_id), []).append(f)

    for (state, _sid), group in groups.items():
        before = client.query_count
        try:
            fetch_state(client, store, config, group, when=when, run=run, results=results)
        except LegiScanError as exc:
            store.rollback()
            msg = f"[{state}] fetch failed: {exc}"
            log.error(msg)
            run.errors.append(msg)
            for f in group:
                results[f.name].error = msg
            continue
        used = client.query_count - before
        for f in group:
            results[f.name].queries = used  # attributed to each feed in the group (quota log)
        store.commit()

    for f in feeds:
        res = results[f.name]
        if res.error:
            continue
        deliver_feed(
            store,
            config,
            f,
            today=today,
            mailer=mailer,
            recipients=recipients_for(f),
            sender=sender,
            result=res,
            announce=announce,
        )
        store.commit()
        if res.error:
            run.errors.append(f"[{f.name}] {res.error}")

    run.feeds = results
    run.queries = client.query_count
    log.info(
        "run complete: %d LegiScan quer%s, %d candidate(s), %d fetched, %d deferred",
        run.queries,
        "y" if run.queries == 1 else "ies",
        run.candidates,
        run.fetched,
        run.deferred,
    )
    return run
