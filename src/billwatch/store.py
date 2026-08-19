"""SQLite state: bills, hearings, seen-hashes, pending digest events, sent-log.

Privacy invariant (design §8.1): this database holds only public legislative
data and aggregate counts. No recipient, credential, or per-person record ever
goes here.

All mutations happen inside one transaction per `commit()`; the pipeline
commits after each state's fetch phase and again after delivery, so a crash
leaves the DB at a consistent point and the next run picks up the rest (NFR3).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from .models import Action, Bill, DigestEvent, Hearing, TrackedBill

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS bills (
  bill_id       INTEGER NOT NULL,
  feed          TEXT    NOT NULL,
  state         TEXT,
  number        TEXT,
  title         TEXT,
  synopsis      TEXT,
  url           TEXT,
  state_url     TEXT,
  status        INTEGER,
  status_date   TEXT,
  committee     TEXT,
  change_hash   TEXT,
  session_id    INTEGER,
  session_name  TEXT,
  last_action   TEXT,
  last_action_date TEXT,
  referrals     TEXT,      -- JSON list
  sponsors      TEXT,      -- JSON list
  history       TEXT,      -- JSON list of {date, action, chamber}
  tracked       INTEGER NOT NULL DEFAULT 1,   -- 1 = keyword/search match, 0 = committee-watch only
  reasons       TEXT,      -- JSON list of match reasons
  first_seen    TEXT,
  last_updated  TEXT,
  PRIMARY KEY (feed, bill_id)
);

CREATE TABLE IF NOT EXISTS hearings (
  feed          TEXT    NOT NULL,
  bill_id       INTEGER NOT NULL,
  date          TEXT    NOT NULL,
  committee     TEXT    NOT NULL,
  time          TEXT,
  location      TEXT,
  kind          TEXT,
  announced_in_digest INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (feed, bill_id, date, committee)
);

CREATE TABLE IF NOT EXISTS seen (
  scope         TEXT    NOT NULL,   -- "state:MD" (masterlist) or "search:<feed>"
  bill_id       INTEGER NOT NULL,
  change_hash   TEXT    NOT NULL,
  last_checked  TEXT,
  PRIMARY KEY (scope, bill_id)
);

CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  feed          TEXT    NOT NULL,
  bill_id       INTEGER NOT NULL,
  kind          TEXT    NOT NULL,   -- new | status | watch
  detail        TEXT,               -- JSON
  created       TEXT    NOT NULL,
  sent          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_unsent ON events (feed, sent);

CREATE TABLE IF NOT EXISTS sent_log (
  run_date      TEXT NOT NULL,
  feed          TEXT NOT NULL,
  new_bills     INTEGER NOT NULL DEFAULT 0,
  changed       INTEGER NOT NULL DEFAULT 0,
  hearings      INTEGER NOT NULL DEFAULT 0,
  watch         INTEGER NOT NULL DEFAULT 0,
  skipped       INTEGER NOT NULL DEFAULT 0,   -- 1 if digest suppressed (no changes)
  sent          INTEGER NOT NULL DEFAULT 0,   -- 1 if delivered
  queries       INTEGER NOT NULL DEFAULT 0,   -- LegiScan queries used this run (quota log)
  PRIMARY KEY (run_date, feed)
);
"""


def _dumps(v: object) -> str:
    return json.dumps(v, ensure_ascii=False)


def _loads(v: str | None, default: object) -> object:
    if not v:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level="DEFERRED")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # -- lifecycle --------------------------------------------------------- #
    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- seen hashes (change detection) ------------------------------------ #
    def seen_hash(self, scope: str, bill_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT change_hash FROM seen WHERE scope=? AND bill_id=?", (scope, bill_id)
        ).fetchone()
        return row["change_hash"] if row else None

    def seen_hashes(self, scope: str) -> dict[int, str]:
        rows = self._conn.execute(
            "SELECT bill_id, change_hash FROM seen WHERE scope=?", (scope,)
        ).fetchall()
        return {r["bill_id"]: r["change_hash"] for r in rows}

    def mark_seen(self, scope: str, bill_id: int, change_hash: str, when: str) -> None:
        self._conn.execute(
            """INSERT INTO seen (scope, bill_id, change_hash, last_checked)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(scope, bill_id) DO UPDATE SET
                 change_hash=excluded.change_hash, last_checked=excluded.last_checked""",
            (scope, bill_id, change_hash, when),
        )

    # -- bills ------------------------------------------------------------- #
    def get_bill(self, feed: str, bill_id: int) -> TrackedBill | None:
        row = self._conn.execute(
            "SELECT * FROM bills WHERE feed=? AND bill_id=?", (feed, bill_id)
        ).fetchone()
        return self._row_to_tracked(row) if row else None

    def _row_to_tracked(self, row: sqlite3.Row) -> TrackedBill:
        history = [Action(**h) for h in _loads(row["history"], [])]  # type: ignore[arg-type]
        bill = Bill(
            bill_id=row["bill_id"],
            state=row["state"] or "",
            number=row["number"] or "",
            title=row["title"] or "",
            synopsis=row["synopsis"] or "",
            url=row["url"] or "",
            state_url=row["state_url"] or "",
            status=row["status"],
            status_date=row["status_date"],
            committee=row["committee"],
            change_hash=row["change_hash"] or "",
            session_id=row["session_id"],
            session_name=row["session_name"],
            last_action=row["last_action"],
            last_action_date=row["last_action_date"],
            referrals=list(_loads(row["referrals"], [])),  # type: ignore[arg-type]
            sponsors=list(_loads(row["sponsors"], [])),  # type: ignore[arg-type]
            history=history,
            hearings=self.hearings_for(row["feed"], row["bill_id"]),
        )
        return TrackedBill(
            bill=bill,
            feed=row["feed"],
            tracked=bool(row["tracked"]),
            reasons=list(_loads(row["reasons"], [])),  # type: ignore[arg-type]
            first_seen=row["first_seen"] or "",
            last_updated=row["last_updated"] or "",
        )

    def upsert_bill(
        self, feed: str, bill: Bill, *, tracked: bool, reasons: list[str], when: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO bills (
                 bill_id, feed, state, number, title, synopsis, url, state_url, status,
                 status_date, committee, change_hash, session_id, session_name, last_action,
                 last_action_date, referrals, sponsors, history, tracked, reasons,
                 first_seen, last_updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(feed, bill_id) DO UPDATE SET
                 state=excluded.state, number=excluded.number, title=excluded.title,
                 synopsis=excluded.synopsis, url=excluded.url, state_url=excluded.state_url,
                 status=excluded.status, status_date=excluded.status_date,
                 committee=excluded.committee, change_hash=excluded.change_hash,
                 session_id=excluded.session_id, session_name=excluded.session_name,
                 last_action=excluded.last_action, last_action_date=excluded.last_action_date,
                 referrals=excluded.referrals, sponsors=excluded.sponsors,
                 history=excluded.history, tracked=excluded.tracked, reasons=excluded.reasons,
                 last_updated=excluded.last_updated""",
            (
                bill.bill_id,
                feed,
                bill.state,
                bill.number,
                bill.title,
                bill.synopsis,
                bill.url,
                bill.state_url,
                bill.status,
                bill.status_date,
                bill.committee,
                bill.change_hash,
                bill.session_id,
                bill.session_name,
                bill.last_action,
                bill.last_action_date,
                _dumps(bill.referrals),
                _dumps(bill.sponsors),
                _dumps([asdict(a) for a in bill.history]),
                int(tracked),
                _dumps(reasons),
                when,
                when,
            ),
        )

    def tracked_bills(self, feed: str, *, tracked_only: bool = True) -> list[TrackedBill]:
        sql = "SELECT * FROM bills WHERE feed=?"
        if tracked_only:
            sql += " AND tracked=1"
        rows = self._conn.execute(sql + " ORDER BY number", (feed,)).fetchall()
        return [self._row_to_tracked(r) for r in rows]

    def count_bills(self, feed: str, *, tracked_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM bills WHERE feed=?"
        if tracked_only:
            sql += " AND tracked=1"
        return int(self._conn.execute(sql, (feed,)).fetchone()[0])

    # -- hearings ---------------------------------------------------------- #
    def hearings_for(self, feed: str, bill_id: int) -> list[Hearing]:
        rows = self._conn.execute(
            "SELECT * FROM hearings WHERE feed=? AND bill_id=? ORDER BY date, committee",
            (feed, bill_id),
        ).fetchall()
        return [self._row_to_hearing(r) for r in rows]

    @staticmethod
    def _row_to_hearing(r: sqlite3.Row) -> Hearing:
        return Hearing(
            bill_id=r["bill_id"],
            date=r["date"],
            committee=r["committee"],
            time=r["time"],
            location=r["location"],
            kind=r["kind"] or "Hearing",
        )

    def replace_hearings(self, feed: str, bill_id: int, hearings: Iterable[Hearing]) -> None:
        """Sync a bill's hearings: upsert present ones (keeping announced flag), drop the rest."""
        keep: list[tuple[str, str]] = []
        for h in hearings:
            keep.append((h.date, h.committee))
            self._conn.execute(
                """INSERT INTO hearings (feed, bill_id, date, committee, time, location, kind)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(feed, bill_id, date, committee) DO UPDATE SET
                     time=excluded.time, location=excluded.location, kind=excluded.kind""",
                (feed, bill_id, h.date, h.committee, h.time, h.location, h.kind),
            )
        rows = self._conn.execute(
            "SELECT date, committee FROM hearings WHERE feed=? AND bill_id=?", (feed, bill_id)
        ).fetchall()
        for r in rows:
            if (r["date"], r["committee"]) not in keep:
                self._conn.execute(
                    "DELETE FROM hearings WHERE feed=? AND bill_id=? AND date=? AND committee=?",
                    (feed, bill_id, r["date"], r["committee"]),
                )

    def upcoming_hearings(
        self, feed: str, start: str, end: str, *, unannounced_only: bool = True
    ) -> list[tuple[Hearing, TrackedBill]]:
        """Hearings for *tracked* bills with start <= date <= end (ISO dates compare lexically)."""
        sql = """SELECT h.* FROM hearings h
                 JOIN bills b ON b.feed = h.feed AND b.bill_id = h.bill_id
                 WHERE h.feed=? AND b.tracked=1 AND h.date >= ? AND h.date <= ?"""
        if unannounced_only:
            sql += " AND h.announced_in_digest = 0"
        sql += " ORDER BY h.date, h.time, b.number"
        rows = self._conn.execute(sql, (feed, start, end)).fetchall()
        out: list[tuple[Hearing, TrackedBill]] = []
        cache: dict[int, TrackedBill] = {}
        for r in rows:
            tb = cache.get(r["bill_id"])
            if tb is None:
                tb = self.get_bill(feed, r["bill_id"])
                if tb is None:  # pragma: no cover - join guarantees existence
                    continue
                cache[r["bill_id"]] = tb
            out.append((self._row_to_hearing(r), tb))
        return out

    def mark_hearings_announced(self, feed: str, hearings: Iterable[Hearing]) -> None:
        for h in hearings:
            self._conn.execute(
                """UPDATE hearings SET announced_in_digest=1
                   WHERE feed=? AND bill_id=? AND date=? AND committee=?""",
                (feed, h.bill_id, h.date, h.committee),
            )

    # -- events (pending digest items) ------------------------------------- #
    def add_event(self, feed: str, bill_id: int, kind: str, detail: dict, when: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (feed, bill_id, kind, detail, created) VALUES (?,?,?,?,?)",
            (feed, bill_id, kind, _dumps(detail), when),
        )
        return int(cur.lastrowid)

    def unsent_events(self, feed: str) -> list[DigestEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE feed=? AND sent=0 ORDER BY id", (feed,)
        ).fetchall()
        return [
            DigestEvent(
                id=r["id"],
                feed=r["feed"],
                bill_id=r["bill_id"],
                kind=r["kind"],
                detail=dict(_loads(r["detail"], {})),
                created=r["created"],  # type: ignore[arg-type]
            )
            for r in rows
        ]

    def mark_events_sent(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        self._conn.executemany("UPDATE events SET sent=1 WHERE id=?", [(i,) for i in ids])

    def mark_all_events_sent(self, feed: str) -> int:
        cur = self._conn.execute("UPDATE events SET sent=1 WHERE feed=? AND sent=0", (feed,))
        return int(cur.rowcount)

    # -- sent log ---------------------------------------------------------- #
    def log_run(
        self,
        run_date: str,
        feed: str,
        *,
        new_bills: int = 0,
        changed: int = 0,
        hearings: int = 0,
        watch: int = 0,
        skipped: bool = False,
        sent: bool = False,
        queries: int = 0,
    ) -> None:
        self._conn.execute(
            """INSERT INTO sent_log (run_date, feed, new_bills, changed, hearings, watch,
                                     skipped, sent, queries)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_date, feed) DO UPDATE SET
                 new_bills=excluded.new_bills, changed=excluded.changed,
                 hearings=excluded.hearings, watch=excluded.watch, skipped=excluded.skipped,
                 sent=excluded.sent, queries=sent_log.queries + excluded.queries""",
            (run_date, feed, new_bills, changed, hearings, watch, int(skipped), int(sent), queries),
        )

    def sent_log(self, feed: str | None = None) -> list[dict]:
        if feed:
            rows = self._conn.execute(
                "SELECT * FROM sent_log WHERE feed=? ORDER BY run_date", (feed,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM sent_log ORDER BY run_date, feed").fetchall()
        return [dict(r) for r in rows]
