"""End-of-session summary: every tracked bill of a session, grouped by outcome.

Unlike the daily digest (which reports *changes* since the last run), the summary is a
snapshot of the whole session — sent once, typically after sine die, or on demand as a
"first taste" for a new recipient.

Outcome buckets are derived from the LegiScan status code plus the last action text,
because some states (Maryland included) leave a signed bill at status 4 "Passed" and put
the enactment in the action history ("Approved by the Governor - Chapter 12").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .config import Config, FeedConfig
from .models import Bill
from .store import Store

# Section order = rank order. key → (emoji, heading, subtitle)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "enacted": ("✅", "Became law", ""),
    "vetoed": ("🚫", "Vetoed", ""),
    "passed": ("📬", "Passed the legislature", "Awaiting the Governor's action."),
    "stalled": (
        "🛑",
        "Did not advance",
        "Died in committee or on the floor when the session ended.",
    ),
}

_ENACTED_RE = re.compile(
    r"approved by the governor|enacted under|became law|chapter\s+\d+", re.IGNORECASE
)
_VETO_RE = re.compile(r"\bvetoed\b", re.IGNORECASE)


def categorize(bill: Bill) -> str:
    """Map a bill's current state to an outcome bucket key."""
    action = bill.last_action or ""
    if bill.status == 5 or (_VETO_RE.search(action) and bill.status != 7):
        return "vetoed"
    if bill.status in (7, 8) or _ENACTED_RE.search(action):
        return "enacted"
    if bill.status in (3, 4):
        return "passed"
    return "stalled"  # introduced/engrossed/referred/failed — did not complete the process


_RANK = {k: i for i, k in enumerate(CATEGORIES)}


def bill_sort_key(number: str) -> tuple[str, int, str]:
    m = re.match(r"([A-Za-z]+)\s*(\d+)(.*)", number or "")
    if not m:
        return (number, 0, "")
    return (m.group(1).upper(), int(m.group(2)), m.group(3))


@dataclass
class SummaryEntry:
    """One line of the summary: a bill, or a cross-filed pair shown together."""

    bills: list[Bill]  # 1 or 2 (cross-filed pair), primary first
    category: str  # best outcome of the pair

    @property
    def primary(self) -> Bill:
        return self.bills[0]

    @property
    def numbers(self) -> str:
        return " / ".join(b.number for b in self.bills)

    @property
    def is_pair(self) -> bool:
        return len(self.bills) > 1

    @property
    def mixed_outcome(self) -> bool:
        return self.is_pair and len({categorize(b) for b in self.bills}) > 1


@dataclass
class SummarySection:
    key: str
    entries: list[SummaryEntry] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return CATEGORIES[self.key][0]

    @property
    def heading(self) -> str:
        return CATEGORIES[self.key][1]

    @property
    def subtitle(self) -> str:
        return CATEGORIES[self.key][2]


@dataclass
class SessionSummary:
    feed: FeedConfig
    generated: str  # ISO date
    session_name: str | None
    sections: list[SummarySection]
    bill_count: int  # tracked bills (pairs count as 2)
    subject_prefix: str = "[billwatch]"
    unsubscribe_note: str = ""

    @property
    def entry_count(self) -> int:
        return sum(len(s.entries) for s in self.sections)

    @property
    def counts_line(self) -> str:
        parts = [f"{len(s.entries)} {s.heading.lower()}" for s in self.sections if s.entries]
        return " · ".join(parts) if parts else "no matching bills"

    @property
    def subject(self) -> str:
        prefix = f"{self.subject_prefix} " if self.subject_prefix else ""
        session = f"{self.session_name} — " if self.session_name else ""
        return f"{prefix}{session}session summary: {self.feed.display_title}"

    @property
    def is_empty(self) -> bool:
        return self.entry_count == 0


def build_summary(
    store: Store, config: Config, feed: FeedConfig, today: date, *, session_id: int | None = None
) -> SessionSummary:
    tracked = store.tracked_bills(feed.name)
    if session_id is not None:
        tracked = [tb for tb in tracked if tb.bill.session_id == session_id]
    by_id = {tb.bill.bill_id: tb.bill for tb in tracked}

    # Pair cross-filed bills (both tracked) into one entry; keep the more-advanced one first.
    entries: list[SummaryEntry] = []
    seen: set[int] = set()
    for bid in sorted(by_id, key=lambda i: bill_sort_key(by_id[i].number)):
        if bid in seen:
            continue
        bill = by_id[bid]
        group = [bill]
        seen.add(bid)
        for rel in bill.crossfiles:
            partner = by_id.get(rel.bill_id)
            if partner is not None and rel.bill_id not in seen:
                group.append(partner)
                seen.add(rel.bill_id)
        group.sort(key=lambda b: (_RANK[categorize(b)], bill_sort_key(b.number)))
        entries.append(SummaryEntry(bills=group, category=categorize(group[0])))

    sections = [SummarySection(key=k) for k in CATEGORIES]
    by_key = {s.key: s for s in sections}
    for e in entries:
        by_key[e.category].entries.append(e)
    for s in sections:
        s.entries.sort(key=lambda e: bill_sort_key(e.primary.number))

    session_name = next((b.session_name for b in by_id.values() if b.session_name), None)
    return SessionSummary(
        feed=feed,
        generated=today.isoformat(),
        session_name=session_name,
        sections=[s for s in sections if s.entries],
        bill_count=len(by_id),
        subject_prefix=config.settings.subject_prefix,
        unsubscribe_note=config.settings.unsubscribe_note,
    )
