"""Execute an approved proposal against the CRM — only after re-validating it against live data."""
from __future__ import annotations

from datetime import date

from . import sop, store
from .crm import CRMError
from .match import norm_street
from .models import APPLIED, ERROR, NEEDS_REAPPROVAL, PENDING

APPROVABLE = (PENDING, NEEDS_REAPPROVAL, ERROR)


def approve(conn, pid: int, client, dry_run: bool = False) -> dict:
    p = store.get(conn, pid)
    if not p or p["status"] not in APPROVABLE:
        return _out(False, p["status"] if p else "missing", "Proposal is not awaiting approval.")
    progress = p.get("result") or {}
    marker = f"bhsync-op:{p['fingerprint'][:12]}"
    steps = _render(p["steps"], marker)

    if not progress.get("new_id"):  # a resumed CHOW already passed validation before its POST
        problem = _revalidate(p, client)
        if problem:
            store.set_status(conn, pid, NEEDS_REAPPROVAL, reason=problem)
            return _out(False, NEEDS_REAPPROVAL, f"Not written: {problem} Re-run the pipeline for a fresh proposal.")
    if dry_run:
        return _out(True, p["status"], "Dry run: nothing written.", {"steps": steps})

    client.audit = lambda *a: store.audit(conn, pid, *a)
    try:
        new_id = progress.get("new_id")
        for step in steps:
            if step["op"] == "create":
                if not new_id and progress.get("stage") == "posting":
                    new_id = _find_created(client, step["fields"], marker)  # crashed mid-POST last time
                if not new_id:
                    store.save_progress(conn, pid, {"stage": "posting", "marker": marker})
                    new_id = _create(client, step["fields"], marker)
                    progress = {"stage": "created", "new_id": new_id, "marker": marker}
                    store.save_progress(conn, pid, progress)
            else:
                target = new_id if step["account_id"] == "$new" else step["account_id"]
                fields = {k: (new_id if v == "$new" else v) for k, v in step["fields"].items()}
                client.patch_account(target, fields)
                _confirm(client, target, fields)
        result = {"new_id": new_id, "touched": sorted({s.get("account_id") for s in p["steps"]} - {None, "$new"}
                                                      | ({new_id} if new_id else set()))}
        store.set_status(conn, pid, APPLIED, result=result)
        return _out(True, APPLIED, "Written to CRM" + (f" (new account {new_id})" if new_id else "") + ".", result)
    except CRMError as e:
        store.set_status(conn, pid, ERROR, reason=str(e)[:500], result=progress or None)
        return _out(False, ERROR, f"CRM write failed: {e}. Approve again to resume safely.", progress or None)
    finally:
        client.audit = None


def _revalidate(p: dict, client) -> str:
    """Live data must still equal the snapshot the reviewer saw; the SOP outcome must not have flipped."""
    for aid, fields in p["before"].items():
        live = client.get_account(aid)
        changed = [f for f, v in fields.items() if live.get(f) != v]
        if changed:
            return f"{aid} changed since proposal ({', '.join(changed)})."
        if p["kind"] in ("CHOW", "REPARENT"):
            outcome = sop.decide(live.get("lifetime_revenue"), live.get("outstanding_ar"))["outcome"]
            if outcome != p["kind"]:
                return f"SOP outcome for {aid} is now {outcome}, proposal was {p['kind']}."
    return ""


def _create(client, fields: dict, marker: str) -> str:
    """POST once; if the outcome is unknown, look for the account we may have created before failing."""
    try:
        body = client.create_account(fields)
        new_id = body.get("account_id") or body.get("id") if isinstance(body, dict) else None
        if new_id:
            return new_id
    except CRMError as e:
        if e.status is not None and e.status < 500:
            raise  # definite rejection: nothing was created
    found = _find_created(client, fields, marker)
    if not found:
        raise CRMError("account creation outcome unknown and no matching account found; not retrying POST")
    return found


def _find_created(client, fields: dict, marker: str) -> str | None:
    key = norm_street(fields["billing_street"])
    for a in client.find_accounts(zip=fields["billing_zip"]):
        if a.get("created_by_candidate") and norm_street(a["billing_street"]) == key and marker in (a.get("note") or ""):
            return a["account_id"]
    return None


def _confirm(client, account_id: str, fields: dict) -> None:
    live = client.get_account(account_id)
    wrong = [f for f, v in fields.items() if live.get(f) != v]
    if wrong:
        raise CRMError(f"{account_id}: write not reflected for {', '.join(wrong)}")


def _render(steps: list[dict], marker: str) -> list[dict]:
    today = date.today().isoformat()
    out = []
    for s in steps:
        fields = {k: (v.replace("{today}", today) if isinstance(v, str) else v) for k, v in s["fields"].items()}
        if s["op"] == "create":
            fields["note"] = f"{fields.get('note', '')} [{marker}]".strip()
        out.append({**s, "fields": fields})
    return out


def _out(ok, status, message, result=None) -> dict:
    return {"ok": ok, "status": status, "message": message, "result": result}
