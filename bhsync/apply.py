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
    if not p or not store.claim(conn, pid, APPROVABLE):  # atomic: only one executor per proposal
        return _out(False, p["status"] if p else "missing", "Proposal is not awaiting approval.")
    prior = p["status"]
    progress = p.get("result") or {}
    marker = f"bhsync-op:{p['fingerprint'][:12]}"
    steps = _render(p["steps"], marker)
    try:
        problem = "" if progress.get("new_id") else _revalidate(p, steps, client)
        if problem:
            store.set_status(conn, pid, NEEDS_REAPPROVAL, reason=problem, result=progress or None)
            return _out(False, NEEDS_REAPPROVAL, f"Not written: {problem} Re-run the pipeline for a fresh proposal.")
        if dry_run:
            store.set_status(conn, pid, prior, reason=p.get("reason", ""), result=progress or None)
            return _out(True, prior, "Dry run: nothing written.", {"steps": steps})
        client.audit = lambda *a: store.audit(conn, pid, *a)
        new_id = progress.get("new_id")
        for step in steps:
            if step["op"] == "create":
                if not new_id:
                    new_id, progress = _create_once(conn, pid, client, step["fields"], marker, progress)
                continue
            target = new_id if step["account_id"] == "$new" else step["account_id"]
            fields = {k: (new_id if v == "$new" else v) for k, v in step["fields"].items()}
            if not _already(client, target, fields):  # a previous attempt may have landed
                client.patch_account(target, fields)
            _confirm(client, target, fields)
        touched = {s["account_id"] for s in p["steps"] if s.get("account_id") not in (None, "$new")}
        result = {"new_id": new_id, "touched": sorted(touched | ({new_id} if new_id else set()))}
        store.set_status(conn, pid, APPLIED, result=result)
        return _out(True, APPLIED, "Written to CRM" + (f" (new account {new_id})" if new_id else "") + ".", result)
    except CRMError as e:
        progress = (store.get(conn, pid).get("result") or progress)  # keep the latest persisted progress
        store.set_status(conn, pid, ERROR, reason=str(e)[:500], result=progress or None)
        return _out(False, ERROR, f"CRM write failed: {e}. Approve again to resume safely.", progress or None)
    finally:
        client.audit = None


def _revalidate(p: dict, steps: list[dict], client) -> str:
    """Live data must equal what the reviewer saw (or what this proposal already wrote); SOP must not flip."""
    targets = {s["account_id"]: s["fields"] for s in steps if s["op"] == "patch"}
    for aid, fields in p["before"].items():
        live = client.get_account(aid)
        done = targets.get(aid, {})
        changed = [f for f, v in fields.items() if live.get(f) != v and not (f in done and live.get(f) == done[f])]
        if changed:
            return f"{aid} changed since proposal ({', '.join(changed)})."
        if p["kind"] in ("CHOW", "REPARENT") and aid == p["account_id"]:
            outcome = sop.decide(live.get("lifetime_revenue"), live.get("outstanding_ar"))["outcome"]
            if outcome != p["kind"]:
                return f"SOP outcome for {aid} is now {outcome}, proposal was {p['kind']}."
    for s in steps:
        if s["op"] == "create":
            clash = _accounts_at(client, s["fields"], exclude=set(p["before"]))
            if clash:
                return f"an account now exists at this address ({', '.join(clash)})."
    return ""


def _create_once(conn, pid, client, fields: dict, marker: str, progress: dict) -> tuple[str, dict]:
    """POST at most once per proposal. An unknown outcome is reconciled, never blindly retried."""
    if progress.get("stage") == "posting":
        found = _find_created(client, fields, marker)
        if not found:
            raise CRMError("a previous create attempt has an unknown outcome and no matching account was found; "
                           "check the CRM before retrying")
        new_id = found
    else:
        store.save_progress(conn, pid, {"stage": "posting", "marker": marker})
        try:
            body = client.create_account(fields)
        except CRMError as e:
            if e.status is not None and e.status < 500:
                store.save_progress(conn, pid, {})  # definite rejection: nothing was created
                raise
            body = None
        new_id = (body.get("account_id") or body.get("id")) if isinstance(body, dict) else None
        new_id = new_id or _find_created(client, fields, marker)
        if not new_id:
            raise CRMError("account creation outcome unknown and no matching account found; not retrying POST")
    progress = {"stage": "created", "new_id": new_id, "marker": marker}
    store.save_progress(conn, pid, progress)
    return new_id, progress


def _accounts_at(client, fields: dict, exclude: set) -> list[str]:
    key = norm_street(fields["billing_street"])
    return [a["account_id"] for a in client.find_accounts(zip=fields["billing_zip"])
            if norm_street(a["billing_street"]) == key and a["account_id"] not in exclude
            and not a.get("duplicate_of_account") and not a.get("chow_current_account")]


def _find_created(client, fields: dict, marker: str) -> str | None:
    key = norm_street(fields["billing_street"])
    for a in client.find_accounts(zip=fields["billing_zip"]):
        if a.get("created_by_candidate") and norm_street(a["billing_street"]) == key and marker in (a.get("note") or ""):
            return a["account_id"]
    return None


def _already(client, account_id: str, fields: dict) -> bool:
    live = client.get_account(account_id)
    return all(live.get(f) == v for f, v in fields.items())


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
