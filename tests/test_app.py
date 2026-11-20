import copy
import json
from pathlib import Path

from bhsync import store
from bhsync.app import create_app

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample_proposals.json").read_text(encoding="utf-8"))


def make(sample=SAMPLE):
    conn = store.connect(":memory:")
    store.upsert_proposals(conn, copy.deepcopy(sample))
    calls = []

    def approve_fn(conn_, pid, client, dry_run):
        calls.append(pid)
        return {"ok": True, "status": "applied", "message": "done", "result": None}

    return conn, calls, create_app(conn, approve_fn=approve_fn).test_client()


def test_index_shows_titles_and_sop():
    _, _, c = make()
    html = c.get("/").get_data(as_text=True)
    assert "CHOW: create new Bellhaven account" in html and "Mark Harborview" in html
    assert "$84,000" in html and "$12,400" in html and "(new account)" in html


def test_script_title_escaped():
    s = copy.deepcopy(SAMPLE)
    s[0]["title"] = "<script>alert(1)</script>"
    html = make(s)[2].get("/").get_data(as_text=True)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_approve_calls_fn_once_and_redirects():
    _, calls, c = make()
    r = c.post("/proposals/1/approve")
    assert r.status_code == 302 and r.headers["Location"].endswith("#p1") and calls == [1]


def test_approve_rejected_not_called():
    conn, calls, c = make()
    store.reject(conn, 1, "no")
    c.post("/proposals/1/approve")
    assert calls == []


def test_reject_sets_reason_and_decided_view():
    conn, _, c = make()
    c.post("/proposals/2/reject", data={"reason": "not a dup"})
    p = store.get(conn, 2)
    assert p["status"] == "rejected" and p["reason"] == "not a dup"
    assert "Mark Harborview" in c.get("/?view=decided").get_data(as_text=True)
    assert "Mark Harborview" not in c.get("/").get_data(as_text=True)
