"""Generate the recorded LegiScan-shaped fixtures used by tests and CI dry-runs.

Run:  uv run python tests/fixtures/make_fixtures.py
Shapes mirror the LegiScan Pull API (getSessionList / getMasterListRaw / getBill /
getSearchRaw). All data is synthetic public-style legislative data — no PII.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAY1 = HERE / "legiscan"
DAY2 = HERE / "legiscan_day2"

SESSION = {
    "session_id": 2200,
    "state_id": 21,
    "year_start": 2026,
    "year_end": 2026,
    "prefile": 0,
    "sine_die": 0,
    "prior": 0,
    "special": 0,
    "session_tag": "Regular Session",
    "session_title": "2026 Regular Session",
    "session_name": "2026 Regular Session",
}
OLD_SESSION = {
    **SESSION,
    "session_id": 2100,
    "year_start": 2025,
    "year_end": 2025,
    "prior": 1,
    "sine_die": 1,
    "session_title": "2025 Regular Session",
    "session_name": "2025 Regular Session",
}
SPECIAL = {
    **SESSION,
    "session_id": 2201,
    "special": 1,
    "session_tag": "Special Session",
    "session_title": "2026 1st Special Session",
    "session_name": "2026 1st Special Session",
}

HGO = {
    "committee_id": 1,
    "chamber": "H",
    "chamber_id": 1,
    "name": "Health and Government Operations",
}
FIN = {"committee_id": 2, "chamber": "S", "chamber_id": 2, "name": "Finance"}
BT = {"committee_id": 3, "chamber": "S", "chamber_id": 2, "name": "Budget and Taxation"}
JUD = {"committee_id": 4, "chamber": "H", "chamber_id": 1, "name": "Judiciary"}
ENV = {"committee_id": 5, "chamber": "H", "chamber_id": 1, "name": "Environment and Transportation"}


def h(*parts: object) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()


def bill(
    bill_id,
    number,
    title,
    desc,
    committee,
    *,
    status=1,
    status_date="2026-01-14",
    history=None,
    calendar=None,
    referrals=None,
    sponsors=None,
    extra_hash="",
    sasts=None,
    texts=None,
):
    body = "H" if number.startswith("H") else "S"
    history = history or [
        {
            "date": status_date,
            "action": f"First Reading {committee['name'] if committee else ''}".strip(),
            "chamber": body,
            "chamber_id": 1 if body == "H" else 2,
            "importance": 1,
        },
    ]
    referrals = referrals if referrals is not None else ([committee] if committee else [])
    return {
        "status": "OK",
        "bill": {
            "bill_id": bill_id,
            "change_hash": h(bill_id, title, status, len(history), len(calendar or []), extra_hash),
            "session_id": SESSION["session_id"],
            "session": {
                k: SESSION[k]
                for k in (
                    "session_id",
                    "state_id",
                    "year_start",
                    "year_end",
                    "prefile",
                    "sine_die",
                    "prior",
                    "special",
                    "session_tag",
                    "session_title",
                    "session_name",
                )
            },
            "url": f"https://legiscan.com/MD/bill/{number.replace(' ', '')}/2026",
            "state_link": f"https://mgaleg.maryland.gov/mgawebsite/Legislation/Details/{number.replace(' ', '').lower()}?ys=2026RS",
            "completed": 0,
            "status": status,
            "status_date": status_date,
            "progress": [{"date": status_date, "event": status}],
            "state": "MD",
            "state_id": 21,
            "bill_number": number.replace(" ", ""),
            "bill_type": "B",
            "bill_type_id": 1,
            "body": body,
            "body_id": 1 if body == "H" else 2,
            "current_body": body,
            "current_body_id": 1 if body == "H" else 2,
            "title": title,
            "description": desc,
            "pending_committee_id": committee["committee_id"] if committee else 0,
            "committee": committee if committee else [],  # LegiScan returns [] when none pending
            "referrals": [{"date": status_date, **c} for c in referrals],
            "history": history,
            "sponsors": sponsors
            or [
                {
                    "people_id": 900 + bill_id % 7,
                    "party": "D",
                    "role": "Rep" if body == "H" else "Sen",
                    "name": f"Delegate {['Ames', 'Baker', 'Cole', 'Diaz', 'Evans', 'Ford', 'Gray'][bill_id % 7]}",
                    "sponsor_type_id": 1,
                    "sponsor_order": 1,
                },
            ],
            "sasts": sasts or [],
            "subjects": [],
            "texts": texts or [text_doc(bill_id, number, status_date)],
            "votes": [],
            "amendments": [],
            "supplements": [],
            "calendar": calendar or [],
        },
    }


def text_doc(bill_id, number, date, kind="Introduced", suffix=""):
    """A `texts[]` entry. doc_id = 5000 + bill_id (+ 10000 per later version)."""
    offset = {"Introduced": 0, "Engrossed": 10000, "Enrolled": 20000, "Chaptered": 30000}[kind]
    doc_id = 5000 + bill_id + offset
    num = number.replace(" ", "")
    return {
        "doc_id": doc_id,
        "date": date,
        "type": kind,
        "type_id": 1 + offset // 10000,
        "mime": "application/pdf",
        "mime_id": 2,
        "url": f"https://legiscan.com/MD/text/{num}/id/{doc_id}",
        "state_link": f"https://mgaleg.maryland.gov/2026RS/bills/{num[:2].lower()}/{num.lower()}{suffix or 'f'}.pdf",
        "text_size": 12345 + offset,
    }


def crossfile(bill_id, number):
    return {
        "type_id": 5,
        "type": "Crossfiled",
        "sast_bill_number": number.replace(" ", ""),
        "sast_bill_id": bill_id,
    }


def _pdf_escape(line):
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def tiny_pdf(lines):
    """A minimal but valid single-page PDF whose text pypdf can extract."""
    content = (
        "BT /F1 11 Tf 40 760 Td 14 TL "
        + " ".join(f"({_pdf_escape(ln)}) Tj T*" for ln in lines)
        + " ET"
    )
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def text_payload(b, tdoc):
    """getBillText payload for one text version of a bill (base64 PDF, like LegiScan)."""
    import base64

    bb = b["bill"]
    body = [
        f"{bb['bill_number']} - {tdoc['type']} ({tdoc['date']})",
        bb["title"],
        "",
        "AN ACT concerning",
        bb["description"],
        "",
        "BY repealing and reenacting, with amendments, the Annotated Code of Maryland.",
        "SECTION 1. BE IT ENACTED BY THE GENERAL ASSEMBLY OF MARYLAND, That this Act shall",
        "take effect October 1, 2026.",
    ]
    pdf = tiny_pdf(body)
    return {
        "status": "OK",
        "text": {
            "doc_id": tdoc["doc_id"],
            "bill_id": bb["bill_id"],
            "date": tdoc["date"],
            "type": tdoc["type"],
            "type_id": tdoc["type_id"],
            "mime": "application/pdf",
            "mime_id": 2,
            "text_size": len(pdf),
            "text_hash": "",
            "doc": base64.b64encode(pdf).decode(),
        },
    }


def hearing(date, desc, time="13:00", location="Room 241, House Office Building"):
    return {
        "type_id": 1,
        "type": "Hearing",
        "date": date,
        "time": time,
        "location": location,
        "description": desc,
    }


DAY1_BILLS = [
    bill(
        1001,
        "HB 101",
        "Public Health - Opioid Overdose Prevention - Naloxone Access",
        "Requiring certain pharmacies to dispense naloxone without a prescription; requiring the "
        "Maryland Department of Health to establish an overdose response program.",
        HGO,
        calendar=[hearing("2026-02-10", "House Health and Government Operations Hearing")],
        sasts=[crossfile(1009, "SB 101")],
    ),
    bill(
        1002,
        "SB 55",
        "Budget Reconciliation and Financing Act of 2026",
        "Altering or repealing certain required appropriations; altering the distribution of "
        "certain revenue.",
        BT,
    ),
    bill(
        1003,
        "HB 210",
        "Public Health - Substance Use Disorder Treatment - Reimbursement",
        "Requiring the Maryland Medical Assistance Program to provide reimbursement for certain "
        "substance use disorder treatment services.",
        HGO,
        calendar=[hearing("2026-03-20", "House Health and Government Operations Hearing")],
    ),
    bill(
        1004,
        "HB 300",
        "Health Occupations - Licensing Renewals - Continuing Education",
        "Altering the continuing education requirements for the renewal of certain health "
        "occupations licenses.",
        HGO,
    ),
    bill(
        1005,
        "SB 120",
        "Vehicle Laws - Speed Monitoring Systems - Work Zones",
        "Authorizing the use of speed monitoring systems in certain highway work zones.",
        JUD,
    ),
    bill(
        1006,
        "HB 400",
        "Public Safety - Emergency Response Program - Establishment",
        "Establishing a program in the Department of Emergency Management to coordinate community "
        "response teams; requiring certain reporting.",
        ENV,
    ),
    bill(
        1007,
        "SB 130",
        "Cannabis - Taxation - Distribution of Revenue",
        "Altering the distribution of the sales and use tax revenue attributable to cannabis.",
        BT,
    ),
    # Senate cross-file of HB 101 with a vaguer title: no keyword hit on its own, Finance committee
    bill(
        1009,
        "SB 101",
        "Public Health - Pharmacies - Dispensing Standards",
        "Requiring certain pharmacies to dispense certain medications without a prescription; "
        "requiring the Department to establish a certain program.",
        FIN,
        sasts=[crossfile(1001, "HB 101")],
    ),
]

DAY1_SEARCH = {
    "overdose": [(1001, 100), (1006, 82), (1005, 31)],
    "opioid": [(1001, 100), (1003, 60)],
    "harm-reduction": [],
}


def masterlist(bills):
    ml = {"session": dict(SESSION)}
    for i, b in enumerate(bills):
        bb = b["bill"]
        ml[str(i)] = {
            "bill_id": bb["bill_id"],
            "number": bb["bill_number"],
            "change_hash": bb["change_hash"],
        }
    return {"status": "OK", "masterlist": ml}


def searchraw(query, hits, bills):
    by_id = {b["bill"]["bill_id"]: b["bill"] for b in bills}
    results = [
        {"relevance": rel, "bill_id": bid, "change_hash": by_id[bid]["change_hash"]}
        for bid, rel in hits
    ]
    return {
        "status": "OK",
        "searchresult": {
            "summary": {
                "page": "1 of 1",
                "range": f"1 - {len(results)}",
                "relevance": "100% - 0%",
                "count": len(results),
                "page_current": 1,
                "page_total": 1,
                "query": query.replace("-", " "),
            },
            "results": results,
        },
    }


def write(dirpath: Path, bills, searches, search_query_names):
    dirpath.mkdir(parents=True, exist_ok=True)
    for old in dirpath.glob("*.json"):
        old.unlink()
    (dirpath / "sessions_MD.json").write_text(
        json.dumps({"status": "OK", "sessions": [SPECIAL, SESSION, OLD_SESSION]}, indent=2) + "\n"
    )
    (dirpath / "masterlist_MD.json").write_text(json.dumps(masterlist(bills), indent=2) + "\n")
    for b in bills:
        (dirpath / f"bill_{b['bill']['bill_id']}.json").write_text(json.dumps(b, indent=2) + "\n")
    for slug, hits in searches.items():
        (dirpath / f"search_MD_{slug}.json").write_text(
            json.dumps(searchraw(search_query_names[slug], hits, bills), indent=2) + "\n"
        )
    for b in bills:
        for tdoc in b["bill"]["texts"]:
            (dirpath / f"text_{tdoc['doc_id']}.json").write_text(
                json.dumps(text_payload(b, tdoc), indent=2) + "\n"
            )


NAMES = {"overdose": "overdose", "opioid": "opioid", "harm-reduction": "harm reduction"}

# ---- day 2: HB 101 moves + gets a second hearing; HB 400 unchanged; new HB 600 (fentanyl) ---
DAY2_BILLS = copy.deepcopy(DAY1_BILLS)
hb101 = DAY2_BILLS[0]["bill"]
hb101["status"] = 2
hb101["status_date"] = "2026-02-12"
hb101["history"] = hb101["history"] + [
    {
        "date": "2026-02-10",
        "action": "Hearing 2/10 at 1:00 p.m.",
        "chamber": "H",
        "chamber_id": 1,
        "importance": 0,
    },
    {
        "date": "2026-02-12",
        "action": "Favorable with Amendments Report by Health and Government Operations",
        "chamber": "H",
        "chamber_id": 1,
        "importance": 1,
    },
    {
        "date": "2026-02-12",
        "action": "Third Reading Passed (135-2)",
        "chamber": "H",
        "chamber_id": 1,
        "importance": 1,
    },
]
hb101["committee"] = FIN
hb101["referrals"] = hb101["referrals"] + [{"date": "2026-02-13", **FIN}]
hb101["calendar"] = hb101["calendar"] + [
    hearing(
        "2026-02-24", "Senate Finance Hearing", "13:00", "3 East, Miller Senate Office Building"
    )
]
hb101["texts"] = hb101["texts"] + [text_doc(1001, "HB 101", "2026-02-12", "Engrossed", "t")]
hb101["change_hash"] = h(1001, "day2", 2, len(hb101["history"]))
DAY2_BILLS.append(
    bill(
        1008,
        "HB 600",
        "Criminal Law - Fentanyl Test Strips - Decriminalization",
        "Excluding fentanyl test strips and xylazine test strips from the definition of drug "
        "paraphernalia.",
        JUD,
        status_date="2026-02-11",
        calendar=[hearing("2026-02-25", "House Judiciary Hearing")],
    )
)
# HB 300 (watch-only) gets a technical text change: hash differs, nothing user-visible
DAY2_BILLS[3]["bill"]["change_hash"] = h(1004, "day2-text-only")

DAY2_SEARCH = {
    "overdose": [(1001, 100), (1006, 82), (1008, 55), (1005, 31)],
    "opioid": [(1001, 100), (1003, 60), (1008, 70)],
    "harm-reduction": [(1008, 64)],
}

if __name__ == "__main__":
    write(DAY1, DAY1_BILLS, DAY1_SEARCH, NAMES)
    write(DAY2, DAY2_BILLS, DAY2_SEARCH, NAMES)
    print(
        "wrote",
        len(list(DAY1.glob("*.json"))),
        "day-1 and",
        len(list(DAY2.glob("*.json"))),
        "day-2 fixture files",
    )
