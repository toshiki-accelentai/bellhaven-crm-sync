"""Prove the CRM end state: every change is explained by an approved proposal and nothing else moved."""
from __future__ import annotations

from . import config
from .models import APPLIED

IGNORED = {"updated_at", "parent_name"}  # server-maintained / derived from parent_id


def verify(baseline: list[dict], live: list[dict], proposals: list[dict]) -> tuple[bool, list[str]]:
    base = {a["account_id"]: a for a in baseline}
    now = {a["account_id"]: a for a in live}
    applied = [p for p in proposals if p["status"] == APPLIED]
    allowed: dict[str, set] = {}  # account_id -> fields an approved proposal wrote
    created = set()
    for p in applied:
        res = p.get("result") or {}
        for s in p["steps"]:
            target = res.get("new_id") if s.get("account_id", "$new") == "$new" else s["account_id"]
            if s["op"] == "create":
                created.add(res.get("new_id"))
            else:
                allowed.setdefault(target, set()).update(s["fields"])
    ok, lines = True, []

    def fail(msg):
        nonlocal ok
        ok = False
        lines.append("FAIL " + msg)

    for aid, a in now.items():
        if aid not in base:
            if aid in created:
                lines.append(f"ok   created {aid} {a['name']}")
            else:
                fail(f"{aid} {a['name']} exists but no approved proposal created it")
            continue
        changed = {f for f in a if f not in IGNORED and a[f] != base[aid].get(f)}
        if not changed:
            continue
        extra = changed - allowed.get(aid, set())
        if extra:
            fail(f"{aid} {a['name']} changed without approval: {sorted(extra)}")
        else:
            lines.append(f"ok   {aid} {a['name']}: {sorted(changed)}")
    for aid in base.keys() - now.keys():
        fail(f"{aid} disappeared")

    for aid, a in now.items():
        if a["chow_current_account"]:
            changed = {f for f in a if f not in IGNORED and a[f] != base.get(aid, {}).get(f)}
            if changed - {"chow_current_account"}:
                fail(f"CHOW predecessor {aid} touched beyond the pointer: {sorted(changed)}")
            succ = now.get(a["chow_current_account"])
            if not succ or succ["parent_id"] != config.PARENT_ID:
                fail(f"CHOW predecessor {aid} points to a missing or non-Bellhaven account")
        if a["duplicate_of_account"]:
            surv = now.get(a["duplicate_of_account"])
            if a["status"] != "Inactive":
                fail(f"duplicate {aid} is not Inactive")
            if not surv or surv["duplicate_of_account"]:
                fail(f"duplicate {aid} points to a missing or non-surviving account")
    lines.append(f"{'PASS' if ok else 'FAIL'}: {len(applied)} applied proposals, "
                 f"{sum(1 for l in lines if l.startswith('ok'))} account changes explained")
    return ok, lines
