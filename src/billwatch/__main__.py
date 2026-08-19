"""billwatch CLI: run | dry-run | backfill | reevaluate | fetch-texts | test-email.

Exit codes: 0 ok, 1 usage/config error, 2 fetch or delivery failure (so GitHub
Actions marks the run failed and emails you — free failure alerting).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, FeedConfig, Settings, load_config, load_dotenv
from .digest import Digest, NewBillItem, build_email, render_html, render_text
from .legiscan import BillSource, FixtureClient, LegiScanClient, LegiScanError
from .mailer import FileMailer, Mailer, MailError, make_mailer
from .models import Action, Bill, Hearing
from .pipeline import RunResult, run_pipeline
from .store import Store

log = logging.getLogger("billwatch")

DEFAULT_CONFIG = "config/feeds.toml"
DEFAULT_DB = "state/billwatch.db"

EXIT_OK, EXIT_USAGE, EXIT_FAILED = 0, 1, 2


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="billwatch", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"billwatch {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--env-file",
        default=".env",
        help="load KEY=VALUE pairs from this file if it exists (default: .env)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, *, fixtures: bool = True) -> None:
        sp.add_argument("--config", default=DEFAULT_CONFIG, help="feeds TOML file")
        sp.add_argument("--db", default=DEFAULT_DB, help="SQLite state file")
        sp.add_argument(
            "--feed",
            action="append",
            dest="feeds",
            metavar="NAME",
            help="only this feed (repeatable); default: all feeds",
        )
        sp.add_argument(
            "--today", type=_parse_date, default=None, help="override the run date (YYYY-MM-DD)"
        )
        sp.add_argument(
            "--max-queries",
            type=int,
            default=None,
            metavar="N",
            help="override settings.max_queries_per_run for this run "
            "(e.g. raise it for a one-time backfill; 0 = unlimited)",
        )
        if fixtures:
            sp.add_argument(
                "--fixtures",
                metavar="DIR",
                default=None,
                help="serve LegiScan responses from recorded JSON files (no network)",
            )

    sp_run = sub.add_parser("run", help="daily run: fetch, diff, send digest, persist")
    common(sp_run)

    sp_dry = sub.add_parser("dry-run", help="fetch + diff, render digest to files, change nothing")
    common(sp_dry)
    sp_dry.add_argument("--out", default="out", help="output directory for rendered digests")

    sp_bf = sub.add_parser("backfill", help="seed the state DB without sending a digest")
    common(sp_bf)

    sp_re = sub.add_parser(
        "reevaluate",
        help="re-apply the current feeds.toml rules to already-fetched bills (keyword tuning)",
    )
    common(sp_re)
    sp_re.add_argument(
        "--dry-run", action="store_true", help="report what would change, save nothing"
    )
    sp_re.add_argument(
        "--announce",
        action="store_true",
        help="put resulting new/watch items in the next digest (default: quiet)",
    )
    sp_re.add_argument(
        "--no-prune",
        action="store_true",
        help="only add/promote; never demote or remove bills that stopped matching",
    )
    sp_re.add_argument(
        "--no-fetch",
        action="store_true",
        help="do not fetch full details for new matches (use cached fields; 0 queries)",
    )
    sp_re.add_argument(
        "--refetch",
        action="store_true",
        help="re-fetch full detail for every tracked bill (1 query each) before reevaluating",
    )
    sp_re.add_argument(
        "--no-texts",
        action="store_true",
        help="do not fetch bill texts for tracked bills",
    )
    sp_re.add_argument(
        "--session-id",
        type=int,
        default=None,
        help="cached session to reevaluate (default: newest in cache)",
    )

    sp_tx = sub.add_parser(
        "fetch-texts", help="fetch/refresh the latest full text for tracked bills (1 query each)"
    )
    common(sp_tx)
    sp_tx.add_argument(
        "--show",
        metavar="NUMBER",
        default=None,
        help="print the stored text of one bill (e.g. HB1109) and exit; no fetch",
    )
    sp_tx.add_argument("--stats", action="store_true", help="print text storage stats and exit")

    sp_te = sub.add_parser("test-email", help="send a sample digest to verify SMTP settings")
    sp_te.add_argument("--config", default=DEFAULT_CONFIG)
    sp_te.add_argument(
        "--feed", dest="feed", default=None, help="feed whose title/recipients to use"
    )
    sp_te.add_argument(
        "--to", action="append", dest="to", metavar="ADDR", help="override recipients (repeatable)"
    )
    return p


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _client(
    settings: Settings, config: Config, fixtures: str | None, max_queries: int | None = None
) -> BillSource:
    budget: int | None = (
        max_queries if max_queries is not None else config.settings.max_queries_per_run
    )
    if budget is not None and budget <= 0:
        budget = None  # 0 or negative → unlimited
    if fixtures:
        return FixtureClient(fixtures, max_queries=budget)
    if not settings.legiscan_api_key:
        raise ConfigError("LEGISCAN_API_KEY is not set (and no --fixtures given)")
    return LegiScanClient(
        settings.legiscan_api_key, base_url=settings.legiscan_base_url, max_queries=budget
    )


def _memory_copy(db_path: str) -> Store:
    """In-memory Store seeded from an on-disk DB (dry-run must never mutate real state)."""
    mem = Store(":memory:")
    if Path(db_path).is_file():
        src = sqlite3.connect(db_path)
        try:
            src.backup(mem._conn)
        finally:
            src.close()
    return mem


def _recipients_fn(settings: Settings):
    def fn(feed: FeedConfig) -> Sequence[str]:
        return settings.recipients_for(feed)

    return fn


def _report(run: RunResult) -> None:
    for name, r in run.feeds.items():
        if r.error:
            log.error("[%s] FAILED: %s", name, r.error)
            continue
        state = "sent" if r.sent else ("skipped" if r.skipped else "rendered")
        summary = r.digest.summary if r.digest else "-"
        log.info(
            "[%s] %s — %s (new=%d changed=%d watch=%d hearings=%d)",
            name,
            state,
            summary,
            r.new_bills,
            r.changed,
            r.watch,
            r.hearings_announced,
        )


def sample_digest(config: Config, feed: FeedConfig, today: date) -> Digest:
    """A small fake digest for `test-email` (no live data needed)."""
    bill = Bill(
        bill_id=0,
        state=feed.state,
        number="HB 0000",
        title="Sample Bill — Overdose Prevention and Naloxone Access",
        synopsis="This is a sample entry generated by `billwatch test-email` to verify delivery.",
        url="https://legiscan.com/",
        state_url="",
        status=1,
        status_date=today.isoformat(),
        committee="Health and Government Operations",
        change_hash="sample",
        session_name="Sample Session",
        last_action="First Reading",
        last_action_date=today.isoformat(),
        history=[Action(date=today.isoformat(), action="First Reading")],
        hearings=[Hearing(bill_id=0, date=today.isoformat(), committee="Sample Committee")],
    )
    d = Digest(
        feed=feed,
        run_date=today.isoformat(),
        lookahead_days=config.lookahead_days(feed),
        unsubscribe_note=config.settings.unsubscribe_note,
        subject_prefix=config.settings.subject_prefix,
    )
    d.new_bills.append(NewBillItem(bill=bill, reasons=["keyword: naloxone"], event_id=0))
    d.tracked_count = 1
    return d


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace, settings: Settings, config: Config) -> int:
    today = args.today or date.today()
    client = _client(settings, config, args.fixtures, args.max_queries)
    mailer: Mailer = make_mailer(settings)
    with Store(args.db) as store:
        run = run_pipeline(
            config=config,
            client=client,
            store=store,
            today=today,
            mailer=mailer,
            recipients_for=_recipients_fn(settings),
            sender=settings.smtp_from or "",
            feed_names=args.feeds,
        )
    _report(run)
    return EXIT_OK if run.ok else EXIT_FAILED


def cmd_dry_run(args: argparse.Namespace, settings: Settings, config: Config) -> int:
    today = args.today or date.today()
    client = _client(settings, config, args.fixtures, args.max_queries)
    out = FileMailer(args.out)
    with _memory_copy(args.db) as store:
        run = run_pipeline(
            config=config,
            client=client,
            store=store,
            today=today,
            mailer=None,
            recipients_for=lambda f: [],
            sender="dry-run@localhost",
            feed_names=args.feeds,
        )
        for name, r in run.feeds.items():
            if r.digest is None:
                continue
            msg = build_email(r.digest, "dry-run@localhost")
            out.send(msg, [])
            log.info(
                "[%s] dry-run digest written: %s", name, ", ".join(str(p) for p in out.written[-2:])
            )
    _report(run)
    return EXIT_OK if run.ok else EXIT_FAILED


def cmd_backfill(args: argparse.Namespace, settings: Settings, config: Config) -> int:
    today = args.today or date.today()
    client = _client(settings, config, args.fixtures, args.max_queries)
    with Store(args.db) as store:
        run = run_pipeline(
            config=config,
            client=client,
            store=store,
            today=today,
            mailer=None,
            recipients_for=lambda f: [],
            sender="",
            feed_names=args.feeds,
            announce=False,
        )
        for name in run.feeds:
            log.info("[%s] backfill: %d tracked bill(s) in store", name, store.count_bills(name))
    _report(run)
    return EXIT_OK if run.ok else EXIT_FAILED


def cmd_reevaluate(args: argparse.Namespace, settings: Settings, config: Config) -> int:
    from .reevaluate import format_report, reevaluate

    # API access is optional here: without a key we can still re-run keyword/committee rules.
    client: BillSource | None
    try:
        client = _client(settings, config, args.fixtures, args.max_queries)
    except ConfigError:
        client = None
        log.warning("LEGISCAN_API_KEY not set: searches skipped and new matches use cached fields")
    with Store(args.db) as store:
        results = reevaluate(
            store,
            config,
            client=client,
            feed_names=args.feeds,
            session_id=args.session_id,
            fetch_details=not args.no_fetch,
            prune=not args.no_prune,
            announce=args.announce,
            dry_run=args.dry_run,
            refetch=args.refetch,
            sync_text=not args.no_texts,
        )
    for r in results:
        print(format_report(r))
    return EXIT_OK


def cmd_fetch_texts(args: argparse.Namespace, settings: Settings, config: Config) -> int:
    from datetime import UTC, datetime

    from .texts import sync_texts

    feeds = [config.feed(n) for n in args.feeds] if args.feeds else list(config.feeds.values())
    with Store(args.db) as store:
        if args.stats:
            st = store.text_stats()
            print(
                f"{st['n']} bill text(s) stored, {st['chars']:,} chars, "
                f"{st['bytes']:,} bytes compressed"
            )
            return EXIT_OK
        if args.show:
            want = args.show.replace(" ", "").upper()
            for feed in feeds:
                for tb in store.tracked_bills(feed.name):
                    if tb.bill.number.replace(" ", "").upper() == want:
                        t = store.get_text(feed.state, tb.bill.bill_id)
                        if t is None:
                            log.error("%s is tracked but has no stored text yet", want)
                            return EXIT_FAILED
                        print(
                            f"# {tb.bill.number} — {tb.bill.title}\n# {t['version']} "
                            f"({t['date']}) · {t['chars']} chars · {t['source_url']}\n"
                        )
                        print(t["text"])
                        return EXIT_OK
            log.error("%s is not a tracked bill in %s", want, [f.name for f in feeds])
            return EXIT_USAGE
        client = _client(settings, config, args.fixtures, args.max_queries)
        when = datetime.now(UTC).replace(microsecond=0).isoformat()
        for feed in feeds:
            bills = [tb.bill for tb in store.tracked_bills(feed.name)]
            res = sync_texts(store, client, bills, when=when)
            store.commit()
            log.info(
                "[%s] texts: %d fetched, %d already current, %d failed, %d deferred (%d queries)",
                feed.name,
                res.fetched,
                res.skipped,
                res.failed,
                res.deferred,
                res.queries,
            )
        st = store.text_stats()
        log.info("%d bill text(s) stored, %s bytes compressed", st["n"], f"{st['bytes']:,}")
    return EXIT_OK


def cmd_test_email(args: argparse.Namespace, settings: Settings, config: Config) -> int:
    feed = config.feed(args.feed) if args.feed else next(iter(config.feeds.values()))
    recipients = list(args.to or []) or settings.recipients_for(feed)
    if not recipients:
        raise ConfigError(f"no recipients: pass --to or set ${feed.recipients_env}")
    digest = sample_digest(config, feed, date.today())
    msg = build_email(digest, settings.smtp_from or "billwatch@localhost")
    mailer = make_mailer(settings)
    mailer.send(msg, recipients)
    log.info("test email sent via %s to %d recipient(s)", mailer.name, len(recipients))
    return EXIT_OK


def render_sample(config: Config, feed: FeedConfig, today: date) -> tuple[str, str]:
    """Used by tests/docs: HTML + text of the sample digest."""
    d = sample_digest(config, feed, today)
    return render_html(d), render_text(d)


COMMANDS = {
    "run": cmd_run,
    "dry-run": cmd_dry_run,
    "backfill": cmd_backfill,
    "reevaluate": cmd_reevaluate,
    "fetch-texts": cmd_fetch_texts,
    "test-email": cmd_test_email,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if args.env_file:
        load_dotenv(args.env_file)
    settings = Settings.from_env()
    try:
        config = load_config(args.config)
        return COMMANDS[args.command](args, settings, config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_USAGE
    except (LegiScanError, MailError) as exc:
        log.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
