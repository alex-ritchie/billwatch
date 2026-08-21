# billwatch

**Legislative bill tracker & email digest.** A scheduled GitHub Actions job pulls
legislative data from [LegiScan](https://legiscan.com/legiscan), filters it for a topic
(v1: Maryland bills on substance use & overdose), diffs it against stored state, and emails
a newsletter-style daily digest: new bills, movement on tracked bills, and upcoming
committee hearings.

Design goals (see [`legiscan-tracker-design.md`](legiscan-tracker-design.md) for the full
design document): **zero cost, zero software delivered to recipients, zero infrastructure
owned, configurable growth to more states/topics, and a public repo that never contains
anything private.**

```
GitHub Actions (cron, daily ~07:30 ET)
┌──────────┐   ┌──────────┐   ┌────────┐   ┌───────────┐
│  fetch   │ → │  filter  │ → │  diff  │ → │  digest   │ → commit updated
│ LegiScan │   │ keywords │   │ SQLite │   │ + email   │   state DB to repo
└──────────┘   └──────────┘   └────────┘   └───────────┘
```

## How it works

Every run (`billwatch run`):

1. Picks the current regular session for each state (`getSessionList`) and pulls the
   session's master list (`getMasterListRaw`) — one query returns every bill's `change_hash`.
2. Fetches details (`getBill`) **only** for bills whose hash is new or changed. Out of
   session this is ~0 queries; a busy in-session day is ~50–150.
3. Runs the feed's full-text searches (`getSearchRaw`) as a safety net for bills whose
   title/synopsis are vague.
4. Applies keyword / committee rules. Matched bills are tracked; changes on tracked bills
   become "movement"; calendar entries become hearings. A matched bill's **cross-filed**
   companion (the same bill in the other chamber — LegiScan's `sasts` links) is tracked too,
   and the digest shows the pair as one entry ("HB 101 / SB 101").
5. Stores the **latest full text** of every tracked bill (extracted from the PDF, compressed,
   ~10 KB each) for later analysis — `billwatch fetch-texts --show HB1109` prints one.
6. Builds the digest from **all unsent events** in the DB (so a failed send is merged into
   the next digest — nothing is lost) plus hearings in the next 14 days not yet announced.
7. Sends via Gmail SMTP with recipients as BCC only, or skips if there's nothing new.
8. The workflow commits `state/billwatch.db` back to the repo.

Feeds are grouped by state, so several topic feeds for one state share a single set of API
calls.

## Setting it up (fork → secrets → enable)

You need: a GitHub account, a free [LegiScan API key](https://legiscan.com/legiscan), and a
Gmail account with 2-step verification (for an *app password*). A dedicated Gmail account
(e.g. `od.bill.watch@gmail.com`) keeps the sender identity clean.

1. **Fork / clone** this repo. Keep it public if you like — nothing private goes in it.
2. **Add repository secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `LEGISCAN_API_KEY` | your LegiScan key |
   | `SMTP_USERNAME` | the Gmail address that sends |
   | `SMTP_APP_PASSWORD` | a Gmail app password (Google Account → Security → App passwords) |
   | `SMTP_FROM` | (optional) From address, defaults to `SMTP_USERNAME` |
   | `RECIPIENTS` | comma-separated recipient addresses; BCC'd |
   | `BUTTONDOWN_API_KEY` | (Phase 3 only) Buttondown API token |

   Optional non-secret settings go on the **Variables** tab of the same page and can be
   left unset: `SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (`587`),
   `BILLWATCH_MAILER` (`smtp` | `buttondown` | `console`). Only the first five secrets are
   needed for v1; the workflow forwards all of these so changing one later is a settings
   edit, not a code change.

3. **Edit [`config/feeds.toml`](config/feeds.toml)** — keywords, searches, watched
   committees. Adding a state/topic is another `[feeds.<name>]` block.
4. **Enable GitHub features**: Actions (allow workflows), *push protection + secret
   scanning* (Settings → Code security), branch protection on `main` (no force-push), and
   Dependabot (already configured in [`.github/dependabot.yml`](.github/dependabot.yml)).
5. **Seed the state** so the first digest isn't a 300-bill wall: locally,
   `uv run billwatch backfill --max-queries 0` (one query per bill in the current session —
   ~2,700 for a Maryland session, a one-time cost), then commit `state/billwatch.db`. This
   also fills the bill cache that `reevaluate` uses for keyword tuning. (Without a backfill,
   the first scheduled run reports everything currently in the session as "new".)
6. Digests arrive daily at ~07:30 ET. GitHub emails you if a run fails.

To swap maintainers: fork, re-add the five secrets, done.

## Local development

```bash
uv sync                          # Python 3.11+, deps from the lockfile
cp .env.example .env             # fill in your keys — .env is gitignored
uv run pytest                    # unit + integration tests (offline, ~5 s)
uv run ruff check .

# Offline dry-run against recorded fixtures (this is what CI does on PRs):
uv run billwatch dry-run --fixtures tests/fixtures/legiscan --today 2026-02-01 --out out/

# Live dry-run: fetch real data, render to out/, change nothing (uses a copy of the DB):
uv run billwatch dry-run --out out/

# Verify SMTP settings with a sample digest:
uv run billwatch test-email --to you@example.com

# Real run (sends email, writes state/billwatch.db):
uv run billwatch run
```

Commands: `run` · `dry-run` · `backfill` · `reevaluate` · `fetch-texts` · `summary` ·
`test-email`. Global flags:
`--env-file`, `-v`. Per-command: `--config`, `--db`, `--feed NAME` (repeatable),
`--today YYYY-MM-DD`, `--fixtures DIR`, `--max-queries N` (override the per-run query cap;
`0` = unlimited). Set `BILLWATCH_MAILER=console` to print digests instead of sending.

### Tuning keywords (the M3 loop)

The daily run only re-evaluates a bill when LegiScan's `change_hash` changes, so editing
`feeds.toml` would not, by itself, touch the bills already seen. Every fetched bill's
filter-relevant fields are therefore kept in a small `bill_cache` table, and
`reevaluate` re-applies the current rules to it — offline, in seconds:

```bash
# edit config/feeds.toml (keywords / searches / watch_committees / exclude_keywords), then:
uv run billwatch reevaluate --dry-run     # preview: added / promoted / demoted / removed
uv run billwatch reevaluate               # apply (quietly — nothing lands in the next digest)
uv run billwatch reevaluate --announce    # ...or surface the additions in the next digest
git add state/billwatch.db && git commit -m "state: reevaluate after keyword change"
```

New matches get their full detail fetched (~1 query each, `--no-fetch` to skip); searches
are re-run (one query per configured search term); cross-filed companions of matches are
adopted, and companions of bills that stop matching are pruned with them. `--no-prune` only
adds/promotes and never removes. `--refetch` re-pulls every tracked bill's detail first
(useful after upgrades). Tip: **quote multi-word search phrases** (`'"harm reduction"'`) —
LegiScan's full-text search matches unquoted words independently.

### End-of-session summary

`billwatch summary` builds a one-shot recap of **every tracked bill in the session**, grouped
by outcome — ✅ Became law · 🚫 Vetoed · 📬 Passed the legislature · 🛑 Did not advance — with
cross-filed pairs shown together and each chamber's final action. Useful after sine die, or
as a sample to show a prospective subscriber what the feed covers.

```bash
uv run billwatch summary                                # render to out/*.html,.txt (no email)
uv run billwatch summary --send                         # email it to $RECIPIENTS
uv run billwatch summary --send --to friend@example.com # one-off send, list untouched
```

Reads a copy of the DB and sends nothing to the sent-log — it never affects the daily digest.

### Bill texts

Only tracked bills get text, and only the latest version (Introduced → Engrossed → Enrolled →
Chaptered), so the committed DB stays small (~10 KB per bill compressed). Texts are fetched
automatically by `run`/`backfill`/`reevaluate` whenever a tracked bill is new or gains a new
version (one LegiScan `getBillText` query each; PDFs are converted with `pypdf`).

```bash
uv run billwatch fetch-texts               # (re)sync texts for all tracked bills
uv run billwatch fetch-texts --stats       # how many, how big
uv run billwatch fetch-texts --show HB1109 # print one bill's text
```
In Python: `Store(...).get_text("MD", bill_id)["text"]` — the intended hook for future
LLM/semantic analysis (design §2.3: deliberately not built yet).

Exit codes: `0` ok · `1` configuration error · `2` fetch/delivery failure (marks the
Actions run failed).

## Configuration reference (`config/feeds.toml`)

```toml
[settings]
hearing_lookahead_days = 14   # "Upcoming hearings" window
send_empty = false            # true → send a minimal "nothing new" note
subject_prefix = "[billwatch]"
unsubscribe_note = "..."      # footer line, CAN-SPAM hygiene
search_min_relevance = 50     # ignore LegiScan search hits below this relevance %
max_queries_per_run = 400     # safety cap; leftover candidates roll to the next run

[feeds.md-substance-use]
state = "MD"                  # two-letter LegiScan state code ("US" = Congress)
title = "..."                 # digest heading
keywords = [...]              # case-insensitive, word-boundary; matched on title + synopsis
searches = [...]              # LegiScan full-text queries (secondary net)
watch_committees = [...]      # flag referrals for manual review even without a keyword hit
exclude_keywords = []         # veto list for noisy false positives
recipients_env = "RECIPIENTS" # env var / secret holding this feed's recipient list
# session_id = 2144           # pin a LegiScan session (default: latest regular session)
# hearing_lookahead_days / send_empty / search_min_relevance may be overridden per feed
```

## Operations notes

- **Quota.** Free tier is 30,000 queries/month. Every run logs its query count and the
  `sent_log` table records it per day. Typical: ~6 queries/day out of session, well under
  200/day at peak. `max_queries_per_run` is a hard stop (`--max-queries` overrides it for one
  run); deferred bills are picked up next run.
- **Session rollover.** Each run picks the newest non-prior regular session automatically.
  Pin `session_id` in a feed if you need a special session or an older one.
- **DB in the repo.** `state/billwatch.db` is small (public legislative data + counts). The
  daily commit also keeps the scheduled workflow from being auto-disabled after 60 idle days.
- **Failed send.** The fetched state is still committed; pending events stay unsent and are
  merged into the next successful digest.
- **Recipient changes.** Edit the `RECIPIENTS` secret. Honor unsubscribe requests promptly.

## Privacy & security (short version)

- The repo and the state DB contain **only public legislative data and aggregate counts**.
  Credentials and recipient addresses live exclusively in GitHub Actions Secrets (or a
  gitignored `.env`). Logs report recipient *counts*, never addresses. Digests use BCC.
- Every GitHub Action is pinned to a full commit SHA with a version comment; Dependabot
  handles bumps. CI uses `pull_request` (never `pull_request_target`), read-only token, no
  secrets. The digest workflow's token has only `contents: write`. Tests enforce all of this
  (`tests/unit/test_workflow_hardening.py`).
- If a secret ever leaks: rotate it first, rewrite history second (see design §8.5).

## State DB (SQLite) at a glance

| table | what | keyed by |
|---|---|---|
| `bills` | every matched bill per feed (+ `tracked` 1/0, match `reasons`, cross-file `sasts`, text versions) | `(feed, bill_id)` |
| `hearings` | calendar entries for bills, with an announced flag | `(feed, bill_id, date, committee)` |
| `events` | pending/sent digest items (`new` / `status` / `watch`) | `id` |
| `seen` | change-detection memory: every bill's last `change_hash` | `(scope, bill_id)` |
| `bill_cache` | filter-relevant fields for every bill ever fetched (for `reevaluate`) | `(state, bill_id)` |
| `bill_texts` | latest full text of each tracked bill, zlib-compressed | `(state, bill_id)` |
| `sent_log` | per-run counts incl. LegiScan queries used (quota ledger) | `(run_date, feed)` |

Only public legislative data and counts — never recipients or credentials (design §8.1).

## Layout

```
.github/workflows/digest.yml   scheduled runner        src/billwatch/legiscan.py  API client + mappers
.github/workflows/ci.yml       lint/tests/dry-run      src/billwatch/filters.py   keyword/committee rules
config/feeds.toml              feed definitions        src/billwatch/store.py     SQLite state
templates/digest.{html,txt}.j2 email templates         src/billwatch/digest.py    digest assembly + rendering
state/billwatch.db             committed by workflow   src/billwatch/mailer.py    SMTP / Buttondown / file
tests/{unit,integration}       pytest                  src/billwatch/pipeline.py  the daily run
tests/fixtures/legiscan*       recorded API responses  src/billwatch/reevaluate.py offline keyword tuning
                                                       src/billwatch/texts.py     bill text fetch/extract
                                                       src/billwatch/__main__.py  CLI
```

## Roadmap

Phase 2: GitHub Pages sign-up page + Formspree. Phase 3: Buttondown backend
(`BILLWATCH_MAILER=buttondown`, already stubbed) with per-feed lists. Phase 4: more states,
Congress, more topics — config changes plus quota care. Details in the design doc.

## License

MIT.
