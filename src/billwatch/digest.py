"""Assemble digest content from the store and render it (HTML + plain text)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config, FeedConfig
from .models import Action, Bill, DigestEvent, Hearing, status_label
from .store import Store

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

# Legislature site labels for the "state page" link; unknown states fall back to "State page".
STATE_SITE_LABELS: dict[str, str] = {
    "MD": "MGA",
    "US": "Congress.gov",
    "VA": "LIS",
    "DC": "LIMS",
    "PA": "PA Legislature",
    "CA": "Leginfo",
    "NY": "NY Senate",
}


@dataclass
class NewBillItem:
    bill: Bill
    reasons: list[str]
    event_id: int
    partners: list[Bill] = field(default_factory=list)  # cross-filed companions, shown together

    @property
    def bills(self) -> list[Bill]:
        return [self.bill, *self.partners]

    @property
    def numbers(self) -> str:
        return " / ".join(b.number for b in self.bills)


@dataclass
class MovementItem:
    bill: Bill
    old_status: int | None
    new_status: int | None
    old_committee: str | None
    new_committee: str | None
    actions: list[Action]
    event_id: int
    partners: list[str] = field(default_factory=list)  # numbers of tracked cross-files

    @property
    def status_changed(self) -> bool:
        return self.old_status != self.new_status

    @property
    def committee_changed(self) -> bool:
        return (self.old_committee or None) != (self.new_committee or None)

    @property
    def old_status_label(self) -> str:
        return status_label(self.old_status)

    @property
    def new_status_label(self) -> str:
        return status_label(self.new_status)


@dataclass
class HearingItem:
    bill: Bill
    hearing: Hearing
    partners: list[str] = field(default_factory=list)


@dataclass
class WatchItem:
    bill: Bill
    reasons: list[str]
    event_id: int


@dataclass
class Digest:
    feed: FeedConfig
    run_date: str
    lookahead_days: int
    new_bills: list[NewBillItem] = field(default_factory=list)
    movement: list[MovementItem] = field(default_factory=list)
    hearings: list[HearingItem] = field(default_factory=list)
    watch: list[WatchItem] = field(default_factory=list)
    tracked_count: int = 0
    session_name: str | None = None
    unsubscribe_note: str = ""
    subject_prefix: str = "[billwatch]"
    # Every pending event consumed while building this digest — including duplicates
    # that were merged/folded into another item — so all of them get marked sent.
    consumed_event_ids: list[int] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.new_bills or self.movement or self.hearings or self.watch)

    @property
    def event_ids(self) -> list[int]:
        return list(self.consumed_event_ids)

    @property
    def summary(self) -> str:
        parts = []
        if self.new_bills:
            parts.append(f"{len(self.new_bills)} new")
        if self.movement:
            parts.append(f"{len(self.movement)} moved")
        if self.hearings:
            parts.append(f"{len(self.hearings)} hearing{'s' if len(self.hearings) != 1 else ''}")
        if self.watch:
            parts.append(f"{len(self.watch)} to review")
        return ", ".join(parts) if parts else "no changes"

    @property
    def subject(self) -> str:
        prefix = f"{self.subject_prefix} " if self.subject_prefix else ""
        return f"{prefix}{self.feed.display_title} — {self.run_date}: {self.summary}"


def build_digest(store: Store, config: Config, feed: FeedConfig, today: date) -> Digest:
    """Collect all unsent events + unannounced upcoming hearings for a feed."""
    lookahead = config.lookahead_days(feed)
    digest = Digest(
        feed=feed,
        run_date=today.isoformat(),
        lookahead_days=lookahead,
        unsubscribe_note=config.settings.unsubscribe_note,
        subject_prefix=config.settings.subject_prefix,
    )
    events: list[DigestEvent] = store.unsent_events(feed.name)
    # If a bill has several pending events (e.g. missed sends), show it once per kind,
    # newest wins for "status" (its detail already spans all missed actions).
    seen_kind: dict[tuple[str, int], int] = {}
    for ev in events:
        digest.consumed_event_ids.append(ev.id)
        tb = store.get_bill(feed.name, ev.bill_id)
        if tb is None:  # orphan event (bill row gone): consume silently
            continue
        b = tb.bill
        if digest.session_name is None and b.session_name:
            digest.session_name = b.session_name
        key = (ev.kind, ev.bill_id)
        if ev.kind == "new":
            if key in seen_kind:
                continue
            seen_kind[key] = ev.id
            digest.new_bills.append(NewBillItem(bill=b, reasons=tb.reasons, event_id=ev.id))
        elif ev.kind == "status":
            actions = [Action(**a) for a in ev.detail.get("actions", [])]
            item = MovementItem(
                bill=b,
                old_status=ev.detail.get("old_status"),
                new_status=ev.detail.get("new_status"),
                old_committee=ev.detail.get("old_committee"),
                new_committee=ev.detail.get("new_committee"),
                actions=actions,
                event_id=ev.id,
            )
            if key in seen_kind:
                # merge: keep earliest old_* and union of actions
                prev = next(m for m in digest.movement if m.bill.bill_id == ev.bill_id)
                prev.new_status, prev.new_committee = item.new_status, item.new_committee
                known = {(a.date, a.action) for a in prev.actions}
                prev.actions.extend(a for a in actions if (a.date, a.action) not in known)
                continue
            seen_kind[key] = ev.id
            digest.movement.append(item)
        elif ev.kind == "watch":
            if key in seen_kind:
                continue
            seen_kind[key] = ev.id
            digest.watch.append(WatchItem(bill=b, reasons=tb.reasons, event_id=ev.id))

    # Bills that are both new and moved this run: fold movement into the new-bill entry.
    new_ids = {i.bill.bill_id for i in digest.new_bills}
    digest.movement = [m for m in digest.movement if m.bill.bill_id not in new_ids]

    # Cross-filed pairs that are both new: show once, as "HB 101 / SB 101".
    by_id = {i.bill.bill_id: i for i in digest.new_bills}
    merged: set[int] = set()
    for item in list(digest.new_bills):
        if item.bill.bill_id in merged:
            continue
        for rel in item.bill.crossfiles:
            partner = by_id.get(rel.bill_id)
            if partner is not None and partner.bill.bill_id not in merged:
                item.partners.append(partner.bill)
                for r in partner.reasons:
                    if r not in item.reasons and not r.startswith("crossfile"):
                        item.reasons.append(r)
                merged.add(partner.bill.bill_id)
    digest.new_bills = [i for i in digest.new_bills if i.bill.bill_id not in merged]

    def tracked_partners(b: Bill) -> list[str]:
        out = []
        for rel in b.crossfiles:
            tb = store.get_bill(feed.name, rel.bill_id)
            if tb is not None and tb.tracked:
                out.append(tb.bill.number)
        return out

    for m in digest.movement:
        m.partners = tracked_partners(m.bill)

    end = today + timedelta(days=lookahead)
    for hearing, tb in store.upcoming_hearings(feed.name, today.isoformat(), end.isoformat()):
        digest.hearings.append(
            HearingItem(bill=tb.bill, hearing=hearing, partners=tracked_partners(tb.bill))
        )
        if digest.session_name is None and tb.bill.session_name:
            digest.session_name = tb.bill.session_name

    digest.tracked_count = store.count_bills(feed.name)
    return digest


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _env(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"], default_for_string=False),
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def _context(digest: Digest) -> dict:
    return {
        "subject": digest.subject,
        "feed_title": digest.feed.display_title,
        "run_date": digest.run_date,
        "session_name": digest.session_name,
        "summary": digest.summary,
        "is_empty": digest.is_empty,
        "new_bills": digest.new_bills,
        "movement": digest.movement,
        "hearings": digest.hearings,
        "watch": digest.watch,
        "lookahead_days": digest.lookahead_days,
        "tracked_count": digest.tracked_count,
        "unsubscribe_note": digest.unsubscribe_note,
        "state_site_label": STATE_SITE_LABELS.get(digest.feed.state, "State page"),
    }


def render_html(digest: Digest, template_dir: Path = TEMPLATE_DIR) -> str:
    env = _env(template_dir)
    return env.get_template("digest.html.j2").render(**_context(digest))


def render_text(digest: Digest, template_dir: Path = TEMPLATE_DIR) -> str:
    env = _env(template_dir)
    env.autoescape = False
    tmpl = env.get_template("digest.txt.j2")
    text = tmpl.render(**_context(digest))
    # collapse runs of blank lines left by template control blocks
    lines, out, blank = text.splitlines(), [], 0
    for line in lines:
        if line.strip():
            blank = 0
            out.append(line.rstrip())
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip() + "\n"


def build_email(digest: Digest, sender: str, *, template_dir: Path = TEMPLATE_DIR) -> EmailMessage:
    """multipart/alternative message. Recipients are NOT set here (BCC at send time)."""
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = sender
    msg["To"] = sender  # visible To is the sender; real recipients go as BCC envelope only
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="billwatch")
    msg["X-Billwatch-Feed"] = digest.feed.name
    msg.set_content(render_text(digest, template_dir))
    msg.add_alternative(render_html(digest, template_dir), subtype="html")
    return msg
