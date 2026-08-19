"""Keyword / committee matching.

Keywords match against title + synopsis, case-insensitively, at word boundaries
(so "opioid" matches "opioids" but not... well, "opioid" is a prefix of
"opioids"; we anchor the *start* at a word boundary and let plurals through
by allowing an optional trailing "s"/"es"). Multi-word keywords match across
any whitespace/hyphen run ("harm reduction" ~ "harm-reduction").

An LLM re-ranker (design §5.3) would slot in here as a second pass over the
keyword candidates; it is deliberately not built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import FeedConfig
from .models import Bill


def compile_keyword(keyword: str) -> re.Pattern[str]:
    """Word-boundary, case-insensitive pattern for a (possibly multi-word) keyword."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", keyword.strip()) if p]
    if not parts:
        raise ValueError("empty keyword")
    body = r"[\s\-]+".join(parts)
    return re.compile(rf"(?<!\w){body}(?:e?s)?(?!\w)", re.IGNORECASE)


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


@dataclass
class MatchResult:
    keyword_hits: list[str] = field(default_factory=list)
    excluded_by: list[str] = field(default_factory=list)
    watch_hits: list[str] = field(default_factory=list)
    search_hits: list[str] = field(default_factory=list)  # search queries that surfaced it
    crossfile_of: list[str] = field(default_factory=list)  # matched bills this is cross-filed with

    @property
    def excluded(self) -> bool:
        return bool(self.excluded_by)

    @property
    def matched(self) -> bool:
        """Should this bill be tracked (New bills / Movement sections)?"""
        return not self.excluded and bool(
            self.keyword_hits or self.search_hits or self.crossfile_of
        )

    @property
    def watch_only(self) -> bool:
        """Not a keyword/search match, but referred to a watched committee."""
        return not self.excluded and not self.matched and bool(self.watch_hits)

    @property
    def reasons(self) -> list[str]:
        """Human-readable match reasons; committee reasons only when that is the sole signal."""
        out = [f"keyword: {k}" for k in self.keyword_hits]
        out += [f"search: {q}" for q in self.search_hits]
        out += [f"crossfile: {n}" for n in self.crossfile_of]
        if not out:
            out += [f"committee: {c}" for c in self.watch_hits]
        return out


class FeedFilter:
    """Compiled matcher for one feed's rules."""

    def __init__(self, feed: FeedConfig) -> None:
        self.feed = feed
        self._keywords = [(k, compile_keyword(k)) for k in feed.keywords]
        self._excludes = [(k, compile_keyword(k)) for k in feed.exclude_keywords]
        self._watch = {_norm(c): c for c in feed.watch_committees}

    def evaluate(
        self,
        bill: Bill,
        search_hits: list[str] | None = None,
        crossfile_of: list[str] | None = None,
    ) -> MatchResult:
        """crossfile_of: numbers of already-matched bills this one is cross-filed with; a
        cross-file of a match is itself a match (it is the same bill in the other chamber)."""
        text = f"{bill.title}\n{bill.synopsis}"
        result = MatchResult(
            search_hits=list(search_hits or []), crossfile_of=list(crossfile_of or [])
        )
        result.keyword_hits = [k for k, pat in self._keywords if pat.search(text)]
        result.excluded_by = [k for k, pat in self._excludes if pat.search(text)]
        for name in bill.committees_seen:
            n = _norm(name)
            for wn, orig in self._watch.items():
                if (wn == n or wn in n) and orig not in result.watch_hits:
                    result.watch_hits.append(orig)
        return result
