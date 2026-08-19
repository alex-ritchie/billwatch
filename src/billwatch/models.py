"""Source-agnostic data model.

These dataclasses are the internal contract between the fetch layer
(`legiscan.py`, or a future Open States mapper), the store, and the digest.
Nothing here knows about LegiScan JSON shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# LegiScan bill status codes → human labels. Kept here (not in legiscan.py)
# because the store persists the integer and the digest renders the label.
STATUS_LABELS: dict[int, str] = {
    0: "Not available",
    1: "Introduced",
    2: "Engrossed",
    3: "Enrolled",
    4: "Passed",
    5: "Vetoed",
    6: "Failed",
    7: "Veto overridden",
    8: "Chaptered",
    9: "Referred",
    10: "Reported favorably",
    11: "Reported unfavorably",
    12: "Draft",
}


def status_label(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return STATUS_LABELS.get(code, f"Status {code}")


@dataclass(frozen=True)
class Hearing:
    """A calendar entry (hearing / floor session) attached to a bill."""

    bill_id: int
    date: str  # ISO YYYY-MM-DD
    committee: str  # committee / event description ("House Finance Hearing")
    time: str | None = None
    location: str | None = None
    kind: str = "Hearing"

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.bill_id, self.date, self.committee)


@dataclass(frozen=True)
class Action:
    """One row of a bill's legislative history."""

    date: str
    action: str
    chamber: str | None = None


@dataclass
class Bill:
    """A bill as we care about it, independent of data source."""

    bill_id: int
    state: str
    number: str
    title: str
    synopsis: str
    url: str  # canonical source page (LegiScan)
    state_url: str  # the legislature's own page (MGA)
    status: int | None
    status_date: str | None
    committee: str | None
    change_hash: str
    session_id: int | None = None
    session_name: str | None = None
    last_action: str | None = None
    last_action_date: str | None = None
    referrals: list[str] = field(default_factory=list)
    sponsors: list[str] = field(default_factory=list)
    history: list[Action] = field(default_factory=list)
    hearings: list[Hearing] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return status_label(self.status)

    @property
    def committees_seen(self) -> list[str]:
        """Every committee this bill has touched (current + past referrals)."""
        seen: list[str] = []
        for name in [self.committee, *self.referrals]:
            if name and name not in seen:
                seen.append(name)
        return seen


@dataclass(frozen=True)
class MasterEntry:
    """One row of a LegiScan master list: bill fingerprint only."""

    bill_id: int
    number: str
    change_hash: str


@dataclass(frozen=True)
class SearchHit:
    bill_id: int
    change_hash: str
    relevance: int
    query: str


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    name: str
    year_start: int
    year_end: int
    special: bool = False
    prior: bool = False
    sine_die: bool = False


@dataclass
class MasterList:
    session: SessionInfo | None
    entries: dict[int, MasterEntry]


@dataclass
class StatusChange:
    """What changed on a tracked bill between two fetches."""

    old_status: int | None
    new_status: int | None
    old_committee: str | None
    new_committee: str | None
    actions: list[Action]  # history rows added since the previous fetch

    @property
    def status_changed(self) -> bool:
        return self.old_status != self.new_status

    @property
    def committee_changed(self) -> bool:
        return (self.old_committee or None) != (self.new_committee or None)

    def is_empty(self) -> bool:
        return not (self.status_changed or self.committee_changed or self.actions)


@dataclass
class TrackedBill:
    """A bill row as stored, with tracking metadata."""

    bill: Bill
    feed: str
    tracked: bool
    reasons: list[str]
    first_seen: str
    last_updated: str


@dataclass
class DigestEvent:
    """A pending (unsent) digest item persisted in the store (NFR3)."""

    id: int
    feed: str
    bill_id: int
    kind: str  # "new" | "status" | "watch"
    detail: dict
    created: str
