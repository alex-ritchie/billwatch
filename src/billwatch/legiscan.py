"""Thin LegiScan Pull API client + JSON → model mappers.

Only three operations are needed (design §3):
  getSessionList(state)      – pick the current regular session (1 query/run/state)
  getMasterListRaw(state|id) – every bill's change_hash (1 query/run/state)
  getBill(id)                – full detail, only for changed bills
  getSearchRaw(state, query) – full-text safety net

Every request is counted so quota use (NFR4) can be logged and capped.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

import requests

from .models import (
    Action,
    Bill,
    Hearing,
    MasterEntry,
    MasterList,
    SearchHit,
    SessionInfo,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.legiscan.com/"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LegiScanError(RuntimeError):
    """Non-retryable API failure (bad key, unknown id, ...)."""


class QuotaExceeded(LegiScanError):
    """The per-run query budget would be exceeded."""


class TransportError(LegiScanError):
    """Retries exhausted against the network / server."""


class BillSource(Protocol):
    """What the pipeline needs from a data source (LegiScan today, Open States later)."""

    def get_session_list(self, state: str) -> list[SessionInfo]: ...
    def get_master_list(self, state: str, session_id: int | None = None) -> MasterList: ...
    def get_bill(self, bill_id: int) -> Bill: ...
    def search(self, state: str, query: str, session_id: int | None = None) -> list[SearchHit]: ...
    def remaining(self) -> int | None: ...
    @property
    def query_count(self) -> int: ...


# --------------------------------------------------------------------------- #
# Mappers: LegiScan JSON → models (pure functions, unit-testable)
# --------------------------------------------------------------------------- #


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def parse_session(raw: dict) -> SessionInfo:
    return SessionInfo(
        session_id=int(raw["session_id"]),
        name=_s(raw.get("session_name") or raw.get("session_title") or raw.get("name")),
        year_start=int(raw.get("year_start") or 0),
        year_end=int(raw.get("year_end") or 0),
        special=bool(int(raw.get("special") or 0)),
        prior=bool(int(raw.get("prior") or 0)),
        sine_die=bool(int(raw.get("sine_die") or 0)),
    )


def pick_current_session(sessions: Iterable[SessionInfo]) -> SessionInfo | None:
    """Choose the session to track: newest non-prior regular session, else newest."""
    sessions = list(sessions)
    if not sessions:
        return None
    regular = [s for s in sessions if not s.special and not s.prior]
    pool = regular or [s for s in sessions if not s.prior] or sessions
    return max(pool, key=lambda s: (s.year_start, s.session_id))


def parse_master_list(payload: dict) -> MasterList:
    """getMasterListRaw → MasterList. The API returns a dict keyed "0","1",... plus "session"."""
    master = payload.get("masterlist")
    if not isinstance(master, dict):
        raise LegiScanError("malformed masterlist response: missing 'masterlist'")
    session = None
    entries: dict[int, MasterEntry] = {}
    for key, val in master.items():
        if key == "session":
            if isinstance(val, dict) and "session_id" in val:
                session = parse_session(val)
            continue
        if not isinstance(val, dict) or "bill_id" not in val:
            continue
        bill_id = int(val["bill_id"])
        entries[bill_id] = MasterEntry(
            bill_id=bill_id,
            number=_s(val.get("number") or val.get("bill_number")),
            change_hash=_s(val.get("change_hash")),
        )
    return MasterList(session=session, entries=entries)


def parse_search_results(payload: dict, query: str) -> list[SearchHit]:
    """getSearchRaw → hits. Tolerates both the raw (list) and paged (dict) shapes."""
    sr = payload.get("searchresult")
    if not isinstance(sr, dict):
        raise LegiScanError("malformed search response: missing 'searchresult'")
    results = sr.get("results")
    if results is None:  # getSearch shape: numeric keys + "summary"
        results = [v for k, v in sr.items() if k != "summary" and isinstance(v, dict)]
    hits: list[SearchHit] = []
    for r in results:
        if not isinstance(r, dict) or "bill_id" not in r:
            continue
        rel = r.get("relevance", 0)
        try:
            rel_i = int(str(rel).rstrip("%"))
        except ValueError:
            rel_i = 0
        hits.append(
            SearchHit(
                bill_id=int(r["bill_id"]),
                change_hash=_s(r.get("change_hash")),
                relevance=rel_i,
                query=query,
            )
        )
    return hits


_HEARING_SUFFIX_RE = re.compile(r"\s+(hearing|meeting|session)\s*$", re.IGNORECASE)


def _committee_name(raw: Any) -> str | None:
    """LegiScan's `committee` is a dict when pending, and `[]` (!) when not."""
    if isinstance(raw, dict):
        name = raw.get("name")
        return str(name) if name else None
    return None


def parse_bill(payload: dict) -> Bill:
    """getBill → Bill (with hearings, history, referrals)."""
    b = payload.get("bill")
    if not isinstance(b, dict) or "bill_id" not in b:
        raise LegiScanError("malformed bill response: missing 'bill'")
    bill_id = int(b["bill_id"])

    session = b.get("session") if isinstance(b.get("session"), dict) else {}
    session_id = b.get("session_id") or session.get("session_id")
    session_name = session.get("session_name") or session.get("session_title")

    history: list[Action] = []
    for h in b.get("history") or []:
        if isinstance(h, dict) and h.get("action"):
            history.append(
                Action(date=_s(h.get("date")), action=_s(h.get("action")), chamber=h.get("chamber"))
            )
    # LegiScan history is oldest → newest; keep that order but be robust to reversal.
    history.sort(key=lambda a: a.date)

    referrals: list[str] = []
    for r in b.get("referrals") or []:
        if isinstance(r, dict) and r.get("name") and r["name"] not in referrals:
            referrals.append(str(r["name"]))

    sponsors = [
        _s(s.get("name"))
        for s in (b.get("sponsors") or [])
        if isinstance(s, dict) and s.get("name")
    ]

    committee = _committee_name(b.get("committee"))
    hearings: list[Hearing] = []
    for c in b.get("calendar") or []:
        if not isinstance(c, dict) or not c.get("date"):
            continue
        desc = _s(c.get("description")).strip()
        kind = _s(c.get("type") or "Hearing").strip() or "Hearing"
        name = _HEARING_SUFFIX_RE.sub("", desc) if desc else (committee or kind)
        hearings.append(
            Hearing(
                bill_id=bill_id,
                date=_s(c.get("date")),
                committee=name or kind,
                time=(_s(c.get("time")).strip() or None),
                location=(_s(c.get("location")).strip() or None),
                kind=kind,
            )
        )

    last = history[-1] if history else None
    status_raw = b.get("status")
    try:
        status = int(status_raw) if status_raw not in (None, "") else None
    except (TypeError, ValueError):
        status = None

    return Bill(
        bill_id=bill_id,
        state=_s(b.get("state")).upper(),
        number=_s(b.get("bill_number") or b.get("number")),
        title=_s(b.get("title")).strip(),
        synopsis=_s(b.get("description")).strip(),
        url=_s(b.get("url")),
        state_url=_s(b.get("state_link")),
        status=status,
        status_date=(_s(b.get("status_date")) or None),
        committee=committee,
        change_hash=_s(b.get("change_hash")),
        session_id=int(session_id) if session_id else None,
        session_name=str(session_name) if session_name else None,
        last_action=last.action if last else None,
        last_action_date=last.date if last else None,
        referrals=referrals,
        sponsors=sponsors,
        history=history,
        hearings=hearings,
    )


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


class LegiScanClient:
    """Small, retrying, quota-counting client for the LegiScan Pull API.

    The API key never appears in logs: request URLs are logged with the key redacted.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        timeout: float = 30.0,
        max_queries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LegiScanError("LEGISCAN_API_KEY is not set")
        self._key = api_key
        self._base = base_url
        self._http = session or requests.Session()
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff_seconds
        self._timeout = timeout
        self._max_queries = max_queries
        self._sleep = sleep
        self._count = 0

    # -- quota ------------------------------------------------------------ #
    @property
    def query_count(self) -> int:
        return self._count

    def remaining(self) -> int | None:
        if self._max_queries is None:
            return None
        return max(0, self._max_queries - self._count)

    # -- core request ------------------------------------------------------ #
    def _request(self, op: str, **params: Any) -> dict:
        if self._max_queries is not None and self._count >= self._max_queries:
            raise QuotaExceeded(f"per-run query budget of {self._max_queries} exhausted")
        query = {"key": self._key, "op": op, **{k: v for k, v in params.items() if v is not None}}
        self._count += 1
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._http.get(self._base, params=query, timeout=self._timeout)
            except requests.RequestException as exc:  # network-level failure
                last_exc = exc
                log.warning(
                    "LegiScan %s attempt %d/%d failed: %s",
                    op,
                    attempt,
                    self._max_attempts,
                    type(exc).__name__,
                )
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    last_exc = TransportError(f"HTTP {resp.status_code} from LegiScan ({op})")
                    log.warning(
                        "LegiScan %s attempt %d/%d: HTTP %s",
                        op,
                        attempt,
                        self._max_attempts,
                        resp.status_code,
                    )
                elif resp.status_code != 200:
                    raise LegiScanError(f"HTTP {resp.status_code} from LegiScan ({op})")
                else:
                    try:
                        payload = resp.json()
                    except ValueError:
                        last_exc = TransportError(f"non-JSON response from LegiScan ({op})")
                        log.warning(
                            "LegiScan %s attempt %d/%d: non-JSON body",
                            op,
                            attempt,
                            self._max_attempts,
                        )
                    else:
                        if not isinstance(payload, dict):
                            raise LegiScanError(f"unexpected payload type for {op}")
                        if payload.get("status") == "ERROR":
                            alert = payload.get("alert") or {}
                            msg = alert.get("message") if isinstance(alert, dict) else str(alert)
                            raise LegiScanError(f"LegiScan {op} error: {msg or 'unknown'}")
                        return payload
            if attempt < self._max_attempts:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
        raise TransportError(
            f"LegiScan {op} failed after {self._max_attempts} attempts: {last_exc}"
        )

    # -- operations -------------------------------------------------------- #
    def get_session_list(self, state: str) -> list[SessionInfo]:
        payload = self._request("getSessionList", state=state)
        raw = payload.get("sessions") or []
        return [parse_session(s) for s in raw if isinstance(s, dict) and "session_id" in s]

    def get_master_list(self, state: str, session_id: int | None = None) -> MasterList:
        if session_id is not None:
            payload = self._request("getMasterListRaw", id=session_id)
        else:
            payload = self._request("getMasterListRaw", state=state)
        return parse_master_list(payload)

    def get_bill(self, bill_id: int) -> Bill:
        return parse_bill(self._request("getBill", id=bill_id))

    def search(self, state: str, query: str, session_id: int | None = None) -> list[SearchHit]:
        if session_id is not None:
            payload = self._request("getSearchRaw", id=session_id, query=query)
        else:
            payload = self._request("getSearchRaw", state=state, query=query, year=2)
        return parse_search_results(payload, query)


# --------------------------------------------------------------------------- #
# Offline fixture client (CI dry-runs, tests) — same interface, no network
# --------------------------------------------------------------------------- #


def search_slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")


class FixtureClient:
    """Serves recorded LegiScan responses from a directory.

    Layout:
      sessions_<STATE>.json          getSessionList payload   (optional)
      masterlist_<STATE>.json        getMasterListRaw payload
      bill_<ID>.json                 getBill payload
      search_<STATE>_<slug>.json     getSearchRaw payload     (optional)
    """

    def __init__(self, directory: str | Path, *, max_queries: int | None = None) -> None:
        self.dir = Path(directory)
        if not self.dir.is_dir():
            raise LegiScanError(f"fixture directory not found: {self.dir}")
        self._count = 0
        self._max_queries = max_queries

    @property
    def query_count(self) -> int:
        return self._count

    def remaining(self) -> int | None:
        if self._max_queries is None:
            return None
        return max(0, self._max_queries - self._count)

    def _load(self, name: str, *, optional: bool = False) -> dict:
        if self._max_queries is not None and self._count >= self._max_queries:
            raise QuotaExceeded(f"per-run query budget of {self._max_queries} exhausted")
        self._count += 1
        path = self.dir / name
        if not path.is_file():
            if optional:
                return {}
            raise LegiScanError(f"fixture not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") == "ERROR":
            raise LegiScanError(f"fixture {name} is an ERROR payload")
        return payload

    def get_session_list(self, state: str) -> list[SessionInfo]:
        payload = self._load(f"sessions_{state}.json", optional=True)
        return [parse_session(s) for s in payload.get("sessions") or []]

    def get_master_list(self, state: str, session_id: int | None = None) -> MasterList:
        return parse_master_list(self._load(f"masterlist_{state}.json"))

    def get_bill(self, bill_id: int) -> Bill:
        return parse_bill(self._load(f"bill_{bill_id}.json"))

    def search(self, state: str, query: str, session_id: int | None = None) -> list[SearchHit]:
        payload = self._load(f"search_{state}_{search_slug(query)}.json", optional=True)
        if not payload:
            return []
        return parse_search_results(payload, query)
