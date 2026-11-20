# Bellhaven CRM Sync — Implementation Plan (v2, Codex-reviewed)

## 1. What the assessment asks

| # | Requirement | Hard constraint |
|---|---|---|
| R1 | Scraper: every Bellhaven location (name, address, city, state, zip, care offerings) | "every" — pagination + homepage-only page |
| R2 | Match + classify: confident match, needs fix (wrong parent / outdated name), not in CRM, Bellhaven child no longer on site | — |
| R3 | Local review app: proposal + evidence, approve / reject; approved → API write | **Nothing writes without approval** |
| R4 | Daily schedule config (cron or GH Actions, not live) | **Re-runs never re-propose decided items** |
| R5 | Duplicates: loser gets `duplicate_of_account` = survivor + `status=Inactive`. No merge/delete. | status ∈ {Active, Inactive, Needs Review} |
| R6 | CHOW SOP: re-parent needed AND `lifetime_revenue>0` AND `outstanding_ar>0` → old account **exactly as is** except `chow_current_account` = new id; new account under correct parent. Else re-parent directly. | "matters as much as matching" |
| R7 | **Graded on CRM end state.** Run, review, approve before deadline. | Unapproved queue = zero |
| R8 | ~2 min recording: evidence, approve one, reject one, change visible in CRM browser | link-shareable |
| R9 | Writeup of choices + honest time | fast & sound beats long & perfect |
| R10 | 45-min live walkthrough incl. a small live code change | code must be easy to change live |

AI tools allowed; no LLM needed in the pipeline; nothing hosted. Execution brief: **2-hour build budget.**

**What they're really grading:** judgment on messy data (catching traps), write discipline (approval gate, SOP, no collateral damage to billing records), operability (idempotent daily runs, audit trail), and whether I *verify* AI output instead of trusting it.

## 2. Ground truth (read-only exploration, already done)

Site: 35 communities = 34 unique in paginated directory + **Findlay (homepage link only)**. Homepage states "35 communities". `?page=4` silently returns page 3. Amberly Manor and Chesterton appear on two pages. CRM: 121 accounts, 67 contacts; Bellhaven parent `0015QAPLGS3FVYEEEM`. About page: Harborview acquired 2025, "select" Cedar Trail communities 2026.

**Expected end state (= golden acceptance test):**

| Case | Facilities | Action |
|---|---|---|
| Clean match | Altoona, Defiance, Goshen, Kalamazoo, Maplewood, Marion, Meadville, New Carlisle, Saline, Akron, Toledo, Wooster | none (Blvd/Boulevard etc. not a change) |
| Outdated name | Riverbend Manor Care Center→Bellhaven of Chagrin Falls; Chesterton Senior Commons→Bellhaven of Chesterton; Sunny Acres Retirement Home→Bellhaven Willow Creek | rename |
| Cosmetic name drift | Arbors Dayton, Ashland, Grove City, Sycamore Ridge | rename, reviewed last |
| Address error | Portsmouth zip 45626→45662; Ashtabula PO Box 517→3156 W Prospect Rd | update; Ashtabula's PO box preserved in the appended note |
| Direct re-parent | Lima (Harborview, rev 47k/AR 0), Findlay (no parent, 22k/0), Zanesville "Cedar Trail of Zanesville" (0/0, + rename) | PATCH parent_id |
| **CHOW** | Marietta (51,250 / 3,800), Tiffin (84,000 / 12,400) — both Cedar Trail | POST new under Bellhaven with website data; old gets **only** `chow_current_account` |
| Duplicates | Monroe ×3, Kettering ×3 (none under Bellhaven → survivor re-parented + renamed), Port Clinton ×2, Erie ×2, Owosso ×2 | losers Inactive + duplicate_of; losers keep their parent |
| Not in CRM | Amberly Manor (Hudson OH), Bellhaven at Union Square, Batavia, Carlisle PA | POST under Bellhaven |
| False-positive traps | Union Square Senior Living (Juniper, other street), Maplewood Senior Care Center (Stonebridge), Amberly Care Center (Grand Rapids MI) | **no match**; shown as near-miss evidence |
| Stale Bellhaven child | Alliance, Coldwater, Sandusky (130k / 5.2k AR) | `Needs Review` + note; parent unchanged |
| Out of scope | Harborview / Cedar Trail parent accounts; contacts on duplicate losers | untouched; noted in writeup |

## 3. Design

Deterministic, explainable rules; a human approves every write; every write is re-validated against live data. No LLM in the pipeline: matching here is address-driven, and each decision has to be explainable to a sales-ops reviewer and reproducible run to run.

### 3.1 Pipeline — `python -m bhsync run`
1. **Scrape.** Read "Page X of Y" and fetch 1..Y (max 20 pages; stop if a page repeats). Add every `/communities/<slug>` link found on `/` and `/about`. Dedupe by slug. Parse each detail page; required fields must be present. Save a raw snapshot JSON.
2. **Completeness gate.** Scraped count must equal the homepage's stated count (35) and the previous run's count minus 2 at most. Otherwise, emit no STALE proposals and log a warning (match/update proposals still run). A broken scrape can never mark facilities as gone.
3. **Load CRM.** Page `/accounts` up to `total`. Count contacts per account. Save a full **pre-run snapshot**, which feeds the end-state diff.
4. **Match** (collect candidates from every rule, then decide):
   - A: address key = street number + normalised street (suffix/directional map) + zip
   - B: street number + normalised street + city/state (catches zip typos)
   - C: only if A and B find nothing: normalised name + city/state among Bellhaven children (catches a bad street, e.g. the PO box)
   - Accounts already carrying `duplicate_of_account`, or `chow_current_account` (follow the pointer), are excluded. Pointers are checked for missing targets and cycles.
   - No candidates → NEW, with same-city or similar-name accounts listed as near-misses.
5. **Resolve per facility:**
   - Multiple accounts → duplicate group. Survivor ranking: revenue>0 → more contacts → already under Bellhaven → name closest to the website → lowest id. Ranking and alternatives are shown on the card.
   - **Facility proposal** (one atomic bundle with the exact payload): field fixes + parent fix.
     - If CHOW is required, the bundle becomes "POST new account carrying the website data + PATCH old `chow_current_account`". The old account gets no field fixes at all.
   - Each duplicate loser gets its own proposal, independent of the survivor bundle.
   - Unmatched active Bellhaven children are STALE, excluding duplicate losers and CHOW predecessors.
   - Care mapping: Short-Term Rehabilitation & Nursing→Skilled Nursing, Memory Support→Memory Care, Assisted Living→Assisted Living. When a site lists several offerings, the CRM value is kept if it is one of them.

### 3.2 Idempotency
- `issue_key` = (type, account_id or slug).
- `fingerprint` = issue_key + before-values + exact payload, with no timestamps.
- A **rejected** fingerprint is suppressed forever. If the evidence changes, the fingerprint changes and the item is re-proposed, marked "evidence changed".
- An **applied** proposal removes its own diff. If the same issue_key reappears later, it is re-proposed as a **recurrence**, which is a new event.
- Single SQLite file, local; cron runs on the same machine as the review app.

### 3.3 Apply (on Approve)
- Serialised; the button is disabled while a write is in flight.
- Re-GET every account in the bundle and compare it with the before-snapshot.
  - On mismatch → `needs re-approval`, no write.
- Re-evaluate the SOP with live revenue/AR. If the outcome flips (e.g. REPARENT→CHOW), the write is **blocked for re-approval**, never silently switched.
- POST safety:
  - Persist state `posting` + op id before sending.
  - The new account's note carries `bhsync-op:<id>`.
  - On an uncertain result (timeout/5xx), reconcile by listing accounts with `created_by_candidate=true` at that address and checking the op marker. Never blind-retry a POST.
- CHOW resume: incomplete bundles (POST done, PATCH pending) are finished **before** the next run's matching.
- Notes are **appended** with a dated prefix, never overwritten.
  - CHOW old accounts get no note and no status change.
- Audit table logs every request/response (token redacted). `--dry-run` prints payloads without sending.

### 3.4 Review app (Flask, 127.0.0.1, server-rendered)
- **One page, cards ordered by risk:** CHOW → re-parent → duplicate → stale → new → field fix → cosmetic.
- **Card contents:**
  - before→after diff and exact payload
  - which match rule fired, plus near-misses
  - website snapshot + live link
  - CRM browser link
  - SOP box ("rev $84,000 · AR $12,400 → CHOW required")
  - survivor ranking
- Approve / Reject (optional reason). The result shows the API response and the CRM link.
- **Filter:** Pending / Decided.
- **Safety:** Jinja autoescape (scraped HTML is untrusted); POST-only state changes.

### 3.5 Verify — `python -m bhsync verify`
Diff the pre-run snapshot against the live CRM and assert:
- (a) every changed account maps to an approved proposal
- (b) every other account is byte-identical
- (c) each CHOW old account differs **only** in `chow_current_account`
- (d) duplicate pointers resolve to Active survivors
- (e) the §2 golden table holds

Output pasted into the README. "Zero pending proposals" alone is not treated as proof.

### 3.6 Stack & layout
- **Stack:** Python 3.12 · requests · beautifulsoup4 · Flask · sqlite3 · difflib · pytest.
- **Rejected:**
  - Streamlit: its rerun model fights approve/write state
  - Playwright: the site is static
  - LLM matching: non-deterministic and unexplainable
  - GitHub Actions: state isn't shared with the local reviewer, so cron instead; noted in the writeup
- **Layout:**
```
bhsync/ models.py config.py scrape.py crm.py match.py sop.py store.py apply.py app.py verify.py __main__.py
bhsync/templates/  tests/fixtures/  tests/test_*.py  crontab.txt  README.md  .env.example
```
- **Config:** `config.py` holds the care map, survivor rule order, risk order, note template and gate thresholds. These are the seams for the live-change part of the walkthrough.

## 4. Orchestration, models, time budget

**Critical path:** contracts → (scrape ‖ CRM load) → match/SOP → store + apply → integration dry-run → **your** review + demo → verify → writeup. Integration is the bottleneck, so only two helpers, each with a file boundary and a fixed interface.

| Agent | Model / effort | Owns | Gets only | Acceptance |
|---|---|---|---|---|
| Lead (me) | Opus, high | models, config, crm, match, sop, store, apply, verify, `__main__`, crontab, README | everything | golden table passes on dry-run; SOP truth table green |
| H1 Scraper | Sonnet subagent, medium | `scrape.py`, `tests/test_scrape.py` | saved HTML fixtures, §3.1 steps 1–2, `Location` dataclass | 35 from fixtures; Findlay; no dupes; clamp/repeat-safe; gate logic |
| H2 Review app | Sonnet subagent, medium | `app.py`, `templates/` | `models.py`, store/apply function signatures, §3.4, sample proposals JSON | renders sample; approve/reject call `apply.approve(id)` / `store.reject(id, reason)`; autoescape; 127.0.0.1 |
| Codex risk review | Codex CLI `gpt-6.1-sol`, high, read-only | none (report only) | `sop.py`, `apply.py`, `match.py`, `store.py` | run **before the first real write**; each finding fixed or rejected with a reason |

Cut from v1: the docs agent (crontab/README stay with the lead), blind golden tests by Codex (they would inherit my expectation table anyway), the GH Actions file, bulk approve and extra tabs. Mechanical file reads use Haiku only if needed.

| Window | Work |
|---|---|
| T+0–10 | Initial commit, `.env`, token redaction, `models.py` + function signatures, fixtures (serial; unblocks helpers) |
| T+10–50 | Lead: crm/match/sop/store/apply ‖ H1 ‖ H2 |
| T+50–65 | Integrate, pytest, live **dry-run** vs golden table |
| T+65–75 | Codex risk review → fixes |
| T+75–95 | **You** review the queue in the app + record the demo (approvals are yours, not mine) |
| T+95–105 | `verify`, re-run → nothing re-proposed, CRM browser spot-check |
| T+105–120 | Writeup; buffer |

**If behind, cut in this order:** cosmetic-rename proposals (documented as a known gap) → near-miss display → audit table detail. **Never cut:** approval gate, SOP + CHOW resume, idempotency, verify.

Honest time: the ~45 min of exploration and planning already spent are logged in the README alongside build time.

## 5. Tests
- **Unit:**
  - normaliser table
  - SOP truth table: rev>0 & AR>0 → CHOW; every other combination → reparent; null / str / negative inputs
  - survivor ranking, care map, fingerprint stability
- **Scraper fixtures:** pagination clamp, repeated page, cross-page duplicate, homepage-only link, multi-badge, missing field.
- **Golden:** CRM + site fixtures → exactly the §2 proposal set.
- **Idempotency:**
  - run → decide all → run → 0 new
  - rejected stays suppressed
  - CHOW interrupted after POST → resume completes the PATCH, no second POST
- **Live:** `verify` (§3.5) after approvals.

## 6. Risks
| Risk | Mitigation |
|---|---|
| Test writes pollute the graded CRM | No exploratory writes; dry-run first; first real write = an approved proposal |
| CHOW old account touched beyond the pointer | Bundle design + verify check (c) |
| Double-create on retry | op marker + reconcile; no blind POST retry |
| Wrong duplicate survivor (Kettering tie) | Ranking + alternatives on card; reviewer rejects if wrong and the writeup records it |
| Broken scrape → mass STALE | Completeness gate |
| Noise proposals | Semantic address normalisation; cosmetic group last |
| Token leak | `.env` gitignored, token redacted from the committed instructions, never logged |
| Overrun | Fixed cut order; two helpers only |

## 7. Schedule
`crontab.txt`: `0 6 * * * cd /path/bhsync && .venv/bin/python -m bhsync run >> logs/run.log 2>&1`. The job only **proposes**; humans approve in the app. The writeup explains why cron on the reviewer host beats GH Actions here (shared SQLite state), and that production would move state to Postgres.

## 8. Deliverables & business value
1. **Repo:** code, tests, crontab, README.
   - README opens with a "what changed in the CRM" table: counts by type, AR preserved on the CHOW predecessors (accurately worded).
   - Then the judgment calls: PO box, stale → Needs Review, survivor rule, parents/contacts untouched, why no LLM.
   - Then the verify output and honest time.
2. **Corrected CRM:** all proposals decided; `verify` passes.
3. **~2 min demo:** CHOW card with SOP evidence → approve → show in the CRM browser; one evidence-backed reject (e.g. a cosmetic rename, with a reason).

## 9. Codex review disposition
| # | Finding | Decision |
|---|---|---|
| 1 | CHOW old account could get separate field updates | **Accepted** — CHOW bundle; website data goes only to the new account |
| 2 | POST not crash-safe | **Accepted in part** — `posting` state + op marker + reconcile. Address + `created_by_candidate` is already unique here (the old CHOW account is not candidate-created) |
| 3 | Fingerprint vs recurrence | **Accepted** — issue_key/fingerprint split, rejected vs recurrence |
| 4 | Approval dependencies | **Accepted** — per-facility atomic bundle; SOP flip blocks for re-approval |
| 5 | First-hit-wins hides duplicates | **Accepted** — union of address rules; name rule is fallback only; pointer cycle check |
| 6 | Survivor ranking favours empty shells | **Accepted** — revenue/contacts before parent. Survivor-override UI **rejected** (time; reject + note covers it) |
| 7 | Ashtabula: keep PO box | **Rejected in part** — `billing_street` holds the physical address for every other account in this CRM, so the PO box is the anomaly; update it but preserve the old value in the note. Stale/Harborview/contacts advice accepted |
| 8 | GH Actions state; stale includes losers | **Accepted** — cron only; exclusions explicit |
| 9 | Scraper gate too loose | **Accepted** — exact stated-count gate, page cap, repeat detection |
| 10 | "0 proposals" isn't proof | **Accepted** — snapshot-diff `verify` |
| 11 | Orchestration overcommitted (195 min) | **Accepted** — 2 helpers, cuts listed, 120-min plan |
