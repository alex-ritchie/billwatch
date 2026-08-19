from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from billwatch.__main__ import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from billwatch.legiscan import FixtureClient, LegiScanError, TextDocument, parse_bill_text
from billwatch.models import BillText
from billwatch.pipeline import run_pipeline
from billwatch.store import Store
from billwatch.texts import (
    TextExtractionError,
    compress,
    decompress,
    extract_text,
    normalize,
    sync_texts,
)
from tests.conftest import make_bill, make_config

FEED = "md-substance-use"


def _doc(content: bytes, mime: str, doc_id: int = 1) -> TextDocument:
    return TextDocument(
        doc_id=doc_id, bill_id=1, mime=mime, date="2026-01-01", type="Introduced", content=content
    )


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


def test_parse_bill_text_decodes_base64_and_rejects_garbage():
    payload = {
        "status": "OK",
        "text": {
            "doc_id": 9,
            "bill_id": 1,
            "mime": "text/plain",
            "date": "2026-01-01",
            "type": "Introduced",
            "doc": base64.b64encode(b"hello").decode(),
        },
    }
    d = parse_bill_text(payload)
    assert d.content == b"hello" and d.doc_id == 9 and d.bill_id == 1
    with pytest.raises(LegiScanError):
        parse_bill_text({"status": "OK"})
    with pytest.raises(LegiScanError, match="base64"):
        parse_bill_text({"status": "OK", "text": {"doc_id": 1, "doc": "%%%not-base64%%%"}})


def test_extract_pdf_fixture(day1_dir):
    payload = json.loads((day1_dir / "text_6001.json").read_text())
    text = extract_text(parse_bill_text(payload))
    assert "HB101 - Introduced" in text
    assert "Opioid Overdose Prevention" in text
    assert "take effect October 1, 2026." in text


def test_extract_pdf_detected_by_magic_bytes_when_mime_missing(day1_dir):
    payload = json.loads((day1_dir / "text_6001.json").read_text())
    raw = base64.b64decode(payload["text"]["doc"])
    assert "Opioid" in extract_text(_doc(raw, ""))


def test_extract_html_and_plain():
    html = (
        b"<html><head><style>p{}</style><script>x()</script></head><body><h1>Title</h1>"
        b"<p>Section&nbsp;1 &amp; 2</p></body></html>"
    )
    out = extract_text(_doc(html, "text/html"))
    assert "Title" in out and "Section" in out and "& 2" in out and "x()" not in out
    assert extract_text(_doc(b"  plain   text \n\n\n\nmore ", "text/plain")) == "plain text\n\nmore"


def test_extract_errors():
    with pytest.raises(TextExtractionError, match="empty"):
        extract_text(_doc(b"", "application/pdf"))
    with pytest.raises(TextExtractionError, match="unsupported"):
        extract_text(_doc(b"\x00\x01", "application/octet-stream"))
    with pytest.raises(TextExtractionError, match="PDF"):
        extract_text(_doc(b"%PDF-1.4 this is not really a pdf", "application/pdf"))


def test_normalize_and_compress_roundtrip():
    assert normalize("a  b\t c \n \n\n\n d\r\n") == "a b c\n\nd"
    s = "x" * 10000 + " é"
    assert decompress(compress(s)) == s
    assert len(compress(s)) < 200


# --------------------------------------------------------------------------- #
# sync_texts
# --------------------------------------------------------------------------- #


def _bill_with_text(bill_id, doc_id, kind="Introduced", date="2026-01-14"):
    return make_bill(bill_id, f"HB {bill_id}", history=[], hearings=[]).__class__(
        **{
            **make_bill(bill_id, f"HB {bill_id}").__dict__,
            "texts": [
                BillText(
                    doc_id=doc_id,
                    type=kind,
                    date=date,
                    mime="application/pdf",
                    url="u",
                    state_url="s",
                )
            ],
        }
    )


def test_sync_texts_fetches_once_then_skips(store, day1_dir):
    client = FixtureClient(day1_dir)
    b = _bill_with_text(1001, 6001)
    res = sync_texts(store, client, [b], when="t1")
    assert (res.fetched, res.skipped, res.failed, res.queries) == (1, 0, 0, 1)
    t = store.get_text("MD", 1001)
    assert t["doc_id"] == 6001 and t["version"] == "Introduced" and t["chars"] > 100
    assert "Opioid" in t["text"]
    # second sync: already current
    res = sync_texts(store, client, [b], when="t2")
    assert (res.fetched, res.skipped, res.queries) == (0, 1, 0)
    # new version → re-fetch and replace
    b2 = _bill_with_text(1001, 16001, "Engrossed", "2026-02-12")

    client2 = FixtureClient(day1_dir.parent / "legiscan_day2")
    res = sync_texts(store, client2, [b2], when="t3")
    assert res.fetched == 1
    assert store.get_text("MD", 1001)["version"] == "Engrossed"
    assert store.text_stats()["n"] == 1


def test_sync_texts_handles_missing_doc_no_client_and_budget(store, day1_dir):
    # bill without any text version → skipped; missing fixture → failed; no client → nothing
    no_text = make_bill(1, "HB 1")
    res = sync_texts(store, FixtureClient(day1_dir), [no_text], when="t")
    assert res.skipped == 1 and res.queries == 0
    missing = _bill_with_text(1, 999999)
    res = sync_texts(store, FixtureClient(day1_dir), [missing], when="t")
    assert res.failed == 1 and store.get_text("MD", 1) is None
    assert sync_texts(store, None, [missing], when="t").queries == 0
    # budget exhausted → deferred, nothing stored
    res = sync_texts(
        store, FixtureClient(day1_dir, max_queries=0), [_bill_with_text(1001, 6001)], when="t"
    )
    assert res.deferred == 1 and store.get_text("MD", 1001) is None


def test_delete_text(store, day1_dir):
    sync_texts(store, FixtureClient(day1_dir), [_bill_with_text(1001, 6001)], when="t")
    store.delete_text("MD", 1001)
    assert store.get_text("MD", 1001) is None and store.text_stats()["n"] == 0


# --------------------------------------------------------------------------- #
# CLI: fetch-texts
# --------------------------------------------------------------------------- #


def test_cli_fetch_texts_show_and_stats(env_console, tmp_path, config_path, day1_dir, capsys):
    db = tmp_path / "bw.db"
    base = ["--env-file", ""]
    assert (
        main(
            [
                *base,
                "backfill",
                "--config",
                str(config_path),
                "--db",
                str(db),
                "--fixtures",
                str(day1_dir),
                "--today",
                "2026-01-20",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()
    # already fetched during backfill → stats shows 4
    assert (
        main([*base, "fetch-texts", "--config", str(config_path), "--db", str(db), "--stats"])
        == EXIT_OK
    )
    assert capsys.readouterr().out.startswith("4 bill text(s) stored")
    # re-sync: nothing to do
    assert (
        main(
            [
                *base,
                "fetch-texts",
                "--config",
                str(config_path),
                "--db",
                str(db),
                "--fixtures",
                str(day1_dir),
            ]
        )
        == EXIT_OK
    )
    # show one
    assert (
        main(
            [
                *base,
                "fetch-texts",
                "--config",
                str(config_path),
                "--db",
                str(db),
                "--show",
                "hb 101",
            ]
        )
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert out.startswith("# HB101 —") and "AN ACT concerning" in out
    # not tracked / no text
    assert (
        main(
            [*base, "fetch-texts", "--config", str(config_path), "--db", str(db), "--show", "SB120"]
        )
        == EXIT_USAGE
    )
    with Store(db) as s:
        s.delete_text("MD", 1001)
        s.commit()
    assert (
        main(
            [*base, "fetch-texts", "--config", str(config_path), "--db", str(db), "--show", "HB101"]
        )
        == EXIT_FAILED
    )


def test_demoted_bill_loses_its_text(store, day1_dir):
    """reevaluate: a bill demoted/removed from tracked also drops its stored text."""
    from billwatch.reevaluate import reevaluate_feed

    run_pipeline(
        config=make_config(),
        client=FixtureClient(day1_dir),
        store=store,
        today=date(2026, 2, 1),
        mailer=None,
        recipients_for=lambda f: [],
        announce=False,
    )
    assert store.get_text("MD", 1003) is not None
    cfg = make_config(keywords=["opioid", "overdose", "naloxone"], searches=["overdose"])
    reevaluate_feed(store, cfg, cfg.feed(FEED), client=FixtureClient(day1_dir))
    assert not store.get_bill(FEED, 1003).tracked
    assert store.get_text("MD", 1003) is None
