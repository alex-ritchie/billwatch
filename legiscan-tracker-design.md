# Design Document: Legislative Bill Tracker & Digest

**Working name:** `billwatch` (rename at will)
**Status:** Draft v2 (adds §8 Data Privacy & Security; public-repo decision)
**Author:** [you]
**Last updated:** August 2026

---

## 1. Purpose & Background

A friend needs to track all new bills in the Maryland General Assembly related to substance use and overdose, including committee assignments, hearing dates, and status changes. Today this means manually checking the MGA website. This project automates that: a scheduled job pulls legislative data, filters for relevant bills, diffs against known state, and emails a newsletter-style digest.

Design goals, in priority order:

1. **Zero cost.** Every component must run on a free tier indefinitely.
2. **Zero software delivered to the user.** The recipient gets email; nothing to install, no executables to trust or maintain.
3. **Zero infrastructure owned.** No servers, no always-on machine, nothing that breaks when your laptop sleeps.
4. **Designed for growth without building for it.** The v1 is Maryland + one topic + a handful of recipients, but no v1 decision should have to be undone to reach all 50 states, multiple topics, or public sign-ups.
5. **Public repository.** The repo is public — it serves as portfolio work and as reusable code others can fork for related projects. Consequence: *nothing private may ever touch the repo*, in any commit, ever. §8 specifies exactly how this is enforced.

## 2. Requirements

### 2.1 Functional requirements

- **FR1 — Detection:** Identify new bills introduced in the Maryland General Assembly matching a configurable topic filter (substance use / overdose).
- **FR2 — Tracking:** For each matched bill, track status changes (committee referral, hearings scheduled, votes, passage, veto, enactment).
- **FR3 — Hearings & committees:** Surface upcoming committee hearing dates for tracked bills, ideally with enough lead time to attend or submit testimony.
- **FR4 — Digest delivery:** Send a periodic (daily) email digest containing: newly matched bills, status changes on tracked bills, and a "coming up" section of hearings in the next N days.
- **FR5 — Quiet handling:** When nothing has changed (common out of session), either skip the send or send a minimal note — configurable.
- **FR6 — Multiple recipients:** Support a small list of recipients from day one; support self-service sign-up later (Phase 3).
- **FR7 — Extensibility:** Adding a new (state, topic) pair should be a configuration change, not a code change.

### 2.2 Non-functional requirements

- **NFR1 — Cost:** $0/month. Hard requirement.
- **NFR2 — Freshness:** Daily granularity is sufficient. Legislative data sources themselves update roughly daily; hourly polling buys nothing and burns API quota.
- **NFR3 — Reliability:** A missed run should self-heal — the next run picks up everything since the last successful run, because diffs are computed against stored state, not against "yesterday."
- **NFR4 — Quota discipline:** Stay well under LegiScan's free limit of 30,000 API queries/month. Budget: < 3,000/month for the Maryland v1 (see §5.4).
- **NFR5 — Maintainability:** One Python package, standard tooling (`uv`, `pytest`, `ruff`), so it can sit untouched for months and still run.
- **NFR6 — Recipient privacy:** Recipient email addresses and all credentials live only in GitHub Actions Secrets or the delegated newsletter service — never in the repo, its history, the state DB, or logs. Digests use BCC. Enforced per §8.
- **NFR7 — Supply-chain integrity:** Every third-party GitHub Action **must** be pinned to a full-length (40-character) commit SHA. Floating tags (`@v4`) or branch refs (`@main`) are prohibited in all workflow files. See §8.3.

### 2.3 Explicit non-goals (v1)

- No web or mobile app, no dashboard, no user accounts.
- No bill full-text analysis or summarization (the synopsis is enough to start; LLM summarization is a Phase 2+ nice-to-have).
- No real-time alerts. Daily is the contract.
- No scraping of the MGA website. See §3.

## 3. Data Source Decision

**Decision: LegiScan Pull API (free public tier).** Registration is free and the public tier allows 30,000 queries per month, covering all 50 states, DC, and Congress with a uniform JSON schema: bill metadata, sponsors, status history, committee referrals, calendar/hearing events, and full-text links.

Why not the alternatives:

| Option | Verdict | Reason |
|---|---|---|
| Scrape mgaleg.maryland.gov | Rejected | Fragile (breaks on redesign), Maryland-specific parsers contradict FR7, and hearing calendars are the messiest part to scrape. |
| Open States (Plural) API | Strong fallback | Also free, well-normalized, 50-state. Kept as the designated Plan B; the internal data model (§5.2) is source-agnostic so a swap touches only the fetch layer. |
| Congress.gov API | Later | Free and official, but federal-only. Becomes relevant in Phase 4; LegiScan already covers Congress anyway. |

Key LegiScan operations we'll use:

- `getMasterListRaw(state)` — lightweight list of all bills in a session with `change_hash` per bill. This is the change-detection backbone: one query returns every bill's fingerprint, and we only fetch details for bills whose hash changed.
- `getBill(bill_id)` — full detail: status history, committee, calendar (hearings), sponsors, texts.
- `getSearchRaw(state, query)` — full-text relevance search, used as a secondary net to catch bills the keyword filter on titles/synopses would miss.

## 4. System Overview

```
┌────────────────────────────────────────────────────────────┐
│  GitHub Actions (cron: daily, ~07:30 ET)                   │
│                                                            │
│  ┌──────────┐   ┌──────────┐   ┌────────┐   ┌───────────┐  │
│  │  fetch    │→ │  filter   │→ │  diff   │→ │  digest    │  │
│  │ LegiScan  │  │ keywords/ │  │ vs      │  │ render +   │  │
│  │ masterlist│  │ committee │  │ SQLite  │  │ send email │  │
│  └──────────┘   └──────────┘   └────────┘   └───────────┘  │
│                                     │                      │
│                              commit updated                │
│                              state DB to repo              │
└────────────────────────────────────────────────────────────┘
```

Everything is one Python CLI (`billwatch run`) executed by a scheduled GitHub Actions workflow. There is no long-running process anywhere.

## 5. Detailed Design

### 5.1 Repository structure

```
billwatch/
├── .github/workflows/digest.yml     # scheduled runner
├── pyproject.toml                   # uv-managed
├── config/
│   └── feeds.toml                   # (state, topic) feed definitions
├── src/billwatch/
│   ├── __main__.py                  # CLI: run, backfill, test-email, dry-run
│   ├── legiscan.py                  # thin API client + quota accounting
│   ├── models.py                    # Bill, Event, Hearing dataclasses (source-agnostic)
│   ├── filters.py                   # keyword/committee matching
│   ├── store.py                     # SQLite state: bills, hashes, sent-log
│   ├── digest.py                    # jinja2 → HTML + plain-text email
│   └── mailer.py                    # SMTP (Gmail) or Buttondown backend
├── templates/
│   └── digest.html.j2
├── state/
│   └── billwatch.db                 # committed back by the workflow
└── tests/
```

### 5.2 Data model (SQLite)

```sql
CREATE TABLE bills (
  bill_id      INTEGER PRIMARY KEY,   -- LegiScan id
  feed         TEXT NOT NULL,         -- e.g. "md-substance-use"
  number       TEXT,                  -- "HB 123"
  title        TEXT,
  synopsis     TEXT,
  url          TEXT,
  status       INTEGER,               -- LegiScan status code
  status_date  TEXT,
  committee    TEXT,
  change_hash  TEXT,                  -- LegiScan fingerprint
  first_seen   TEXT,
  last_updated TEXT
);

CREATE TABLE hearings (
  bill_id      INTEGER REFERENCES bills(bill_id),
  date         TEXT,
  time         TEXT,
  committee    TEXT,
  location     TEXT,
  announced_in_digest INTEGER DEFAULT 0,
  PRIMARY KEY (bill_id, date, committee)
);

CREATE TABLE sent_log (
  run_date     TEXT PRIMARY KEY,
  feed         TEXT,
  new_bills    INTEGER,
  changed      INTEGER,
  skipped      INTEGER          -- 1 if digest suppressed (no changes)
);
```

The `models.py` dataclasses are the internal contract; `legiscan.py` maps API JSON into them. Swapping to Open States later means writing one new mapper.

### 5.3 Feed configuration

A *feed* = (jurisdiction, topic filter, recipients, schedule). This is the unit of extensibility (FR7).

```toml
# config/feeds.toml
[feeds.md-substance-use]
state    = "MD"
schedule = "daily"

# Primary filter: keywords against title + synopsis (case-insensitive,
# word-boundary regex). Curated with the domain expert (your friend).
keywords = [
  "opioid", "overdose", "naloxone", "fentanyl", "xylazine",
  "harm reduction", "controlled dangerous substance", "buprenorphine",
  "methadone", "substance use", "substance abuse", "drug treatment",
  "recovery residence", "syringe", "prescription drug monitoring",
]

# Secondary net: LegiScan full-text search queries (catches bills whose
# title/synopsis are vague but whose text is on-topic).
searches = ["overdose", "opioid", "harm reduction"]

# Tertiary signal (optional): flag anything referred to these committees
# for manual review in a separate digest section, even if no keyword hit.
watch_committees = ["Health and Government Operations", "Finance"]

exclude_keywords = []   # escape hatch for noisy false positives
```

Filtering philosophy: legislation titles/synopses are keyword-dense, so curated keywords + LegiScan search will achieve high recall. Precision errors (false positives) are cheap — one extra line in an email. An LLM classifier is deliberately deferred; if added later it slots into `filters.py` as a re-ranker over keyword candidates, not a replacement.

### 5.4 The daily run, step by step

1. **Load state** — open `state/billwatch.db` from the repo checkout.
2. **Masterlist pull** — `getMasterListRaw("MD")`: 1 query returning every bill + `change_hash` for the current session.
3. **Candidate selection** — bills that are (a) not in the DB at all, or (b) in the DB with a different `change_hash`. Out of session this is usually zero.
4. **Detail fetch** — `getBill()` for each candidate only. In-session Maryland introduces roughly 2,500–3,500 bills across a 90-day session; on the busiest introduction days this might be ~150 detail fetches, but a typical in-session day is well under 50 and out-of-session days are ~0. Also run the configured `getSearchRaw` queries (3/day) and fetch details for any new hits.
5. **Filter** — apply keyword/committee rules to candidates; matched bills are inserted/updated, hearings extracted from the bill's calendar block.
6. **Diff → events** — produce three lists: *new matches*, *status changes on tracked bills*, *hearings within the next 14 days not yet announced*.
7. **Render** — Jinja2 → HTML digest (plus plain-text alternative part). Sections: 🆕 New bills / 🔄 Movement / 📅 Upcoming hearings / (optional) 👀 Committee watch. Each bill links to its LegiScan and MGA pages.
8. **Send** — BCC to recipient list (see §7). If all three lists are empty and `send_empty = false`, skip and log.
9. **Persist** — commit the updated DB + append to `sent_log`; push.

**Quota math (NFR4):** worst-case in-session ≈ 1 masterlist + 150 details + 3 searches + ~20 search-hit details ≈ 175 queries/day ≈ 5,400/month during the session, and far less in a typical week. Against a 30,000/month allowance that leaves ~5x headroom — enough for several additional states later, though a 50-state build-out would need the caching layer to be aggressive and might eventually justify LegiScan's paid tiers or a hybrid with Open States.

### 5.5 Error handling

- Any API failure → retry with backoff (3 attempts); on final failure, exit nonzero so the Actions run is marked failed and GitHub emails you (free failure alerting).
- Send failures after a successful fetch → state is still committed, but the events are marked unsent and merged into the next digest (NFR3: diffs come from the DB, so nothing is lost).
- A `dry-run` CLI mode renders the digest to a file without sending — used in CI on pull requests.

## 6. Running on GitHub Actions

### 6.1 Why Actions instead of your local machine

Your machine sleeps, reboots, and travels. A scheduled workflow in a GitHub repo runs regardless, costs nothing (public repos: free; private repos: 2,000 minutes/month free, and this job needs ~2 min/day ≈ 60 min/month), and doubles as hosting, version control, and failure alerting. It also fully dissolves the "how do I ship my friend an executable" problem — nobody receives software, only email.

### 6.2 Workflow

```yaml
# .github/workflows/digest.yml
name: daily-digest
on:
  schedule:
    - cron: "30 11 * * *"   # 11:30 UTC ≈ 07:30 ET (cron is always UTC)
  workflow_dispatch: {}      # manual "run now" button

permissions:
  contents: write            # to commit the state DB back

concurrency:
  group: digest
  cancel-in-progress: false

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      # NFR7: actions MUST be pinned to full commit SHAs, never floating tags.
      # <sha> placeholders below — resolve each to the 40-char commit SHA of the
      # release you're adopting (visible on the action's Releases/Tags page),
      # and record the human-readable version in the trailing comment.
      - uses: actions/checkout@<sha>        # vX.Y.Z
      - uses: astral-sh/setup-uv@<sha>      # vX.Y.Z
      - run: uv sync --frozen
      - name: Run billwatch
        env:
          LEGISCAN_API_KEY: ${{ secrets.LEGISCAN_API_KEY }}
          SMTP_USERNAME:    ${{ secrets.SMTP_USERNAME }}
          SMTP_APP_PASSWORD: ${{ secrets.SMTP_APP_PASSWORD }}
          RECIPIENTS:       ${{ secrets.RECIPIENTS }}   # comma-separated
        run: uv run billwatch run --config config/feeds.toml
      - name: Persist state
        run: |
          git config user.name "billwatch-bot"
          git config user.email "actions@github.com"
          git add state/billwatch.db
          git diff --cached --quiet || git commit -m "state: $(date -u +%F)"
          git push
```

### 6.3 Operational notes & gotchas

- **Scheduled-workflow auto-disable:** GitHub disables cron workflows on repos with no activity for 60 days. Our workflow *commits daily*, which counts as activity and keeps itself alive — a happy side effect of committing the state DB. Belt-and-suspenders: you'll also notice if digests stop.
- **Cron drift:** scheduled runs can start minutes-to-an-hour late during peak load. Irrelevant at daily granularity; don't schedule exactly on the hour (hence `:30`).
- **Secrets hygiene:** API key, SMTP credentials, and the recipient list are repo secrets, never in code. The repo is public, so full privacy rules live in §8; per NFR7, the `<sha>` placeholders in the YAML above must be resolved to real commit SHAs before the workflow is ever enabled.
- **DB-in-repo:** committing a small SQLite file daily is unusual but fine at this scale (the DB stays in the low MBs; a session's worth of matched bills is tiny). If it ever feels wrong, alternatives are a separate `state` branch, GitHub Actions cache, or a free-tier hosted store — but don't add that complexity preemptively.
- **Reproducibility:** `uv sync --frozen` against the committed lockfile means the job that runs in March is byte-identical to the one you tested in January.

## 7. Email Delivery & Sign-up

Delivery evolves in three phases. Each phase is a superset of the previous; nothing is thrown away.

### Phase 1 — Hardcoded recipients (v1, ships first)

- **Transport:** Gmail SMTP with an app password (requires 2FA on the account). Fine for a handful of recipients; Gmail's ordinary sending limits are orders of magnitude above one digest/day.
- **List management:** `RECIPIENTS` secret, comma-separated, BCC'd. "Unsubscribe" = text your friend.
- Consider a dedicated Gmail account (`mdbillwatch@gmail.com`) so the sender identity is clean and transferable.

### Phase 2 — Sign-up form, still ~zero users of your time

When a second-degree contact asks to join:

- **Landing page:** GitHub Pages (free) on the same repo — one static `index.html` describing the digest, with a sample issue.
- **Form backend:** Formspree free tier — a plain HTML `<form action="https://formspree.io/f/...">` on the Pages site; submissions arrive as email. Free tier allows 50 submissions/month, which comfortably covers organic sign-ups for a niche digest; unlimited forms, 2 linked notification emails, 30-day history.
- **Processing:** manual at first — you get the sign-up email, you add the address to the `RECIPIENTS` secret. One minute per subscriber, entirely acceptable below ~30 people.
- **Compliance note:** once you email people you don't personally know, include an unsubscribe line (a `mailto:` link is fine at this scale) and honor it promptly — both CAN-SPAM hygiene and basic courtesy.

### Phase 3 — Real newsletter plumbing (only if it grows)

At the point where manual list edits or Gmail's limits chafe (~50+ subscribers), move the *audience* out of secrets and into a newsletter service:

- **Buttondown** (free tier ≈ 100 subscribers, has an API and hosted sign-up/unsubscribe pages) is the natural fit: `mailer.py` grows a second backend that POSTs the rendered digest to Buttondown's API, and sign-up/unsubscribe/archives become their problem. Verify current tier limits when you get there.
- Alternatives at similar cost (Brevo/Mailgun free SMTP tiers) would require managing the subscriber list ourselves. **Ruled out**: this repo is public, so no subscriber data may be stored anywhere in the project (§8). Delegating the audience to a newsletter service is not just convenient — it is the privacy design.
- The Formspree form is replaced by the service's embed form; the Pages site stays.

**Per-feed subscriptions** (someone wants Virginia but not Maryland) arrive in this phase: the recipient list becomes per-feed — either separate Buttondown newsletters per feed or tags — and `feeds.toml` gains a `list_id` per feed. The digest pipeline doesn't change at all.

## 8. Data Privacy & Security

The repo is public: every file, every commit, and the entire git history are world-readable, permanently. There are no per-file permissions or hidden paths in a public GitHub repo, so privacy cannot be achieved by hiding — it is achieved by **strict separation**: private data never enters the repo at all.

### 8.1 Data classification & inventory

Every piece of data the system touches, classified once, with exactly one permitted storage location:

| Data | Classification | Where it lives | Never appears in |
|---|---|---|---|
| Bill metadata, statuses, hearings, sent-log counts | Public (it's public legislative data) | Repo: `state/billwatch.db`, config, templates | — |
| LegiScan API key | Secret | GitHub Actions Secret `LEGISCAN_API_KEY` | Repo, DB, logs |
| Gmail/SMTP username & app password | Secret | Actions Secrets `SMTP_USERNAME`, `SMTP_APP_PASSWORD` | Repo, DB, logs |
| Recipient email addresses (Phases 1–2) | Private PII | Actions Secret `RECIPIENTS` (comma-separated) | Repo, DB, logs, digest headers (BCC only) |
| Subscriber list (Phase 3) | Private PII | Buttondown's audience system exclusively | Repo, DB — the project never stores it |
| Sign-up submissions (Phase 2) | Private PII | Formspree → your email inbox | Repo (submissions are never committed) |
| Local dev credentials | Secret | `.env`, gitignored | Repo |

The invariant, stated once and enforced everywhere: **the repo and the state DB contain only public legislative data and aggregate counts.** The `sent_log` table stores *how many* items went out, never *to whom*. No names, no email addresses, no per-recipient records exist anywhere in the project's own storage, in any phase.

### 8.2 Why GitHub Actions Secrets are sufficient

Actions Secrets are encrypted at rest, are not visible in the repo UI or API, are not exposed to forks, and are automatically redacted (`***`) if a workflow step would print them. They are injected only as environment variables into workflow runs on *this* repo's branches. This is the standard mechanism for exactly this problem, and at this project's threat level (hobbyist credentials, a short recipient list) it is the appropriate one.

### 8.3 Write-access & workflow hardening

Public ≠ writable. Outsiders can read and fork but cannot push; still, because the workflow runs daily with secrets in scope, the following are required:

- **Fork PR isolation:** PRs from forks must never receive secrets. Use only `pull_request` triggers for CI (which runs forks without secrets); **never** introduce `pull_request_target` with secret access. CI on PRs is limited to lint/tests/`dry-run` against recorded fixtures, not live credentials.
- **Least-privilege token:** the workflow's `GITHUB_TOKEN` carries only `contents: write` (needed to commit the state DB). No other scopes.
- **SHA-pinned actions (mandatory — NFR7):** every third-party action reference **must** use a full 40-character commit SHA (`actions/checkout@34e1148...  # v4.1.7`), never a floating tag or branch. Tags are mutable pointers: a compromised maintainer account can silently repoint `v4` at malicious code, which then executes inside a secrets-bearing run — the mechanism behind the March 2025 `tj-actions/changed-files` supply-chain attack. A commit SHA is content-addressed and cannot be repointed. Policy details: (a) each pin carries a trailing comment naming the version it corresponds to; (b) updates happen only via Dependabot PRs (`package-ecosystem: github-actions`, which understands SHA pins) reviewed like any dependency bump; (c) this applies to *all* workflow files, including CI, not just the secrets-bearing digest workflow — one policy, no exceptions to remember; (d) verify the SHA belongs to the intended repo/tag when first adopting an action (copy it from the action's Releases page, not from a blog post).
- **Pinned dependencies:** `uv sync --frozen` against the committed lockfile; Dependabot enabled for deliberate, reviewed updates.
- **Branch protection** on `main`: no force-pushes, so the daily state-commit history can't be silently rewritten by a compromised collaborator account (also enable 2FA on the GitHub account itself).

### 8.4 Leak prevention & log hygiene

- **Push protection + secret scanning** (free on public repos) enabled from day one, catching credential-shaped strings before they reach history.
- **`.gitignore`** covers `.env` and any local scratch output from `test-email`/`dry-run` runs that could contain a real address.
- **Code-level rule:** logging and exception paths must never echo `RECIPIENTS` or SMTP values; the mailer logs recipient *count*, not addresses. The digest template has no per-recipient content, and delivery uses BCC so recipients can't see each other either.
- **Tests use fixtures**, never live keys; recorded API responses are scrubbed before committing (LegiScan responses are public data, but scrub as a habit).

### 8.5 Git history is permanent — incident response

Deleting a leaked secret or email address in a follow-up commit does **not** remove it: it remains in history and in any fork made in the meantime. If a leak ever happens, the response is, in order: (1) **rotate the credential immediately** (revoke the Gmail app password / regenerate the LegiScan key) or, for a leaked address, notify the person; (2) rewrite history (`git filter-repo`) and force-push; (3) assume the pre-rewrite content was copied and act accordingly. Rotation is the real fix; history rewriting is cosmetic. This is why §8.4's prevention measures matter more than any cleanup procedure.

### 8.6 Third-party data handling (Phases 2–3)

Sign-ups and subscriptions deliberately live with processors built for PII: Formspree (submission → your inbox, 30-day retention on their side) and Buttondown (audience management, hosted unsubscribe). The project's public code only ever *sends content to* these services; it never syncs their subscriber data back into the repo. Include an unsubscribe path in every digest and honor removals promptly — with the `RECIPIENTS` secret this is a manual edit; with Buttondown it's automatic.

## 9. Roadmap to 50 States, Congress, and More Topics

The design makes each expansion a config/ops change, not an architecture change:

| Phase | Scope | What changes |
|---|---|---|
| 1 | MD × substance use, ~3 recipients | Ship everything in §5–§6, Phase-1 email |
| 2 | + sign-up page; maybe +1–2 states or topics | Pages site + Formspree; new `[feeds.*]` blocks; quota check |
| 3 | Public-ish newsletter, per-feed subscriptions | Buttondown backend in `mailer.py`; per-feed lists |
| 4 | Many states / Congress / many topics | Quota becomes the binding constraint: add masterlist caching per state, batch feeds so each state is pulled once and shared across topic feeds, and evaluate Open States as a supplementary source or LegiScan paid tier. Possibly split workflows per state for isolation. |

Two things to watch as it scales: (a) **quota** — pulling masterlists for 50 states is 50 queries/day, trivial, but detail-fetch volume scales with matched-bill churn, so the shared-fetch/caching layer in Phase 4 matters; (b) **curation** — keyword lists are per-topic domain knowledge, and quality will depend on having a subject-matter reviewer per topic, which is a human bottleneck, not a technical one.

## 10. Risks & Open Questions

- **LegiScan free-tier changes:** terms could tighten. Mitigation: source-agnostic models + Open States fallback (§3).
- **Hearing-data completeness:** LegiScan's calendar data depends on how promptly each legislature publishes hearing notices; Maryland is generally good, but validate against the MGA site during the first session and, if gaps appear, add a narrow MGA hearings-page fetch for *tracked bills only* (a much smaller scrape than full-site scraping).
- **Session boundaries:** LegiScan sessions roll over yearly; the fetch layer must pin the current session id and handle special sessions. Handle in `legiscan.py`, test at the January session start.
- **Filter recall:** the real risk is a relevant bill that never matches. Mitigations: the committee-watch section (§5.3) as a human backstop, and periodically asking your friend "did we miss anything this month?" as ground truth for tuning.
- **Bus factor:** it runs under your GitHub account and Gmail app password. Document setup in the README well enough that the repo could be forked and re-secreted by someone else in an afternoon.
- **PII/credential leak into public history:** the highest-consequence failure mode for a public repo. Mitigated by the §8 storage invariant, push protection, log hygiene, and the rotation-first incident response in §8.5.

## 11. Milestones

1. **M1 (a weekend):** LegiScan client + models + SQLite store + keyword filter; `dry-run` renders a digest from live MD data.
2. **M2 (+ a few evenings):** Gmail sending, Actions workflow, state-commit loop; first real daily digest to yourself for a week.
3. **M3:** add your friend; tune keywords against his feedback for 2–4 weeks (ideally overlapping a session or interim committee activity).
4. **M4:** Pages site + Formspree form when the first outside person asks.
5. **M5+:** per roadmap table.
