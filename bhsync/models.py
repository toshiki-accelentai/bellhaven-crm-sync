"""Shared contracts. Owned by the lead; helpers code against these, never edit them."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Proposal kinds, in review-risk order (lowest number = review first).
KINDS = ("CHOW", "REPARENT", "DUPLICATE", "STALE", "NEW", "UPDATE", "COSMETIC")

# Proposal lifecycle.
PENDING, APPLIED, REJECTED, ERROR, NEEDS_REAPPROVAL = (
    "pending", "applied", "rejected", "error", "needs_reapproval")


@dataclass
class Location:
    """One community scraped from the Bellhaven website."""
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    offerings: list[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeMeta:
    stated_count: int | None  # "Today we proudly serve N communities" on the homepage
    directory_count: int | None  # "N communities listed" on the directory
    pages: int  # directory pages actually fetched
    warnings: list[str] = field(default_factory=list)


# A proposal is a plain dict (JSON-serialisable, stored in SQLite):
# {
#   "id": int,                       # assigned by store
#   "issue_key": str,                # e.g. "UPDATE:001ABC" or "NEW:bellhaven-of-batavia"
#   "fingerprint": str,              # sha256 of issue_key + before + steps
#   "kind": one of KINDS,
#   "risk": int,                     # KINDS.index(kind)
#   "title": str,                    # "Re-parent Bellhaven Crossings of Lima to Bellhaven"
#   "facility": str,                 # website name or CRM name
#   "account_id": str | "",          # primary CRM account touched ("" for NEW)
#   "slug": str | "",                # website slug ("" for STALE)
#   "summary": str,                  # one-line why
#   "diff": [{"account_id", "field", "before", "after"}],
#   "before": {account_id: {field: value}},   # snapshot checked again at approve time
#   "steps": [                       # executed in order by apply.approve
#       {"op": "create", "ref": "new", "fields": {...}},
#       {"op": "patch", "account_id": "<id or $new>", "fields": {...}},
#   ],
#   "evidence": {
#       "website": Location dict | None,
#       "match_rule": str,           # "A: address+zip" | "B: address+city" | "C: name+city" | "none"
#       "candidates": [account summary dicts],
#       "near_misses": [account summary dicts],
#       "sop": {"lifetime_revenue", "outstanding_ar", "outcome", "reason"} | None,
#       "survivor_ranking": [str] | None,
#       "notes": [str],
#   },
#   "status": PENDING | APPLIED | REJECTED | ERROR | NEEDS_REAPPROVAL,
#   "reason": str,                   # reviewer's reject reason / error text
#   "result": dict | None,           # API responses from apply
#   "recurrence": bool,
#   "created_at": str, "decided_at": str | None,
# }
# "account summary" = {account_id, name, street, city, state, zip, parent_id, parent_name,
#                     status, care_type, lifetime_revenue, outstanding_ar, contacts}
