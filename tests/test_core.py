"""Golden, SOP, idempotency and write-path tests. No network: a fake CRM stands in for the API."""
import copy
import json
from pathlib import Path

import pytest

from bhsync import apply, match, sop, store
from bhsync.crm import CRMError
from bhsync.scrape import scrape
from bhsync.verify import verify
from test_scrape import BASE, make_get

FIX = Path(__file__).parent / "fixtures" / "crm"
ACCOUNTS = json.loads((FIX / "accounts.json").read_text())
CONTACTS = json.loads((FIX / "contacts.json").read_text())
LOCS, _ = scrape(BASE, make_get())


def build(accounts=ACCOUNTS):
    return match.build(LOCS, accounts, CONTACTS)


def by_facility(props):
    out = {}
    for p in props:
        out.setdefault(p["facility"], []).append(p["kind"])
    return {k: sorted(v) for k, v in out.items()}


# ---------- golden: the reviewed expectation table from PLAN.md §2 ----------

GOLDEN = {
    "Bellhaven of Marietta": ["CHOW"], "Bellhaven of Tiffin": ["CHOW"],
    "Bellhaven Crossings of Lima": ["REPARENT"], "Bellhaven Meadows of Findlay": ["REPARENT"],
    "Bellhaven of Zanesville": ["REPARENT"],
    "Bellhaven of Kettering": ["DUPLICATE", "DUPLICATE", "REPARENT"],
    "Bellhaven Gardens of Monroe": ["DUPLICATE", "DUPLICATE"], "Bellhaven of Port Clinton": ["DUPLICATE"],
    "Bellhaven Shores of Erie": ["DUPLICATE"], "Bellhaven of Owosso": ["DUPLICATE"],
    "Amberly Manor": ["NEW"], "Bellhaven at Union Square": ["NEW"], "Bellhaven of Batavia": ["NEW"],
    "Bellhaven of Carlisle": ["NEW"],
    "Bellhaven of Chagrin Falls": ["UPDATE"], "Bellhaven of Chesterton": ["UPDATE"],
    "Bellhaven Willow Creek": ["UPDATE"], "Bellhaven of Portsmouth": ["UPDATE"], "Bellhaven of Ashtabula": ["UPDATE"],
    "The Arbors at Bellhaven - Dayton": ["COSMETIC"], "Bellhaven Healthcare Centre of Ashland": ["COSMETIC"],
    "Bellhaven Rehabilitation & Nursing of Grove City": ["COSMETIC"], "Bellhaven at Sycamore Ridge": ["COSMETIC"],
    "Bellhaven Care Center of Alliance": ["STALE"], "Bellhaven of Coldwater": ["STALE"],
    "Bellhaven of Sandusky": ["STALE"],
}


def test_golden_proposal_set():
    props, report, warnings = build()
    assert by_facility(props) == GOLDEN
    assert warnings == []
    assert sum(r["class"] == "CONFIDENT MATCH" for r in report) == 12


def test_false_positive_traps_not_matched():
    props, _, _ = build()
    touched = {p["account_id"] for p in props}
    for trap in ("Union Square Senior Living", "Maplewood Senior Care Center", "Amberly Care Center"):
        aid = next(a["account_id"] for a in ACCOUNTS if a["name"] == trap)
        assert aid not in touched


def test_chow_leaves_old_account_untouched_except_pointer():
    props, _, _ = build()
    for p in (p for p in props if p["kind"] == "CHOW"):
        patches = [s for s in p["steps"] if s["op"] == "patch"]
        assert patches == [{"op": "patch", "account_id": p["account_id"], "fields": {"chow_current_account": "$new"}}]
        assert p["steps"][0]["op"] == "create" and p["steps"][0]["fields"]["parent_id"] == match.config.PARENT_ID


def test_duplicate_losers_keep_parent_and_go_inactive():
    props, _, _ = build()
    for p in (p for p in props if p["kind"] == "DUPLICATE"):
        f = p["steps"][0]["fields"]
        assert f["status"] == "Inactive" and f["duplicate_of_account"] and "parent_id" not in f


def test_ashtabula_po_box_preserved_in_note():
    p = next(p for p in build()[0] if p["facility"] == "Bellhaven of Ashtabula")
    assert "PO Box 517" in p["steps"][0]["fields"]["note"]


def test_incomplete_scrape_withholds_stale():
    props, _, warnings = match.build(LOCS, ACCOUNTS, CONTACTS, site_complete=False)
    assert not [p for p in props if p["kind"] == "STALE"] and warnings


# ---------- SOP truth table ----------

@pytest.mark.parametrize("rev,ar,outcome", [
    (100, 5, "CHOW"), (100, 0, "REPARENT"), (0, 5, "REPARENT"), (0, 0, "REPARENT"),
    (None, None, "REPARENT"), ("84,000", "12,400", "CHOW"), (100, -5, "REPARENT"), ("n/a", 5, "CHOW"),
])
def test_sop(rev, ar, outcome):
    assert sop.decide(rev, ar)["outcome"] == outcome


# ---------- idempotency ----------

def test_rerun_does_not_repropose_decided():
    conn = store.connect(":memory:")
    props, _, _ = build()
    assert store.upsert_proposals(conn, props) == (len(props), 0)
    first = store.list_proposals(conn)
    store.reject(conn, first[0]["id"], "no")
    assert store.upsert_proposals(conn, build()[0]) == (0, len(props))


# ---------- write path against a fake CRM ----------

class FakeCRM:
    def __init__(self, accounts, fail_patch_once=False):
        self.db = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.posts = 0
        self.fail_patch_once = fail_patch_once
        self.audit = None

    def get_account(self, aid):
        return copy.deepcopy(self.db[aid])

    def find_accounts(self, **f):
        return [copy.deepcopy(a) for a in self.db.values() if a["billing_zip"] == f.get("zip")]

    def accounts(self):
        return [copy.deepcopy(a) for a in self.db.values()]

    def create_account(self, fields):
        self.posts += 1
        aid = f"001NEW{self.posts:012d}"
        base = {k: "" for k in next(iter(self.db.values()))}
        self.db[aid] = {**base, "lifetime_revenue": 0, "outstanding_ar": 0, **fields, "account_id": aid,
                        "created_by_candidate": True}
        return copy.deepcopy(self.db[aid])

    def patch_account(self, aid, fields):
        if self.fail_patch_once:
            self.fail_patch_once = False
            raise CRMError("HTTP 503", 503)
        self.db[aid].update(fields)
        return copy.deepcopy(self.db[aid])


def _approve_all(conn, crm):
    for p in store.list_proposals(conn, ("pending",)):
        assert apply.approve(conn, p["id"], crm)["ok"], p["title"]


def test_full_cycle_end_state_and_rerun_is_quiet():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    store.upsert_proposals(conn, build()[0])
    _approve_all(conn, crm)
    ok, lines = verify(ACCOUNTS, crm.accounts(), store.list_proposals(conn))
    assert ok, lines
    props, _, warnings = build(crm.accounts())
    assert props == [] and warnings == []  # nothing left to fix, CHOW successors matched, losers excluded


def test_chow_resumes_after_failed_patch_without_second_post():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS, fail_patch_once=True)
    store.upsert_proposals(conn, [p for p in build()[0] if p["facility"] == "Bellhaven of Tiffin"])
    pid = store.list_proposals(conn)[0]["id"]
    first = apply.approve(conn, pid, crm)
    assert not first["ok"] and first["result"]["new_id"]
    second = apply.approve(conn, pid, crm)
    assert second["ok"] and crm.posts == 1


def test_stale_snapshot_blocks_write():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    p = next(p for p in build()[0] if p["facility"] == "Bellhaven Crossings of Lima")
    store.upsert_proposals(conn, [p])
    crm.db[p["account_id"]]["outstanding_ar"] = 500  # billing changed after the proposal was made
    out = apply.approve(conn, store.list_proposals(conn)[0]["id"], crm)
    assert not out["ok"] and out["status"] == "needs_reapproval"
    assert crm.db[p["account_id"]]["parent_id"] != match.config.PARENT_ID


def test_dry_run_writes_nothing():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    store.upsert_proposals(conn, build()[0])
    snapshot = copy.deepcopy(crm.db)
    for p in store.list_proposals(conn):
        apply.approve(conn, p["id"], crm, dry_run=True)
    assert crm.db == snapshot and crm.posts == 0


# ---------- regressions for the Codex write-path review ----------

def _one(conn, facility, kind=None):
    p = next(p for p in build()[0] if p["facility"] == facility and (kind is None or p["kind"] == kind))
    store.upsert_proposals(conn, [p])
    return store.list_proposals(conn)[-1]["id"], p


def test_post_lost_response_is_reconciled_not_reposted():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    real = crm.create_account

    def flaky(fields):  # account is created server-side, but the client sees a 502
        real(fields)
        raise CRMError("HTTP 502", 502)
    crm.create_account = flaky
    pid, _ = _one(conn, "Bellhaven of Batavia")
    out = apply.approve(conn, pid, crm)
    assert out["ok"] and crm.posts == 1


def test_unknown_post_outcome_never_reposts():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)

    def lost(fields):  # nothing created, outcome unknown
        raise CRMError("timeout")
    crm.create_account = lost
    pid, _ = _one(conn, "Bellhaven of Batavia")
    assert not apply.approve(conn, pid, crm)["ok"]
    crm.create_account = lambda f: pytest.fail("must not POST again while the first outcome is unknown")
    assert not apply.approve(conn, pid, crm)["ok"]


def test_new_blocked_if_account_appeared_since():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    pid, p = _one(conn, "Bellhaven of Batavia")
    crm.create_account(p["steps"][0]["fields"])  # someone else created it meanwhile
    out = apply.approve(conn, pid, crm)
    assert out["status"] == "needs_reapproval" and crm.posts == 1


def test_patch_that_landed_can_be_resumed():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    pid, p = _one(conn, "Bellhaven Crossings of Lima")
    crm.db[p["account_id"]].update(apply._render(p["steps"], "x")[0]["fields"])  # write landed, response lost
    store.set_status(conn, pid, "error", reason="timeout")
    assert apply.approve(conn, pid, crm)["ok"]


def test_claim_blocks_second_executor():
    conn = store.connect(":memory:")
    pid, _ = _one(conn, "Bellhaven Crossings of Lima")
    assert store.claim(conn, pid, apply.APPROVABLE)
    assert not apply.approve(conn, pid, FakeCRM(ACCOUNTS))["ok"]


def test_verify_fails_when_applied_change_is_missing():
    conn, crm = store.connect(":memory:"), FakeCRM(ACCOUNTS)
    pid, p = _one(conn, "Bellhaven Crossings of Lima")
    assert apply.approve(conn, pid, crm)["ok"]
    crm.db[p["account_id"]]["parent_id"] = ACCOUNTS[0]["parent_id"]  # silently reverted later
    assert not verify(ACCOUNTS, crm.accounts(), store.list_proposals(conn))[0]


def test_note_rewording_does_not_resurrect_rejected():
    conn = store.connect(":memory:")
    props = build()[0]
    store.upsert_proposals(conn, props)
    for p in store.list_proposals(conn):
        store.reject(conn, p["id"])
    reworded = [{**p, "steps": [{**s, "fields": {**s["fields"], "note": "different"}} for s in p["steps"]]}
                for p in props]
    assert store.upsert_proposals(conn, reworded) == (0, len(props))
