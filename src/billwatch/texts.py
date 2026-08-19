"""Bill full text: fetch the latest version for tracked bills and keep the extracted text.

Only *tracked* bills get text (design: precision errors are cheap, but storing 50–100 MB of
text per session in a git-committed DB is not). One LegiScan query per new text version.
Text is stored zlib-compressed; PDFs are converted with pypdf, HTML is tag-stripped.
"""

from __future__ import annotations

import html
import io
import logging
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass

from .legiscan import BillSource, LegiScanError, QuotaExceeded, TextDocument
from .models import Bill
from .store import Store

log = logging.getLogger(__name__)


class TextExtractionError(RuntimeError):
    pass


def _pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise TextExtractionError("pypdf is not installed") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf raises a zoo of exception types
        raise TextExtractionError(f"PDF extraction failed: {type(exc).__name__}") from exc
    return "\n\f".join(pages)


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def _html_to_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = _TAG_RE.sub(" ", text)
    return html.unescape(text)


def normalize(text: str) -> str:
    """Collapse whitespace noise from PDF extraction but keep paragraph/page breaks."""
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text(doc: TextDocument) -> str:
    mime = (doc.mime or "").lower()
    data = doc.content
    if not data:
        raise TextExtractionError("empty document")
    if "pdf" in mime or data[:5] == b"%PDF-":
        return normalize(_pdf_to_text(data))
    if "html" in mime or data[:15].lower().lstrip().startswith((b"<!doctype", b"<html")):
        return normalize(_html_to_text(data))
    if mime.startswith("text/") or not mime:
        return normalize(data.decode("utf-8", errors="replace"))
    raise TextExtractionError(f"unsupported mime type {mime!r}")


def compress(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 9)


def decompress(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


@dataclass
class TextSyncResult:
    fetched: int = 0
    skipped: int = 0  # already current
    failed: int = 0
    deferred: int = 0  # budget exhausted
    queries: int = 0


def sync_texts(
    store: Store, client: BillSource | None, bills: Iterable[Bill], *, when: str
) -> TextSyncResult:
    """Ensure each bill's *latest* text version is stored. Idempotent; budget-aware."""
    res = TextSyncResult()
    if client is None:
        return res
    start = client.query_count
    for bill in bills:
        latest = bill.latest_text
        if latest is None:
            res.skipped += 1
            continue
        current = store.text_doc_id(bill.state, bill.bill_id)
        if current == latest.doc_id:
            res.skipped += 1
            continue
        rem = client.remaining()
        if rem is not None and rem <= 0:
            res.deferred += 1
            continue
        try:
            doc = client.get_bill_text(latest.doc_id)
            text = extract_text(doc)
        except QuotaExceeded:
            res.deferred += 1
            continue
        except (LegiScanError, TextExtractionError) as exc:
            res.failed += 1
            log.warning(
                "[%s] text for %s (doc %s) failed: %s", bill.state, bill.number, latest.doc_id, exc
            )
            continue
        store.save_text(
            bill.state,
            bill.bill_id,
            doc_id=latest.doc_id,
            version=latest.type,
            date=latest.date,
            mime=doc.mime or latest.mime,
            source_url=latest.state_url or latest.url,
            text=text,
            when=when,
        )
        res.fetched += 1
    res.queries = client.query_count - start
    if res.fetched or res.failed or res.deferred:
        log.info(
            "texts: %d fetched, %d current, %d failed, %d deferred (%d queries)",
            res.fetched,
            res.skipped,
            res.failed,
            res.deferred,
            res.queries,
        )
    return res
