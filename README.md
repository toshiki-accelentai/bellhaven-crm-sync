# Bellhaven CRM sync

Keeps the CRM's picture of **which facilities Bellhaven Senior Living owns** in line with Bellhaven's own website. Every day it scrapes the website, matches each community to a CRM account, and queues proposed fixes. A reviewer approves or rejects each one in a local app. Only approved changes are written, and each is re-checked against live data at the moment of approval.

```
scrape website ──► match to CRM ──► proposals (SQLite) ──► review app ──approve──► re-validate ──► CRM API
   35 sites          121 accounts      29 findings          human decides          live GET + SOP     PATCH / POST
```

## What changed in the CRM

_Filled in after review. Paste the `python -m bhsync verify` output here._

| Finding | Count | What I did |
|---|---|---|
| Confident match, nothing to change | 12 | none |
| Outdated / wrong name | 3 material + 4 cosmetic | renamed to the website name |
| Address error | 2 | Portsmouth zip 45626→45662; Ashtabula PO Box → street address |
| Wrong parent, re-parented directly | 4 | Lima (Harborview), Findlay (no parent), Zanesville (Cedar Trail), Kettering (Harborview) |
| Wrong parent, **CHOW** | 2 | Marietta and Tiffin: revenue history + outstanding AR, so the old accounts are preserved; new Bellhaven accounts created and linked via `chow_current_account` |
| Duplicate records | 7 losers across 5 facilities | Monroe ×3, Kettering ×3, Port Clinton, Erie, Owosso: losers set Inactive + `duplicate_of_account` |
| On website, not in CRM | 4 | Amberly Manor, Union Square, Batavia, Carlisle: created under Bellhaven |
| Under Bellhaven, no longer on website | 3 | Alliance, Coldwater, Sandusky: `Needs Review` + note |

## Judgment calls (and why)

- **Address beats name for matching.** Facilities get rebranded constantly; street addresses rarely change. Matching uses house number + normalised street (Rd/Road, W/West, Pk/Pike…) + zip. If the zip disagrees, the city is used instead (this caught the Portsmouth zip typo). A name + city fallback applies only inside Bellhaven's own accounts (this caught Ashtabula's PO box). Look-alikes such as *Union Square Senior Living*, *Maplewood Senior Care Center* and *Amberly Care Center* are at different addresses, so they are **not** matched. They appear on the card as "near-misses checked".
- **CHOW is applied literally.** The old account gets exactly one change, `chow_current_account`: no rename, no note, no status. The new account carries the website data and a note explaining the CHOW. The SOP check is re-run at approval time with live revenue/AR. If the outcome would flip (e.g. AR posted overnight), the write is blocked for re-approval rather than silently switched.
- **"No longer on the website" → `Needs Review`, parent kept.** Being absent from the website doesn't prove a facility closed or who owns it now. Detaching the parent would lose the only ownership signal we have, and Sandusky carries AR. Inactive is reserved for confirmed closures. This status queues the accounts for a human.
- **Duplicate survivor** = has revenue → most contacts → already under Bellhaven → name closest to the website → lowest id. The ranking is shown on every card. Losers keep their old parent: they are history, not live facilities. **Contacts on losers are not moved** (no merge exists, and moving contacts is a separate decision); the loser's note points to the survivor.
- **Ashtabula's PO Box** is replaced with the physical address, because every other account in this CRM stores the facility's physical address in `billing_street`. The PO box is preserved in the note in case billing needs it.
- **Cosmetic renames** (e.g. *Health Care Center* → *Healthcare Centre*) are proposed but grouped last. They matter for reps searching by the name on the sign, but they're low-risk.
- **Parent accounts (Harborview, Cedar Trail) are untouched.** An acquired portfolio still has a legal entity; that is not something to infer from a website.
- **No LLM in the pipeline.** Every decision has to be explainable on a review card and reproducible run to run; deterministic rules do that, and an LLM would add cost and variance. I used AI heavily to *build* and *review* the system, not to run it.

## Safety properties

| Property | How |
|---|---|
| Nothing writes without approval | `run` never calls a write endpoint; writes live only in `apply.approve`, triggered by the Approve button |
| Re-runs are safe | proposal fingerprint = issue + the data it was based on + exact payload (no dates). Decided fingerprints are never re-queued. If the data changes, a fresh proposal appears; a fixed issue that comes back is flagged as a *recurrence* |
| No acting on stale data | at approval, each account is re-fetched and compared with the snapshot the reviewer saw; any difference → *needs re-approval*, no write |
| No double-created accounts | POST is never blindly retried. The op marker is saved before sending; an unknown outcome is reconciled by looking for our marker. A CHOW whose PATCH failed resumes without a second POST |
| Every write is checked | after each PATCH the account is re-read and the fields confirmed |
| Broken scrape can't flag everything as gone | scraped count must equal the homepage's stated count ("35 communities") and stay within 2 of the last run; otherwise "not on website" proposals are withheld |
| Proof of end state | `verify` diffs the first-run CRM snapshot against the live CRM. Every changed field must be one an approved proposal wrote; CHOW predecessors may differ only in the pointer; duplicate pointers must resolve |
| Secrets | token only in `.env` (gitignored), redacted from the committed brief, never logged |
| Untrusted HTML | review app escapes all scraped text; binds to 127.0.0.1 |

Scraper traps handled: the directory's `?page=4` silently returns page 3 (the loop stops at "Page X of Y", with a page cap and repeat detection). Two communities appear on two pages (deduped by slug). **Findlay** is linked only from the homepage notice, not the directory, so links on `/` and `/about` are crawled too.

## Run it

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # then set BH_TOKEN
.venv\Scripts\python -m bhsync run        # scrape + match + queue (read-only against the CRM)
.venv\Scripts\python -m bhsync serve      # review app at http://127.0.0.1:5000  (--dry-run to rehearse)
.venv\Scripts\python -m bhsync verify     # prove the end state
.venv\Scripts\python -m bhsync report     # classification of every location
.venv\Scripts\python -m pytest -q         # 31 tests, no network
```

Daily schedule: [`crontab.txt`](crontab.txt) (cron + Windows Task Scheduler line). The job runs on the reviewer's host because proposals and decisions live in one SQLite file. GitHub Actions would need that state moved to a shared database first, which is the production upgrade path (Postgres).

Business rules you'd change live are all in [`bhsync/config.py`](bhsync/config.py): care-type mapping, survivor rule order, gate threshold, note prefix.

## Layout

| File | Role |
|---|---|
| `bhsync/scrape.py` | website → `Location` list + completeness gate |
| `bhsync/crm.py` | API client (paging, retries on reads, no blind POST retry) |
| `bhsync/match.py` | matching, survivor choice, proposal building (pure, deterministic) |
| `bhsync/sop.py` | CHOW vs re-parent decision |
| `bhsync/store.py` | SQLite: proposals, fingerprints, decisions, audit log |
| `bhsync/apply.py` | approve → re-validate → write → confirm; resumable |
| `bhsync/app.py` | Flask review app |
| `bhsync/verify.py` | end-state proof |
| `tests/` | golden test against a snapshot of the real site + CRM, SOP truth table, idempotency, crash/resume, stale-snapshot block, dry-run |

## Testing

- **Golden test:** pins the exact expected proposal for every one of the 35 locations plus the 3 stale accounts, using saved copies of the real site and CRM.
- **Full-cycle test:** approves everything against a fake CRM, then asserts `verify` passes and a re-run proposes **nothing**. This proves the CHOW successors and duplicate losers are recognised on the next day's run.
- **Guard tests were mutation-checked:** flipping the SOP's `and` to `or` fails 4 tests.

## How AI was used

- **Claude Code (Opus):** lead. Explored the site and API read-only, wrote the plan, the matcher, the SOP and the write path.
- **Two Sonnet subagents:** built the scraper and the review app, each against a fixed interface with its own tests.
- **Codex (`gpt-6.1-sol`):** independently reviewed the plan before any code (11 findings, 9 accepted, 2 partly accepted, reasons recorded in `PLAN.md` §9). It reviewed the write path again before the first real CRM write.
- **Verification was done by me, not trusted from the tools:** a hand-built expectation table from the raw data became the golden test, plus the mutation check, a live dry-run through the UI, and `verify` on the real CRM.

## Time

_Filled in at the end._
