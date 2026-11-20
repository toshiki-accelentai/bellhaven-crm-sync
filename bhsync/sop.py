"""Change-of-ownership SOP: when an account must move to a new parent, decide CHOW vs direct re-parent."""
from __future__ import annotations

CHOW, REPARENT = "CHOW", "REPARENT"


def money(v) -> float | None:
    """Parse a CRM money value; None if it can't be read."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def decide(lifetime_revenue, outstanding_ar) -> dict:
    """Revenue history AND outstanding AR > 0 -> preserve old account (CHOW); otherwise re-parent directly."""
    rev, ar = money(lifetime_revenue), money(outstanding_ar)
    if rev is None or ar is None:
        # Unreadable billing data: take the path that cannot break a billing record.
        return {"lifetime_revenue": lifetime_revenue, "outstanding_ar": outstanding_ar, "outcome": CHOW,
                "reason": "billing values unreadable; preserving old account to be safe"}
    if rev > 0 and ar > 0:
        outcome, reason = CHOW, "revenue history and outstanding AR > 0: billing needs the old account preserved"
    elif rev > 0:
        outcome, reason = REPARENT, "revenue history but no outstanding AR: re-parent directly"
    elif ar > 0:
        outcome, reason = REPARENT, "outstanding AR but no revenue history: re-parent directly"
    else:
        outcome, reason = REPARENT, "no revenue history and no AR: re-parent directly"
    return {"lifetime_revenue": rev, "outstanding_ar": ar, "outcome": outcome, "reason": reason}
