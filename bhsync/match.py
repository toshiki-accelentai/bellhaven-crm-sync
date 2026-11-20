"""Match scraped locations to CRM accounts and turn each finding into a reviewable proposal.

Pure function of (locations, accounts, contacts): no I/O, deterministic, so re-runs produce identical
fingerprints and the whole thing is unit-testable against fixtures.
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from . import config, sop
from .models import KINDS, Location

STREET_WORDS = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue", "blvd": "boulevard", "dr": "drive",
    "ln": "lane", "pk": "pike", "pke": "pike", "ct": "court", "pl": "place", "hwy": "highway",
    "pkwy": "parkway", "cir": "circle", "ter": "terrace", "sq": "square",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "nw": "northwest", "ne": "northeast", "sw": "southwest", "se": "southeast",
}
NAME_WORDS = {"&": "and", "rehab": "rehabilitation", "centre": "center", "ctr": "center"}
NAME_STOP = {"the", "of", "at"}
TODAY = "{today}"  # filled in at apply time so the date never changes a proposal's fingerprint


# ---------- normalisation ----------

def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9&]+", (s or "").lower())


def norm_street(street: str) -> tuple[str, str]:
    """('1120', 'west main street') — house number + canonical street words."""
    t = [STREET_WORDS.get(w, w) for w in _tokens(street)]
    return (t[0], " ".join(t[1:])) if t and t[0].isdigit() else ("", " ".join(t))


def norm_name(name: str) -> str:
    s = (name or "").lower().replace("health care", "healthcare")
    return " ".join(NAME_WORDS.get(w, w) for w in _tokens(s) if w not in NAME_STOP)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def summary(a: dict, contacts: Counter) -> dict:
    return {"account_id": a["account_id"], "name": a["name"], "street": a["billing_street"],
            "city": a["billing_city"], "state": a["billing_state"], "zip": a["billing_zip"],
            "parent_id": a["parent_id"], "parent_name": a["parent_name"], "status": a["status"],
            "care_type": a["care_type"], "lifetime_revenue": a["lifetime_revenue"],
            "outstanding_ar": a["outstanding_ar"], "contacts": contacts[a["account_id"]]}


def append_note(old: str, line: str) -> str:
    entry = f"{config.NOTE_PREFIX.format(date=TODAY)} {line}"
    return f"{old}\n{entry}" if old else entry


# ---------- matching ----------

def find_candidates(loc: Location, pool: list[dict]) -> tuple[list[dict], str]:
    num, street = norm_street(loc.street)
    same_addr = [a for a in pool if num and norm_street(a["billing_street"]) == (num, street)]
    rule_a = [a for a in same_addr if a["billing_zip"] == loc.zip]
    rule_b = [a for a in same_addr if a not in rule_a and a["billing_city"].lower() == loc.city.lower()
              and a["billing_state"] == loc.state]
    if rule_a or rule_b:
        label = "A: street+zip" if rule_a and not rule_b else ("B: street+city (zip differs)" if not rule_a
                                                               else "A+B: street+zip / street+city")
        return rule_a + rule_b, label
    # Fallback only when no address agrees: same name, same city, already under our parent (e.g. PO box).
    rule_c = [a for a in pool if a["parent_id"] == config.PARENT_ID and norm_name(a["name"]) == norm_name(loc.name)
              and a["billing_city"].lower() == loc.city.lower() and a["billing_state"] == loc.state]
    return rule_c, ("C: name+city under Bellhaven (address differs)" if rule_c else "none")


def near_misses(loc: Location, accounts: list[dict], exclude: set[str]) -> list[dict]:
    hits = [a for a in accounts if a["account_id"] not in exclude and a["billing_street"] and (
        (a["billing_city"].lower() == loc.city.lower() and a["billing_state"] == loc.state)
        or similarity(a["name"], loc.name) >= 0.6)]
    return sorted(hits, key=lambda a: -similarity(a["name"], loc.name))[:3]


def rank_survivor(group: list[dict], loc: Location, contacts: Counter) -> list[dict]:
    keys = {
        "has_revenue": lambda a: -(1 if (sop.money(a["lifetime_revenue"]) or 0) > 0 else 0),
        "contacts": lambda a: -contacts[a["account_id"]],
        "under_parent": lambda a: 0 if a["parent_id"] == config.PARENT_ID else 1,
        "name_similarity": lambda a: -round(similarity(a["name"], loc.name), 3),
        "lowest_id": lambda a: a["account_id"],
    }
    return sorted(group, key=lambda a: tuple(keys[r](a) for r in config.SURVIVOR_RULES))


def desired_fields(loc: Location, acct: dict) -> dict:
    """Fields the account should carry according to the website (only real differences)."""
    want = {}
    if acct["name"].strip() != loc.name:
        want["name"] = loc.name
    if norm_street(acct["billing_street"]) != norm_street(loc.street):
        want["billing_street"] = loc.street
    if acct["billing_city"].lower() != loc.city.lower():
        want["billing_city"] = loc.city
    if acct["billing_state"] != loc.state:
        want["billing_state"] = loc.state
    if acct["billing_zip"] != loc.zip:
        want["billing_zip"] = loc.zip
    mapped = [config.CARE_MAP[o] for o in loc.offerings if o in config.CARE_MAP]
    if mapped and acct["care_type"] not in mapped:
        want["care_type"] = mapped[0]
    if acct["status"] != "Active":
        want["status"] = "Active"
    return want


def new_account_fields(loc: Location, note: str) -> dict:
    mapped = [config.CARE_MAP[o] for o in loc.offerings if o in config.CARE_MAP]
    return {"name": loc.name, "parent_id": config.PARENT_ID, "billing_street": loc.street,
            "billing_city": loc.city, "billing_state": loc.state, "billing_zip": loc.zip,
            "care_type": mapped[0] if mapped else "", "phone": loc.phone, "status": "Active", "note": note}


# ---------- proposal builders ----------

def _proposal(kind, issue_key, title, facility, account_id, slug, summary_, diff, before, steps, evidence):
    return {"issue_key": issue_key, "kind": kind, "risk": KINDS.index(kind), "title": title,
            "facility": facility, "account_id": account_id, "slug": slug, "summary": summary_,
            "diff": diff, "before": before, "steps": steps,
            "evidence": {"website": None, "match_rule": "", "candidates": [], "near_misses": [], "sop": None,
                         "survivor_ranking": None, "notes": [], **evidence}}


def _diff(account_id, acct, fields):
    """Display-only diff (writes come from `steps`); parent ids are shown with their names."""
    def show(f, v, name=None):
        return f"{name or (config.PARENT_NAME if v == config.PARENT_ID else '')} [{v}]".strip() \
            if f == "parent_id" and v else v
    return [{"account_id": account_id, "field": f,
             "before": show(f, (acct or {}).get(f), (acct or {}).get("parent_name")), "after": show(f, v)}
            for f, v in fields.items() if f != "note"]


def facility_proposal(loc: Location, acct: dict, evidence: dict) -> dict | None:
    want = desired_fields(loc, acct)
    moving = acct["parent_id"] != config.PARENT_ID
    aid = acct["account_id"]
    if moving:
        decision = sop.decide(acct["lifetime_revenue"], acct["outstanding_ar"])
        evidence = {**evidence, "sop": decision}
        old_parent = acct["parent_name"] or "no parent"
        if decision["outcome"] == sop.CHOW:
            note = (f"CHOW successor of {aid} ({acct['name']}, previously under {old_parent}). Old account kept "
                    f"for billing (revenue {_amt(decision['lifetime_revenue'])}, AR {_amt(decision['outstanding_ar'])}).")
            fields = new_account_fields(loc, append_note("", note))
            # The old account is left exactly as it is apart from the pointer the SOP requires.
            return _proposal(
                "CHOW", f"FACILITY:{aid}", f"CHOW: new Bellhaven account for {loc.name}; old account points to it",
                loc.name, aid, loc.slug,
                f"Under {old_parent} in CRM, listed on the Bellhaven website. {decision['reason']}.",
                _diff("$new", None, fields) + [{"account_id": aid, "field": "chow_current_account",
                                                "before": acct["chow_current_account"], "after": "$new"}],
                {aid: {k: acct[k] for k in ("parent_id", "chow_current_account", "duplicate_of_account",
                                            "lifetime_revenue", "outstanding_ar", "status")}},
                [{"op": "create", "ref": "new", "fields": fields},
                 {"op": "patch", "account_id": aid, "fields": {"chow_current_account": "$new"}}],
                evidence)
        want = {**want, "parent_id": config.PARENT_ID}
    if not want:
        return None
    changes = ", ".join(f"{k} '{acct.get(k)}' -> '{v}'" for k, v in want.items())
    want["note"] = append_note(acct["note"], f"Updated from Bellhaven website ({loc.url}): {changes}.")
    before = {aid: {k: acct[k] for k in set(want) | {"parent_id", "lifetime_revenue", "outstanding_ar",
                                                     "chow_current_account", "duplicate_of_account"}}}
    cosmetic = set(want) - {"note"} == {"name"} and norm_name(acct["name"]) == norm_name(loc.name)
    kind = "REPARENT" if moving else ("COSMETIC" if cosmetic else "UPDATE")
    title = {"REPARENT": f"Re-parent {acct['name']} to Bellhaven",
             "COSMETIC": f"Align name: {acct['name']} -> {loc.name}",
             "UPDATE": f"Update {acct['name']} from website"}[kind]
    if moving and acct["name"] != loc.name:
        title += f" (and rename to {loc.name})"
    why = (f"Under {acct['parent_name'] or 'no parent'} in CRM, listed on the Bellhaven website. "
           f"{evidence['sop']['reason']}." if moving else f"CRM differs from the website: {changes}.")
    return _proposal(kind, f"FACILITY:{aid}", title, loc.name, aid, loc.slug, why,
                     _diff(aid, acct, want), before, [{"op": "patch", "account_id": aid, "fields": want}], evidence)


def duplicate_proposal(loser: dict, survivor: dict, loc: Location, ranking: list[str], rule: str) -> dict:
    lid, sid = loser["account_id"], survivor["account_id"]
    fields = {"duplicate_of_account": sid, "status": "Inactive",
              "note": append_note(loser["note"], f"Duplicate of {sid} ({survivor['name']}): same facility "
                                                 f"as Bellhaven website listing {loc.url}.")}
    return _proposal(
        "DUPLICATE", f"DUPLICATE:{lid}", f"Mark {loser['name']} as duplicate of {survivor['name']}",
        loc.name, lid, loc.slug,
        f"Same address as surviving account {sid}. Parent left unchanged ({loser['parent_name'] or 'none'}).",
        _diff(lid, loser, fields),
        # The survivor is snapshotted too: if it stops being a live record, the pointer must not be written.
        {lid: {k: loser[k] for k in ("status", "duplicate_of_account", "note")},
         sid: {k: survivor[k] for k in ("duplicate_of_account", "chow_current_account")}},
        [{"op": "patch", "account_id": lid, "fields": fields}],
        {"website": loc.to_dict(), "match_rule": rule, "survivor_ranking": ranking})


def stale_proposal(acct: dict) -> dict:
    aid = acct["account_id"]
    fields = {"status": "Needs Review",
              "note": append_note(acct["note"], "No longer listed on the Bellhaven website. Ownership unconfirmed: "
                                                "verify sale/closure before re-parenting or inactivating.")}
    return _proposal(
        "STALE", f"STALE:{aid}", f"Flag {acct['name']} for review: not on the Bellhaven website",
        acct["name"], aid, "",
        "Under Bellhaven in CRM but absent from the website. Parent kept: the new owner is unknown"
        + (" and the account carries AR." if (sop.money(acct["outstanding_ar"]) or 0) > 0 else "."),
        _diff(aid, acct, fields), {aid: {k: acct[k] for k in ("status", "note", "parent_id")}},
        [{"op": "patch", "account_id": aid, "fields": fields}], {})


def new_proposal(loc: Location, misses: list[dict]) -> dict:
    fields = new_account_fields(loc, append_note("", f"Created from Bellhaven website listing {loc.url}."))
    return _proposal(
        "NEW", f"NEW:{loc.slug}", f"Create account for {loc.name}", loc.name, "", loc.slug,
        "Listed on the Bellhaven website; no CRM account at this address."
        + (" Near-misses shown were checked and do not share the address." if misses else ""),
        _diff("$new", None, fields), {}, [{"op": "create", "ref": "new", "fields": fields}],
        {"website": loc.to_dict(), "match_rule": "none", "near_misses": misses})


# ---------- orchestration ----------

def build(locations: list[Location], accounts: list[dict], contacts: list[dict],
          site_complete: bool = True) -> tuple[list[dict], list[dict], list[str]]:
    """Return (proposals, classification report rows, warnings)."""
    counts = Counter(c["account_id"] for c in contacts)
    by_id = {a["account_id"]: a for a in accounts}
    warnings = _pointer_warnings(accounts, by_id)
    # Resolved records are history, not candidates: duplicates, and CHOW predecessors (their successor matches).
    pool = [a for a in accounts if a["billing_street"] and not a["duplicate_of_account"]
            and not a["chow_current_account"]]
    proposals, report, claimed = [], [], {}
    for loc in locations:
        cands, rule = find_candidates(loc, pool)
        for a in cands:
            if a["account_id"] in claimed:
                warnings.append(f"{a['account_id']} matches both {claimed[a['account_id']]} and {loc.slug}")
            claimed[a["account_id"]] = loc.slug
        if not cands:
            misses = [summary(a, counts) for a in near_misses(loc, accounts, set())]
            proposals.append(new_proposal(loc, misses))
            report.append(_row(loc, "NOT IN CRM", "", rule))
            continue
        ranked = rank_survivor(cands, loc, counts)
        survivor = ranked[0]
        ranking = [f"{a['account_id']} {a['name']} (rev {a['lifetime_revenue']}, contacts {counts[a['account_id']]},"
                   f" parent {a['parent_name'] or 'none'})" + (" <- survivor" if a is survivor else "")
                   for a in ranked]
        evidence = {"website": loc.to_dict(), "match_rule": rule,
                    "candidates": [summary(a, counts) for a in ranked],
                    "near_misses": [summary(a, counts) for a in near_misses(loc, accounts, {a["account_id"] for a in cands})
                                    ][:2],
                    "survivor_ranking": ranking if len(ranked) > 1 else None}
        prop = facility_proposal(loc, survivor, evidence)
        if prop:
            proposals.append(prop)
        proposals += [duplicate_proposal(a, survivor, loc, ranking, rule) for a in ranked[1:]]
        status = prop["kind"] if prop else "CONFIDENT MATCH"
        report.append(_row(loc, status + (f" +{len(ranked) - 1} DUPLICATE" if len(ranked) > 1 else ""),
                           survivor["account_id"], rule))
    for a in accounts:
        if (a["parent_id"] == config.PARENT_ID and a["account_id"] not in claimed and not a["duplicate_of_account"]
                and not a["chow_current_account"] and a["status"] == "Active"):
            report.append({"slug": "", "name": a["name"], "class": "NOT ON WEBSITE", "account_id": a["account_id"],
                           "rule": ""})
            if site_complete:
                proposals.append(stale_proposal(a))
    if not site_complete:
        warnings.append("Scrape incomplete: 'not on website' proposals withheld this run")
    return proposals, report, warnings


def _amt(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def _row(loc, cls, account_id, rule):
    return {"slug": loc.slug, "name": loc.name, "class": cls, "account_id": account_id, "rule": rule}


def _pointer_warnings(accounts, by_id) -> list[str]:
    out = []
    for a in accounts:
        for f in ("duplicate_of_account", "chow_current_account"):
            seen, cur = {a["account_id"]}, a[f]
            while cur:
                if cur not in by_id:
                    out.append(f"{a['account_id']}.{f} -> missing account {cur}")
                    break
                if cur in seen:
                    out.append(f"{a['account_id']}.{f} forms a cycle")
                    break
                seen.add(cur)
                cur = by_id[cur][f]
    return out
